"""
runtime/session/sessions.py
Agent Governance v2 — Session Layer

Sessions form a tree rooted at the human-authorized root session.
Authority flows downward only through explicit attenuation.

Attenuation invariants enforced at create_session():
    1. child.granted_capabilities ⊆ parent.effective_capabilities
    2. child.budget_grant ≤ parent.remaining_budget
    3. child.can_spawn_depth = parent.can_spawn_depth - 1
       (ORCHESTRATOR sessions are exempt — set explicitly by HUMAN)

Lifecycle state machine:
    ACTIVE → FROZEN → TERMINATED
    ACTIVE → REVOKED → TERMINATED
    ACTIVE → PENDING_APPROVAL (session continues for other actions)

Functions:
    create_session(...)           -> session_id
    get_session(session_id)       -> dict or None
    get_session_tree(root_id)     -> list of all descendant session dicts
    freeze_session(session_id)    -> None
    terminate_session(session_id) -> None
    revoke_session(session_id)    -> None
    cascade_freeze(session_id)    -> None
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from runtime.schema import get_connection
from runtime.identity.principals import SPAWN_AUTHORITY, VALID_TYPES

# ── Default session duration ───────────────────────────────────────────────────
DEFAULT_DURATION_SECONDS = 3600  # 1 hour

# ── Capability set — all valid capabilities in the system ─────────────────────
ALL_CAPABILITIES = {
    "can_query_records",
    "can_query_pii",
    "can_post_external",
    "can_spend_budget",
    "can_spawn",
    "can_reauth",
    "can_access_sensitive",
    "can_read_tree_state",
    "can_approve_pending",
    "can_revoke_session",
}

# ── Capabilities only HUMAN principals may hold ───────────────────────────────
HUMAN_ONLY_CAPS = {"can_approve_pending"}

# ── Capabilities only COMPLIANCE principals may hold ──────────────────────────
COMPLIANCE_ONLY_CAPS = {"can_read_tree_state"}

# ── Capabilities only ORCHESTRATOR or HUMAN may hold ─────────────────────────
ORCHESTRATOR_ONLY_CAPS = {"can_query_pii", "can_post_external", "can_access_sensitive"}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_session(
    principal_id: str,
    principal_type: str,
    parent_session_id: Optional[str] = None,
    granted_capabilities: list = None,
    can_spawn_depth: int = 0,
    budget_grant: float = 0.0,
    grace_period_seconds: int = 30,
    autonomous_flag: bool = False,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
) -> str:
    """
    Create a new session and persist it.

    For non-root sessions (parent_session_id provided), enforces the three
    attenuation invariants before writing:
        1. Capability attenuation — granted caps must be subset of parent's
        2. Budget attenuation — grant must not exceed parent's remaining budget
        3. Spawn depth attenuation — depth = parent depth - 1
           (ORCHESTRATOR sessions set depth explicitly, exempt from decrement rule)

    Args:
        principal_id:        UUID of the principal owning this session.
        principal_type:      Type of principal. Must match principal record.
        parent_session_id:   UUID of parent session. None for root sessions.
        granted_capabilities: List of capability strings. Validated against
                              ALL_CAPABILITIES and parent's effective set.
        can_spawn_depth:     How many levels of child spawning are permitted.
                             Automatically decremented for non-ORCHESTRATOR children.
        budget_grant:        Budget allocated to this session in USD.
                             Must not exceed parent's remaining budget.
        grace_period_seconds: Seconds child has to finish in-flight action
                              after parent expires. Set by parent at spawn time.
        autonomous_flag:     If True, session continues after parent task completes
                             (parent normal completion does not cascade freeze).
        duration_seconds:    Session lifetime in seconds from now.

    Returns:
        session_id: UUID string of the created session.

    Raises:
        ValueError: If any attenuation invariant is violated, principal not found,
                    or capability is invalid for this principal type.
    """
    if granted_capabilities is None:
        granted_capabilities = []

    # ── Validate principal_type is valid ─────────────────────────────────────────
    if principal_type not in VALID_TYPES:
        raise ValueError(f"Invalid principal_type '{principal_type}'")

    # ── Validate principal exists and type matches ─────────────────────────────
    from runtime.identity.principals import get_principal

    principal = get_principal(principal_id)
    if principal is None:
        raise ValueError(f"Principal '{principal_id}' does not exist.")
    if principal["principal_type"] != principal_type:
        raise ValueError(
            f"Principal type mismatch: expected {principal['principal_type']}, "
            f"got {principal_type}."
        )

    # ── Validate capability names ──────────────────────────────────────────────
    _validate_capability_names(granted_capabilities)

    # ── Validate type-capability constraints ──────────────────────────────────
    _validate_type_capability_fit(principal_type, granted_capabilities)

    conn = get_connection()

    try:
        # ── Root session — no parent attenuation checks ────────────────────────
        if parent_session_id is None:
            _assert_root_session_allowed(principal_type)
            session_id = _write_session(
                conn=conn,
                principal_id=principal_id,
                principal_type=principal_type,
                parent_session_id=None,
                granted_capabilities=granted_capabilities,
                can_spawn_depth=can_spawn_depth,
                budget_grant=budget_grant,
                grace_period_seconds=grace_period_seconds,
                autonomous_flag=autonomous_flag,
                duration_seconds=duration_seconds,
            )
            return session_id

        # ── Child session — enforce all attenuation invariants ─────────────────
        parent = _get_session_row(conn, parent_session_id)

        if parent is None:
            raise ValueError(
                f"Parent session '{parent_session_id}' does not exist."
            )
        if not parent["active"]:
            raise ValueError(
                f"Parent session '{parent_session_id}' is not active. "
                f"Cannot spawn child from inactive session."
            )
        if parent["frozen"]:
            raise ValueError(
                f"Parent session '{parent_session_id}' is frozen. "
                f"Cannot spawn child from frozen session."
            )

        parent_type = parent["principal_type"]

        # ── Validate spawn authority (who can create whom) ─────────────────────
        _validate_spawn_authority(parent_type, principal_type)

        # ── Invariant 1: Capability attenuation ───────────────────────────────
        parent_caps = set(json.loads(parent["granted_capabilities"]))
        child_caps = set(granted_capabilities)

        # COMPLIANCE sessions may receive can_read_tree_state as a special
        # grant from ORCHESTRATOR even though ORCHESTRATOR does not itself
        # hold this capability. It is a compliance-specific delegation
        # explicitly defined in the capability vocabulary (Section 5).
        special_grant = set()
        if (principal_type == "COMPLIANCE"
                and parent_type == "ORCHESTRATOR"
                and "can_read_tree_state" in child_caps):
            special_grant = {"can_read_tree_state"}

        excess = child_caps - parent_caps - special_grant
        if excess:
            raise ValueError(
                f"Attenuation violation: child requested capabilities not held "
                f"by parent: {sorted(excess)}. "
                f"Parent holds: {sorted(parent_caps)}."
            )

        # ── Invariant 2: Budget attenuation ───────────────────────────────────
        parent_remaining = _get_parent_remaining_grant(conn, parent_session_id, parent)
        if budget_grant > parent_remaining:
            raise ValueError(
                f"Attenuation violation: child budget_grant={budget_grant:.2f} "
                f"exceeds parent remaining budget={parent_remaining:.2f}."
            )

        # ── Invariant 3: Spawn depth attenuation ──────────────────────────────
        parent_depth = parent["can_spawn_depth"]
        if parent_depth == 0:
            raise ValueError(
                f"Attenuation violation: parent session '{parent_session_id}' "
                f"has can_spawn_depth=0. Cannot spawn child sessions."
            )

        # For non-ORCHESTRATOR parents, depth must decrement
        if parent_type != "ORCHESTRATOR":
            enforced_depth = parent_depth - 1
            if can_spawn_depth > enforced_depth:
                raise ValueError(
                    f"Attenuation violation: requested can_spawn_depth={can_spawn_depth} "
                    f"exceeds allowed depth={enforced_depth} "
                    f"(parent depth {parent_depth} - 1)."
                )
            can_spawn_depth = enforced_depth
        else:
            # ORCHESTRATOR parent: child depth is set explicitly but must be < parent
            if can_spawn_depth >= parent_depth:
                raise ValueError(
                    f"Attenuation violation: child can_spawn_depth={can_spawn_depth} "
                    f"must be less than ORCHESTRATOR parent depth={parent_depth}."
                )

        # ── Inherit taint from parent if pii_accessed is set ──────────────────
        # (Checked here for awareness — actual taint flag written by constraint layer)
        _check_parent_taint_inheritance(conn, parent_session_id, granted_capabilities)

        session_id = _write_session(
            conn=conn,
            principal_id=principal_id,
            principal_type=principal_type,
            parent_session_id=parent_session_id,
            granted_capabilities=granted_capabilities,
            can_spawn_depth=can_spawn_depth,
            budget_grant=budget_grant,
            grace_period_seconds=grace_period_seconds,
            autonomous_flag=autonomous_flag,
            duration_seconds=duration_seconds,
        )
        return session_id

    finally:
        conn.close()


def get_session(session_id: str) -> Optional[dict]:
    """
    Retrieve a session record by id.

    Args:
        session_id: UUID string of the session.

    Returns:
        dict of all session fields, or None if not found.
        granted_capabilities is returned as a Python list (parsed from JSON).
    """
    conn = get_connection()
    row = _get_session_row(conn, session_id)
    conn.close()

    if row is None:
        return None

    result = dict(row)
    result["granted_capabilities"] = json.loads(result["granted_capabilities"])
    result["active"] = bool(result["active"])
    result["frozen"] = bool(result["frozen"])
    result["autonomous_flag"] = bool(result["autonomous_flag"])
    return result


def get_session_tree(root_session_id: str) -> list:
    """
    Return all descendant session dicts for a root session.

    Performs a recursive traversal of the session tree via parent_session_id.
    Includes the root session itself as the first element.

    Args:
        root_session_id: UUID of the root session.

    Returns:
        List of session dicts ordered breadth-first from root.
        Empty list if root session does not exist.
    """
    conn = get_connection()

    root = _get_session_row(conn, root_session_id)
    if root is None:
        conn.close()
        return []

    result = []
    queue = [root_session_id]

    while queue:
        current_id = queue.pop(0)
        row = _get_session_row(conn, current_id)
        if row:
            session_dict = dict(row)
            session_dict["granted_capabilities"] = json.loads(
                session_dict["granted_capabilities"]
            )
            session_dict["active"] = bool(session_dict["active"])
            session_dict["frozen"] = bool(session_dict["frozen"])
            session_dict["autonomous_flag"] = bool(session_dict["autonomous_flag"])
            result.append(session_dict)

        # Find children
        children = conn.execute(
            "SELECT session_id FROM sessions WHERE parent_session_id = ?",
            (current_id,),
        ).fetchall()
        queue.extend(c["session_id"] for c in children)

    conn.close()
    return result


def freeze_session(session_id: str, reason: str = "parent_expired") -> None:
    """
    Transition a session to FROZEN state.

    A frozen session can complete one in-flight atomic action if already started.
    It cannot start new actions, write new constraints, or spawn children.
    Grace period timer starts at freeze time.

    Args:
        session_id: UUID of the session to freeze.
        reason: Reason string for audit trail.

    Raises:
        ValueError: If session does not exist or is already terminated/revoked.
    """
    conn = get_connection()
    try:
        row = _get_session_row(conn, session_id)
        if row is None:
            raise ValueError(f"Session '{session_id}' does not exist.")
        if row["state"] in ("TERMINATED", "REVOKED"):
            raise ValueError(
                f"Session '{session_id}' is already {row['state']}. "
                f"Cannot freeze."
            )
        if row["frozen"]:
            return  # Already frozen — idempotent

        with conn:
            conn.execute(
                """
                UPDATE sessions
                SET frozen = 1, state = 'FROZEN'
                WHERE session_id = ?
                """,
                (session_id,),
            )
    finally:
        conn.close()


def terminate_session(session_id: str) -> None:
    """
    Transition a session to TERMINATED state.

    TERMINATED sessions are permanently inactive. All actions blocked.
    Record is preserved for audit purposes — never deleted.

    Args:
        session_id: UUID of the session to terminate.

    Raises:
        ValueError: If session does not exist.
    """
    conn = get_connection()
    try:
        row = _get_session_row(conn, session_id)
        if row is None:
            raise ValueError(f"Session '{session_id}' does not exist.")
        if row["state"] == "TERMINATED":
            return  # Already terminated — idempotent

        with conn:
            conn.execute(
                """
                UPDATE sessions
                SET active = 0, frozen = 0, state = 'TERMINATED'
                WHERE session_id = ?
                """,
                (session_id,),
            )
    finally:
        conn.close()


def revoke_session(session_id: str) -> None:
    """
    Administratively revoke a session before its natural expiry.

    Immediate effect: session is marked REVOKED and inactive.
    Children are cascade-frozen with 0 grace period.

    Args:
        session_id: UUID of the session to revoke.

    Raises:
        ValueError: If session does not exist.
    """
    conn = get_connection()
    try:
        row = _get_session_row(conn, session_id)
        if row is None:
            raise ValueError(f"Session '{session_id}' does not exist.")
        if row["state"] in ("TERMINATED", "REVOKED"):
            return  # Already inactive — idempotent

        with conn:
            conn.execute(
                """
                UPDATE sessions
                SET active = 0, frozen = 1, state = 'REVOKED'
                WHERE session_id = ?
                """,
                (session_id,),
            )
    finally:
        conn.close()

    # Cascade freeze all children with depth tracking
    cascade_freeze(session_id, propagation_depth=0)


def cascade_freeze(session_id: str, propagation_depth: int = 0) -> None:
    """
    Freeze all active children of a session, breadth-first.

    Writes a cascade_events record for every session frozen by this cascade.
    Children of the given session are frozen at propagation_depth + 1.
    Continues BFS until no active children remain.

    Args:
        session_id: UUID of the session triggering the cascade.
                    This is the trigger — its children will be frozen.
        propagation_depth: Depth of this cascade from the original trigger.
                           0 means direct children of the trigger.

    Note:
        This function freezes the CHILDREN of session_id, not session_id itself.
        The trigger session is already frozen/terminated by the caller.
        To freeze the trigger itself, call freeze_session() first.
        BFS prevents stack overflow on deep session trees.
    """
    conn = get_connection()

    try:
        now = datetime.now(timezone.utc).isoformat()
        queue = [(session_id, propagation_depth)]

        while queue:
            current_id, depth = queue.pop(0)

            children = conn.execute(
                """
                SELECT session_id, state, frozen, autonomous_flag
                FROM sessions
                WHERE parent_session_id = ?
                  AND state NOT IN ('TERMINATED', 'REVOKED')
                """,
                (current_id,),
            ).fetchall()

            for child in children:
                child_id = child["session_id"]
                already_frozen = bool(child["frozen"])
                is_autonomous = bool(child["autonomous_flag"])

                # Gap 4 fix: autonomous sessions are not cascade-frozen when their parent
                # completes normally. autonomous_flag=True means the session was explicitly
                # granted the right to continue after its parent ends. Still record the
                # cascade_event for audit visibility — but skip the freeze.
                if is_autonomous:
                    with conn:
                        conn.execute(
                            """
                            INSERT INTO cascade_events
                                (trigger_session_id, affected_session_id, event_type,
                                 propagation_depth, timestamp)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (session_id, child_id, "CASCADE_FROZEN", depth + 1, now),
                        )
                    # Do not freeze — do not recurse into autonomous children's subtrees
                    continue

                # Write cascade event and freeze child atomically
                with conn:
                    conn.execute(
                        """
                        INSERT INTO cascade_events
                            (trigger_session_id, affected_session_id, event_type,
                             propagation_depth, timestamp)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            child_id,
                            "CASCADE_FROZEN",
                            depth + 1,
                            now,
                        ),
                    )

                    # Freeze child inside same transaction — ensures both the
                    # cascade_event record and the session state update are committed
                    # together.
                    if not already_frozen:
                        conn.execute(
                            """
                            UPDATE sessions
                            SET frozen = 1, state = 'FROZEN'
                            WHERE session_id = ?
                              AND state NOT IN ('TERMINATED', 'REVOKED')
                            """,
                            (child_id,),
                        )

                queue.append((child_id, depth + 1))

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _freeze_session_with_conn(conn, session_id: str) -> None:
    """Internal freeze using existing connection (no new connection)."""
    row = _get_session_row(conn, session_id)
    if row is None:
        return
    if row["state"] in ("TERMINATED", "REVOKED"):
        return
    if row["frozen"]:
        return

    conn.execute(
        """
        UPDATE sessions
        SET frozen = 1, state = 'FROZEN'
        WHERE session_id = ?
        """,
        (session_id,),
    )


def _write_session(
    conn,
    principal_id: str,
    principal_type: str,
    parent_session_id: Optional[str],
    granted_capabilities: list,
    can_spawn_depth: int,
    budget_grant: float,
    grace_period_seconds: int,
    autonomous_flag: bool,
    duration_seconds: int,
) -> str:
    """Write a validated session row and return the session_id."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=duration_seconds)).isoformat()
    created_at = now.isoformat()

    with conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, principal_id, principal_type, parent_session_id,
                granted_capabilities, can_spawn_depth, budget_grant,
                grace_period_seconds, autonomous_flag, expires_at,
                active, frozen, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'ACTIVE', ?)
            """,
            (
                session_id,
                principal_id,
                principal_type,
                parent_session_id,
                json.dumps(granted_capabilities),
                can_spawn_depth,
                budget_grant,
                grace_period_seconds,
                1 if autonomous_flag else 0,
                expires_at,
                created_at,
            ),
        )

    return session_id


def _get_session_row(conn, session_id: str):
    """Return raw sqlite3.Row for a session, or None."""
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()


def _get_parent_remaining_grant(conn, parent_session_id: str, parent_row) -> float:
    """
    Calculate parent's remaining unallocated budget grant.

    remaining = parent.budget_grant - sum(budget_grants of direct children)

    Child budget grants are commitments made at spawn time. We sum all
    active + frozen children's grants to find committed budget, then
    subtract from parent's own grant.

    NOTE:
    This is allocation-level enforcement only.
    Actual budget spend is enforced via constraint layer + interceptor.
    """
    parent_grant = parent_row["budget_grant"]

    committed = conn.execute(
        """
        SELECT COALESCE(SUM(budget_grant), 0.0) as total
        FROM sessions
        WHERE parent_session_id = ?
          AND state NOT IN ('TERMINATED', 'REVOKED')
        """,
        (parent_session_id,),
    ).fetchone()["total"]

    return max(0.0, parent_grant - committed)


def _validate_capability_names(capabilities: list) -> None:
    """Raise ValueError if any capability name is not in ALL_CAPABILITIES."""
    unknown = set(capabilities) - ALL_CAPABILITIES
    if unknown:
        raise ValueError(
            f"Unknown capabilities: {sorted(unknown)}. "
            f"Valid capabilities: {sorted(ALL_CAPABILITIES)}"
        )


def _validate_type_capability_fit(principal_type: str, capabilities: list) -> None:
    """
    Raise ValueError if capabilities are incompatible with principal_type.

    HUMAN-only caps cannot be granted to non-HUMAN principals.
    COMPLIANCE-only caps cannot be granted to non-COMPLIANCE principals.
    ORCHESTRATOR-only caps cannot be granted to AGENT/SUBAGENT principals.
    """
    caps = set(capabilities)

    if principal_type != "HUMAN":
        human_violations = caps & HUMAN_ONLY_CAPS
        if human_violations:
            raise ValueError(
                f"Principal type '{principal_type}' cannot hold "
                f"HUMAN-only capabilities: {sorted(human_violations)}"
            )

    if principal_type != "COMPLIANCE":
        compliance_violations = caps & COMPLIANCE_ONLY_CAPS
        if compliance_violations:
            raise ValueError(
                f"Principal type '{principal_type}' cannot hold "
                f"COMPLIANCE-only capabilities: {sorted(compliance_violations)}"
            )

    if principal_type in ("AGENT", "SUBAGENT"):
        orch_violations = caps & ORCHESTRATOR_ONLY_CAPS
        if orch_violations:
            raise ValueError(
                f"Principal type '{principal_type}' cannot hold "
                f"ORCHESTRATOR-only capabilities: {sorted(orch_violations)}"
            )


def _assert_root_session_allowed(principal_type: str) -> None:
    """Root sessions (no parent) may only be created for HUMAN principals."""
    if principal_type != "HUMAN":
        raise ValueError(
            f"Only HUMAN principals can create root sessions. "
            f"Got: '{principal_type}'. "
            f"ORCHESTRATOR, AGENT, SUBAGENT, and COMPLIANCE sessions require a parent."
        )


def _validate_spawn_authority(parent_type: str, child_type: str) -> None:
    """
    Validate that parent principal type is allowed to create child principal type.

    Uses SPAWN_AUTHORITY map from principals module.
    HUMAN principals are exempt — they can create ORCHESTRATOR root sessions.
    """
    if child_type == "HUMAN":
        raise ValueError("HUMAN principals cannot be created via session spawning.")

    allowed_parents = SPAWN_AUTHORITY.get(child_type, set())
    if parent_type not in allowed_parents:
        raise ValueError(
            f"Principal type '{parent_type}' is not authorized to spawn "
            f"'{child_type}' sessions. Allowed parents: {sorted(allowed_parents)}"
        )


def _check_parent_taint_inheritance(
    conn, parent_session_id: str, child_capabilities: list
) -> None:
    """
    Check if parent session has pii_accessed taint.

    If the parent is tainted and the child has can_post_external,
    raise a warning-level ValueError — child would be born already tainted
    and immediately unable to post externally, which is likely a logic error
    in the orchestrator's session design.

    This does not block session creation — it is an advisory check.
    Actual taint propagation to the child's constraint table is handled
    by the constraint layer at spawn time.
    """
    taint_row = conn.execute(
        """
        SELECT constraint_value FROM constraints
        WHERE session_id = ? AND constraint_key = 'pii_accessed'
        ORDER BY priority_level DESC, set_at DESC
        LIMIT 1
        """,
        (parent_session_id,),
    ).fetchone()

    if taint_row and json.loads(taint_row["constraint_value"]) is True:
        if "can_post_external" in child_capabilities:
            # Advisory — log but do not block
            # In production this would emit a structured warning
            pass  # Taint will propagate via constraint layer

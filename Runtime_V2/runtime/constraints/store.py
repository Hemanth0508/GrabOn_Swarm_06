"""
runtime/constraints/store.py
Agent Governance v2 — Constraint Store

The constraint store is the core state engine of the governance runtime.
It is the single source of truth for all evolving session state.

Design invariants:
    - APPEND-ONLY: rows are never updated or deleted after insert
    - AUTHORITY: only authorized principal types may write each key
    - PRIORITY: higher priority writes shadow lower priority writes
    - VERSION: monotonic counter increments on every write per session
    - TAINT PROPAGATION: pii_accessed and session_taint propagate upward
      synchronously in the same write transaction as the triggering write
    - SNAPSHOTS: written every 100 rows per session for fast reconstruction

Functions:
    write_constraint(...)         -> None
    get_constraint(...)           -> Any
    get_constraint_version(...)   -> int
    snapshot_if_needed(...)       -> None
    reconstruct_state(...)        -> dict
"""

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from runtime.schema import get_connection

# ── Global budget lock ─────────────────────────────────────────────────────────
# Shared across all constraint writes that touch budget_spent or principal_ledger.
# The interceptor acquires this lock before reading + writing budget state.
# Exposed here so the interceptor can import and use it directly.
_budget_lock = threading.Lock()

# ── INTERCEPTOR system principal UUID ─────────────────────────────────────────
# Well-known UUID for all INTERCEPTOR-authored constraint writes.
# Bootstrapped into the principals table by schema.init_db() with type=COMPLIANCE.
# Used as written_by_principal on system-generated state transitions so that the
# FOREIGN KEY constraint on constraints.written_by_principal is satisfied and
# every write is traceable in the audit trail.
INTERCEPTOR_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000000"

# ── Snapshot interval ──────────────────────────────────────────────────────────
SNAPSHOT_INTERVAL = 100  # write snapshot every N constraint rows per session

# ── Taint keys that propagate upward synchronously ────────────────────────────
UPWARD_TAINT_KEYS = {"pii_accessed", "session_taint"}

# ── Default values for constraints that may not have been set ─────────────────
CONSTRAINT_DEFAULTS: dict[str, Any] = {
    "budget_spent":    0.0,
    "pii_accessed":    False,
    "reauth_verified": False,
    "session_taint":   False,
    "budget_limit":    None,
    "budget_grant":    None,
    "can_spawn_depth": None,
    "grace_period_seconds": None,
}

# ── Constraint authority map ───────────────────────────────────────────────────
# Defines which principal types are authorized to write each constraint key.
# Writes from unauthorized principal types are rejected before any DB access.
# "INTERCEPTOR" is a synthetic type used for writes made by the interceptor
# itself (not a real principal — the interceptor calls write_constraint with
# principal_type="INTERCEPTOR" for system-generated state transitions).
CONSTRAINT_AUTHORITY_MAP: dict[str, list[str]] = {
    "budget_limit":          ["HUMAN"],
    "budget_spent":          ["INTERCEPTOR"],
    "pii_accessed":          ["INTERCEPTOR"],
    "reauth_verified":       ["INTERCEPTOR"],
    "can_spawn_depth":       ["ORCHESTRATOR", "HUMAN"],
    "session_taint":         ["INTERCEPTOR"],
    "budget_grant":          ["ORCHESTRATOR"],
    "grace_period_seconds":  ["ORCHESTRATOR"],
    # Cross-session spend ceiling per principal — enforced by interceptor ledger check.
    # Written by HUMAN at system setup time. Without this key in the map,
    # write_constraint() raises ValueError and the enforcement path in validate.py
    # (the principal_budget_limit ledger check) can never be activated.
    "principal_budget_limit": ["HUMAN"],
    # Merchant risk verdict — written by COMPLIANCE Agent after conflict resolution.
    # Only COMPLIANCE may write this key — ensures verdicts are authoritative.
    # Used in Event 21 typed protocol conflict resolution scenario.
    "merchant_risk": ["COMPLIANCE"],
}

# ── Priority levels ────────────────────────────────────────────────────────────
# Higher number = higher priority. Current value = highest priority most-recent row.
PRIORITY_HUMAN       = 1  # PRIORITY_1 in spec — highest, cannot be overridden
PRIORITY_INTERCEPTOR = 1  # INTERCEPTOR writes are also PRIORITY_1
PRIORITY_ORCHESTRATOR = 2  # PRIORITY_2 — can be shadowed by PRIORITY_1
PRIORITY_DEFAULT     = 2  # fallback for keys not in the authority map

# Map from authorized writer to its priority level
_WRITER_PRIORITY: dict[str, int] = {
    "HUMAN":        PRIORITY_HUMAN,
    "INTERCEPTOR":  PRIORITY_INTERCEPTOR,
    "ORCHESTRATOR": PRIORITY_ORCHESTRATOR,
    "AGENT":        PRIORITY_DEFAULT,
    "SUBAGENT":     PRIORITY_DEFAULT,
    "COMPLIANCE":   PRIORITY_DEFAULT,
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def write_constraint(
    session_id: str,
    key: str,
    value: Any,
    written_by_principal: str,
    principal_type: str,
    priority_level: Optional[int] = None,
    valid_until: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    signature: Optional[str] = None,
) -> None:
    """
    Append a new constraint row for this session and key.

    Enforces the complete authority model before writing:
        1. Principal type must be authorized to write this key
        2. No higher-priority write may exist for this key in this session
        3. Signature is verified if provided; required for HUMAN and INTERCEPTOR principals
        4. Session version counter is incremented atomically
        5. Taint keys (pii_accessed, session_taint) propagate upward
           to parent session in the same write transaction

    Args:
        session_id:          UUID of the session owning this constraint.
        key:                 Constraint key. Must exist in CONSTRAINT_AUTHORITY_MAP
                             or the write will be rejected with ValueError.
        value:               Python value. Stored as JSON. Any JSON-serializable type.
        written_by_principal: UUID of the writing principal.
        principal_type:      Type of the writing principal. Checked against
                             CONSTRAINT_AUTHORITY_MAP for this key.
        priority_level:      Override priority. If None, derived from principal_type.
        valid_until:         ISO8601 timestamp after which this constraint expires.
                             None means no TTL — constraint persists until session end.
        idempotency_key:     Unique key for deduplication. If already written,
                             this call is a no-op (idempotent).
        signature:           Base64-encoded Ed25519 signature over the constraint
                             payload. Verified if provided. HUMAN and INTERCEPTOR
                             principals should always provide a signature.

    Raises:
        ValueError: If principal_type is not authorized to write this key.
        ValueError: If a higher-priority write already exists for this key.
        ValueError: If value is not JSON-serializable.
    """
    conn = get_connection()
    try:
        _write_constraint_internal(
            conn=conn,
            session_id=session_id,
            key=key,
            value=value,
            written_by_principal=written_by_principal,
            principal_type=principal_type,
            priority_level=priority_level,
            valid_until=valid_until,
            idempotency_key=idempotency_key,
            signature=signature,
            propagate_taint=True,
        )
    finally:
        conn.close()


def get_constraint(session_id: str, key: str) -> Any:
    """
    Return the current effective value of a constraint for this session.

    Current value = highest-priority, most-recent, non-expired row
    for this (session_id, key) pair.

    Priority resolution order:
        1. Rows with higher priority_level win over lower priority rows
        2. Among equal-priority rows, the most recently written row wins
        3. Expired rows (valid_until < now) are excluded

    Args:
        session_id: UUID of the session.
        key:        Constraint key to read.

    Returns:
        The current Python value of the constraint.
        Returns the defined default from CONSTRAINT_DEFAULTS if no row exists.
        Returns None if key has no default and no row exists.
    """
    conn = get_connection()
    try:
        return _read_constraint(conn, session_id, key)
    finally:
        conn.close()


def get_constraint_version(session_id: str) -> int:
    """
    Return the current monotonic version counter for this session.

    The version counter increments on every constraint write to this session.
    Used by the interceptor's read-your-writes consistency check:
    if the caller's last-seen version is lower than the current version,
    the interceptor re-reads all constraints before evaluating.

    Args:
        session_id: UUID of the session.

    Returns:
        Current version integer. 0 if no constraints have been written.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT MAX(version) as current_version
            FROM constraints
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        v = row["current_version"] if row else None
        return v if v is not None else 0
    finally:
        conn.close()


def snapshot_if_needed(session_id: str) -> None:
    """
    Write a constraint snapshot if the row count threshold is reached.

    A snapshot captures the complete current state of all constraints
    for a session at the current version. Written every SNAPSHOT_INTERVAL
    (100) rows. Reconstruction reads the latest snapshot then replays only
    writes after it — preventing O(n) full-history replay on long sessions.

    Args:
        session_id: UUID of the session to snapshot.

    Note:
        This is called automatically by write_constraint after every write.
        It is exposed publicly so the interceptor can trigger it explicitly
        if needed (e.g. before a long-running workflow starts).
    """
    conn = get_connection()
    try:
        _snapshot_if_needed_internal(conn, session_id)
    finally:
        conn.close()


def reconstruct_state(session_id: str) -> dict:
    """
    Reconstruct the complete current constraint state for a session.

    Algorithm:
        1. Find the latest snapshot for this session (if any)
        2. Start with snapshot state as base (or empty dict)
        3. Replay all constraint writes after the snapshot version,
           applying priority resolution
        4. Filter out expired constraints
        5. Return the final state dict

    This is O(writes since last snapshot) rather than O(all writes ever).

    Args:
        session_id: UUID of the session.

    Returns:
        dict mapping constraint_key -> current_value for all active constraints.
        Empty dict if no constraints exist.
    """
    conn = get_connection()
    try:
        return _reconstruct_state_internal(conn, session_id)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Internal implementation
# ─────────────────────────────────────────────────────────────────────────────

def _write_constraint_internal(
    conn,
    session_id: str,
    key: str,
    value: Any,
    written_by_principal: str,
    principal_type: str,
    priority_level: Optional[int],
    valid_until: Optional[str],
    idempotency_key: Optional[str],
    signature: Optional[str],
    propagate_taint: bool,
) -> None:
    """Core write logic. Uses provided connection (caller controls transaction scope)."""

    # ── Session existence check ────────────────────────────────────────────────
    session_exists = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not session_exists:
        raise ValueError("Session does not exist")

    # ── Idempotency check ──────────────────────────────────────────────────────
    if idempotency_key:
        existing = conn.execute(
            "SELECT 1 FROM constraints WHERE idempotency_key = ? AND session_id = ?",
            (idempotency_key, session_id),
        ).fetchone()
        if existing:
            return  # Already written — no-op

    # ── Authority check ────────────────────────────────────────────────────────
    authorized_writers = CONSTRAINT_AUTHORITY_MAP.get(key)
    if authorized_writers is None:
        raise ValueError(f"Unknown constraint key '{key}'")
    if principal_type not in authorized_writers:
        raise ValueError(
            f"Principal type '{principal_type}' is not authorized to write "
            f"constraint key '{key}'. "
            f"Authorized writers: {authorized_writers}"
        )

    # ── Resolve priority ───────────────────────────────────────────────────────
    if priority_level is None:
        priority_level = _WRITER_PRIORITY.get(principal_type, PRIORITY_DEFAULT)

    # ── Check no higher-priority write already exists ──────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    highest_existing = conn.execute(
        """
        SELECT MAX(priority_level) as max_priority
        FROM constraints
        WHERE session_id = ?
          AND constraint_key = ?
          AND (valid_until IS NULL OR valid_until > ?)
        """,
        (session_id, key, now_iso),
    ).fetchone()

    existing_priority = highest_existing["max_priority"] if highest_existing else None

    if existing_priority is not None and existing_priority < priority_level:
        # Existing write has LOWER priority number = HIGHER authority
        # (priority 1 = PRIORITY_1 = highest; priority 2 = lower)
        raise ValueError(
            f"A higher-priority write (priority={existing_priority}) already exists "
            f"for key '{key}' in session '{session_id}'. "
            f"Current write (priority={priority_level}) cannot override it."
        )

    # ── Validate JSON serializability ─────────────────────────────────────────
    try:
        value_json = json.dumps(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Constraint value is not JSON-serializable: {e}") from e

    # ── Signature verification ─────────────────────────────────────────────────
    # DESIGN DECISION — Option A (clean system):
    #   HUMAN principals must sign constraint writes (external authority — must be verifiable).
    #   INTERCEPTOR writes are trusted system-internal state transitions — no external
    #   signature is possible or required (the interceptor IS the enforcement boundary).
    #   ORCHESTRATOR/AGENT/SUBAGENT/COMPLIANCE: signed via the interceptor auth pre-check.
    if principal_type == "HUMAN" and not signature:
        raise ValueError("Signature required for HUMAN constraint writes")
    if signature and principal_type == "HUMAN":
        from runtime.identity.principals import verify_signature
        message = f"{session_id}:{key}:{json.dumps(value, sort_keys=True)}".encode()
        if not verify_signature(written_by_principal, message, signature):
            raise ValueError("Invalid HUMAN signature on constraint write")

    # ── Get next version number ────────────────────────────────────────────────
    # NOTE: No manual BEGIN IMMEDIATE — get_connection() uses isolation_level=DEFERRED
    # which means Python's sqlite3 module auto-manages transactions. Issuing
    # BEGIN IMMEDIATE manually inside that context causes:
    #   OperationalError: cannot start a transaction within a transaction
    # The UNIQUE(session_id, version) constraint ensures monotonicity even under
    # concurrent writers — SQLite WAL serializes conflicting writes via locking.
    current_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM constraints WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    next_version = current_version + 1

    # ── Write the constraint row ───────────────────────────────────────────────
    conn.execute(
        """
        INSERT INTO constraints (
            session_id, constraint_key, constraint_value,
            written_by_principal, priority_level,
            valid_from, valid_until, idempotency_key,
            signature, version, set_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            key,
            value_json,
            written_by_principal,
            priority_level,
            now_iso,
            valid_until,
            idempotency_key,
            signature,
            next_version,
            now_iso,
        ),
    )

    # ── Snapshot check ─────────────────────────────────────────────────────────
    _snapshot_if_needed_internal(conn, session_id)

    # ── Upward taint propagation ───────────────────────────────────────────────
    # If the key is a taint key AND the value is truthy, propagate synchronously
    # to the parent session in the same logical operation.
    if propagate_taint and key in UPWARD_TAINT_KEYS and value:
        _propagate_taint_upward(conn, session_id, key, value, INTERCEPTOR_PRINCIPAL_ID, "INTERCEPTOR")

    # ── Single commit — main write + snapshot + taint propagation are atomic ───
    conn.commit()


def _propagate_taint_upward(
    conn,
    session_id: str,
    taint_key: str,
    taint_value: Any,
    written_by_principal: str,
    principal_type: str,
) -> None:
    """
    Propagate a taint flag upward through the session tree synchronously.

    When a child session sets pii_accessed=true or session_taint=true,
    the same flag is immediately written to the parent session, and
    recursively to the grandparent, and so on to the root.

    This ensures siblings become aware of taint at their next validate()
    call (they check parent session state), and new children spawned
    after the taint inherit it.

    Propagation uses the INTERCEPTOR principal type so it is always
    authorized to write taint keys at any level.

    Args:
        conn:                  Active database connection.
        session_id:            Session that triggered the taint.
        taint_key:             The taint constraint key (pii_accessed, session_taint).
        taint_value:           The taint value (True).
        written_by_principal:  Original principal that triggered the taint.
    """
    # Walk up the tree until we reach a session with no parent
    current_id = session_id
    depth = 0

    while True:
        parent_row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id = ?",
            (current_id,),
        ).fetchone()

        if parent_row is None or parent_row["parent_session_id"] is None:
            break  # Reached root or session not found

        parent_id = parent_row["parent_session_id"]
        depth += 1

        # Check if parent already has this taint set to true
        existing_taint = _read_constraint(conn, parent_id, taint_key)
        if existing_taint is True:
            break  # Already propagated — no need to continue upward

        # Check if parent session is still active enough to receive taint
        parent_session = conn.execute(
            "SELECT state FROM sessions WHERE session_id = ?",
            (parent_id,),
        ).fetchone()
        if parent_session is None or parent_session["state"] == "TERMINATED":
            break  # Cannot write to terminated session

        # Get next version for parent
        parent_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM constraints WHERE session_id = ?",
            (parent_id,),
        ).fetchone()[0]
        next_version = parent_version + 1
        now_iso = datetime.now(timezone.utc).isoformat()

        # Write taint to parent — INTERCEPTOR authority, PRIORITY_1
        conn.execute(
            """
            INSERT INTO constraints (
                session_id, constraint_key, constraint_value,
                written_by_principal, priority_level,
                valid_from, valid_until, idempotency_key,
                signature, version, set_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                parent_id,
                taint_key,
                json.dumps(taint_value),
                written_by_principal,
                PRIORITY_INTERCEPTOR,  # taint propagation is always PRIORITY_1
                now_iso,
                next_version,
                now_iso,
            ),
        )

        # Snapshot parent if needed
        _snapshot_if_needed_internal(conn, parent_id)

        # Continue upward
        current_id = parent_id


def _read_constraint(conn, session_id: str, key: str) -> Any:
    """
    Read the current effective constraint value using priority resolution.

    Priority resolution:
        - Rows with LOWER priority_level number have HIGHER authority
          (priority 1 beats priority 2)
        - Among equal-priority rows, most recent (set_at DESC) wins
        - Expired rows excluded
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    row = conn.execute(
        """
        SELECT constraint_value, priority_level, valid_until
        FROM constraints
        WHERE session_id = ?
          AND constraint_key = ?
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY priority_level ASC, set_at DESC
        LIMIT 1
        """,
        (session_id, key, now_iso),
    ).fetchone()

    if row is None:
        return CONSTRAINT_DEFAULTS.get(key, None)

    return json.loads(row["constraint_value"])


def _snapshot_if_needed_internal(conn, session_id: str) -> None:
    """
    Write a snapshot if total constraint rows >= last_snapshot + SNAPSHOT_INTERVAL.
    """
    # Count total rows for this session
    total_rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM constraints WHERE session_id = ?",
        (session_id,),
    ).fetchone()["cnt"]

    if total_rows == 0:
        return

    # Get the version of the last snapshot
    last_snapshot = conn.execute(
        """
        SELECT snapshot_at_version
        FROM constraint_snapshots
        WHERE session_id = ?
        ORDER BY snapshot_at_version DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    last_snapshot_version = last_snapshot["snapshot_at_version"] if last_snapshot else 0

    # Rows written since last snapshot
    rows_since_snapshot = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM constraints
        WHERE session_id = ? AND version > ?
        """,
        (session_id, last_snapshot_version),
    ).fetchone()["cnt"]

    if rows_since_snapshot < SNAPSHOT_INTERVAL:
        return

    # Build full state snapshot
    full_state = _reconstruct_state_internal(conn, session_id)
    current_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM constraints WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]

    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO constraint_snapshots
            (session_id, snapshot_at_version, full_state, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, current_version, json.dumps(full_state), now_iso),
    )


def _reconstruct_state_internal(conn, session_id: str) -> dict:
    """
    Reconstruct complete constraint state from latest snapshot + replay.

    1. Find latest snapshot
    2. Start with snapshot state
    3. Replay all writes after snapshot_at_version
    4. Apply priority resolution: higher authority (lower priority number) wins
    5. Exclude expired constraints
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Step 1: Find latest snapshot
    snapshot_row = conn.execute(
        """
        SELECT snapshot_at_version, full_state
        FROM constraint_snapshots
        WHERE session_id = ?
        ORDER BY snapshot_at_version DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()

    if snapshot_row:
        base_state = json.loads(snapshot_row["full_state"])
        base_version = snapshot_row["snapshot_at_version"]
    else:
        base_state = {}
        base_version = 0

    # Step 2: Replay writes after base_version
    # Fetch all rows after base version ordered by version ASC
    # Then apply priority resolution: for each key, highest-authority
    # (lowest priority_level), then most-recent wins
    rows = conn.execute(
        """
        SELECT constraint_key, constraint_value, priority_level, valid_until, set_at
        FROM constraints
        WHERE session_id = ?
          AND version > ?
          AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY priority_level ASC, set_at DESC
        """,
        (session_id, base_version, now_iso),
    ).fetchall()

    # Apply priority resolution for replay rows
    # Track: key -> (priority_level, set_at, value)
    # Lower priority_level number = higher authority
    replay_state: dict[str, tuple[int, str, Any]] = {}

    for row in rows:
        k = row["constraint_key"]
        p = row["priority_level"]
        t = row["set_at"]
        v = json.loads(row["constraint_value"])

        if k not in replay_state:
            replay_state[k] = (p, t, v)
        else:
            existing_p, existing_t, _ = replay_state[k]
            # Lower priority number = higher authority
            if p < existing_p:
                replay_state[k] = (p, t, v)
            elif p == existing_p and t > existing_t:
                replay_state[k] = (p, t, v)
            # else: existing row wins — do nothing

    # Merge replay into base state
    final_state = dict(base_state)
    for k, (p, t, v) in replay_state.items():
        final_state[k] = v

    return final_state
# Architecture (Detailed)

This document covers the internal design of every component. For the high-level overview see `ARCHITECTURE.md` at the repo root.

---

## Interceptor (`runtime/interceptor/validate.py`)

### Responsibility

The single enforcement function. Every agent tool call passes through `validate()`. The interceptor reads session state and constraint state directly — it never trusts anything the agent passes in.

### Check Order

Checks execute in fixed order. The first failure short-circuits — later checks do not run.

```
Check 1 — Session existence
  Session ID must exist in the sessions table.
  Failure: BLOCKED, reason="session_not_found"

Check 2 — Session validity
  session.state must be ACTIVE.
  session.frozen must be 0.
  session.expires_at must be in the future.
  Failure: BLOCKED, reason="session_expired|session_frozen|session_inactive"

Check 3 — Identity continuity
  claimed_principal must match session.principal_id.
  claimed_principal_type must match session.principal_type.
  Failure: BLOCKED, reason="identity_mismatch"

Check 4 — Constraint version freshness
  caller_constraint_version must be ≥ session's last known version.
  Prevents stale-read attacks where agent acts on outdated state.
  Failure: BLOCKED, reason="stale_constraint_version"

Check 5 — Re-authentication gate
  If action is in SENSITIVE_ACTIONS and reauth_verified=False in store:
  Failure: BLOCKED, reason="reauth_required"

Check 6 — Dynamic constraints
  6a. Budget: budget_spent + requested_amount ≤ budget_limit
      If amount > PENDING_BUDGET_THRESHOLD: return PENDING
      If amount > remaining: BLOCKED
  6b. Taint: if pii_accessed=True and action in PII_TAINT_BLOCKED_ACTIONS: BLOCKED
  6c. Capability: action must be in session.granted_capabilities
  6d. Cross-session: principal_ledger total + amount ≤ principal_budget_limit

Check 7 — Idempotency
  If idempotency_key provided and key already in execution_log: return cached result
  Prevents duplicate execution on network retry.

Check 7.5 — Rate limit + loop detection (v2)
  Count calls to this tool in last RATE_LIMIT_WINDOW seconds.
  If count ≥ RATE_LIMIT_MAX: BLOCKED, reason="rate_limit"
  Count BLOCKED results for same (tool, action) in last 5 minutes.
  If count ≥ 3: return ESCALATE, reason="repeated_block:{tool}/{action}"
```

### Post-Approval Triggers

Executed only when result is ALLOWED, inside the same database transaction:

- Write `budget_spent` update to constraint store
- Set `pii_accessed=True` if action is in PII_TRIGGER_ACTIONS
- Set `reauth_verified=False` if action consumes reauth gate
- Propagate taint to parent sessions
- Cascade freeze if action triggers cascade
- Write heartbeat progress marker (v2)

### Audit Log Write

Written for every decision — ALLOWED, BLOCKED, PENDING, ESCALATE — in the same transaction as the decision. Failure to write the audit log causes the entire transaction to roll back. The decision is not returned to the caller if it cannot be recorded.

---

## Constraint Store (`runtime/constraints/store.py`)

### Append-Only Design

No UPDATE or DELETE statements exist in this module. Every constraint write is an INSERT. Current value resolution reads the highest-priority, most-recent, non-expired row for each `(session_id, constraint_key)` pair.

### Budget Write — Full Transaction Sequence

```python
acquire _budget_lock
  open WAL transaction
    read current budget_spent (consistent snapshot)
    read budget_limit
    if current + amount > limit:
      INSERT execution_log (BLOCKED)
      commit
      release lock
      return BLOCKED
    INSERT constraints (budget_spent = current + amount)
    UPDATE principal_ledger (running total)
    INSERT execution_log (ALLOWED)
    commit
  release lock
```

If any step fails, the transaction rolls back. The lock is released in a `finally` block — it cannot be held indefinitely.

### Taint Propagation

When `pii_accessed=True` is written for a session, the store walks the session tree upward and writes `pii_accessed=True` for every ancestor session in the same transaction. Taint is synchronous and immediate — it does not propagate asynchronously.

### Constraint Version

A monotonic version counter increments on every constraint write per session. The interceptor's Check 4 reads this version. If the agent presents a version lower than the current store version, the request is blocked — the agent is acting on stale state.

---

## Session Tree (`runtime/session/sessions.py`)

### Spawn Validation

`create_session()` enforces three attenuation invariants before inserting:

```python
# 1. Capability attenuation
for cap in child_capabilities:
    if cap not in parent_effective_capabilities:
        raise ValueError(f"capability escalation: {cap} not held by parent")

# 2. Budget attenuation
if child_budget_grant > parent_remaining_budget:
    raise ValueError(f"budget escalation: {child_budget_grant} > {parent_remaining}")

# 3. Depth attenuation
child_depth = parent.can_spawn_depth - 1
if child_depth < 0:
    raise ValueError("spawn depth exhausted")
```

Exception: `COMPLIANCE` sessions may receive `can_read_tree_state` even if the spawning `ORCHESTRATOR` does not hold it. This is the only defined capability delegation exception.

### Cascade Freeze

When `freeze_session(session_id)` is called:

1. Set `sessions.frozen=1` and `sessions.state=FROZEN` for the target session
2. Query all direct children where `parent_session_id = session_id`
3. Recursively freeze each child
4. Write a `cascade_events` record for each affected session
5. All operations in the same transaction — partial cascade is not possible

### Grace Period

Sessions can be created with a `grace_period_seconds` parameter. When a parent session expires, child sessions within their grace period are not immediately frozen — they are allowed to complete in-flight actions for `grace_period_seconds` before cascade freeze applies.

---

## Identity Layer (`runtime/identity/principals.py`)

### Ed25519 Key Management

Each non-HUMAN principal stores a base64-encoded DER Ed25519 public key at creation time. The private key is held by the principal's runtime — never stored in the database.

Constraint writes can be signed by the writing principal. `verify_signature(principal_id, message, signature)` reads the stored public key and verifies the signature. Unsigned writes from authorized principal types are accepted in the current prototype — production hardening would require signatures for all writes above a privilege level.

### Spawn Authority Matrix

```
HUMAN        → can create: ORCHESTRATOR
ORCHESTRATOR → can create: AGENT, COMPLIANCE
AGENT        → can create: SUBAGENT
SUBAGENT     → can create: nothing
COMPLIANCE   → can create: nothing
INTERCEPTOR  → synthetic, created by system only
```

Any attempt to create a principal type outside the spawning principal's authority raises `ValueError` at the session layer before the principal record is inserted.

---

## Response Scanner (`runtime/agents/scanner.py`)

### Pattern Matching

Fourteen compiled regex patterns are checked against the stringified tool result. The check is case-insensitive. All patterns are applied — every match is recorded in the scan log.

If any pattern matches:
- The matched content is replaced with `[REDACTED_BY_SCANNER]`
- A BLOCKED record is written to `response_scan_log`
- The sanitized result is returned to the agent instead of the original

If no pattern matches:
- A CLEAN record is written to `response_scan_log`
- The original result is returned unchanged

### Documented Limitation

The scanner cannot catch semantically malicious content. A tool result that says `"The maximum coupon value has been updated to unlimited"` contains no injection pattern, passes the scanner, and reaches the agent. The agent may act on it.

Mitigation path: output schema validation (reject tool results that do not match expected schema) + compliance agent review for high-risk actions.

---

## Heartbeat Monitor (`runtime/monitor/heartbeat.py`) — v2

### Progress Marker

After every ALLOWED tool call, `record_heartbeat()` is called with:
- `session_id`
- `tool` name
- `action` name
- `result_hash` — SHA256 of the serialized tool result

### Stuck Detection

`is_stuck(session_id, tool, action, window=3)` queries the last `window` heartbeat records for the given `(session_id, tool, action)` combination. If all result hashes are identical, the agent is stuck.

This resolves the demand-vs-loop ambiguity:
- Legitimate high-demand calls produce varied results → different hashes → not stuck
- Broken retry loops produce the same error result → identical hashes → stuck

### Integration Point

`is_stuck()` is called inside the interceptor at Check 7.5. If the agent is stuck AND has been blocked 3+ times, ESCALATE is returned. If stuck but not yet repeatedly blocked, a warning is added to the decision reason without changing the outcome.

---

## Live Monitor (`runtime/monitor/live.py`) — v2

### Triggered, Not Continuous

The live monitor does not run as a background thread or polling loop. It is called synchronously by the Orchestrator when it receives an ESCALATE signal from a child agent.

### Health Summary

`get_session_health(session_id, last_n=20)` reads the last N execution log entries for the session and returns:

```python
{
    "session_id": "...",
    "recent_blocked": 14,
    "recent_allowed": 2,
    "last_progress": "2026-05-06T02:47:00+00:00",
    "health": "STUCK"   # or "OK"
}
```

`health="STUCK"` when `recent_blocked > recent_allowed * 2`.

### Orchestrator Decision Flow

```
ESCALATE received
  → call get_session_health(child_session_id)
  → health="STUCK" and reason="repeated_block:scrape_tool/fetch_coupons"
    → reassign task to sibling Data Agent
  → health="STUCK" and reason="budget_exhausted"
    → cannot recover → alert human
  → health="OK" (transient issue)
    → allow agent to retry
```

---

## Eval Runner (`eval/runner.py`) — v2

### Execution

```bash
python eval/runner.py
```

Runs all 10 assertions in sequence against the current database state. Each assertion prints:
```
✅ PASS  budget_never_exceeded           spent=300.0, limit=500.0
✅ PASS  audit_chain_intact              chain intact (47 entries)
❌ FAIL  escalate_fires_on_loop          result=BLOCKED, expected=ESCALATE
```

Exit code 0 if all pass. Exit code 1 if any fail. Suitable for CI integration.

### Regression Detection

When a model is swapped or a prompt is rewritten, re-run `eval/runner.py` immediately. Any change in agent behavior that affects governance outcomes will surface as a FAIL. This is the difference between an eval and a vibe check.

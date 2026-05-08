# Data Model

## Overview

Three core tables form the enforcement backbone. Two supporting tables handle approvals and scanning. Three v2 tables extend the system with progress tracking, cascade events, and rate limiting.

All tables are append-only except `sessions` (lifecycle state), `pending_approvals` (status updates), and `principal_ledger` (running totals).

---

## Core Tables

### `principals`

Every entity in the system. Type is immutable after creation.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT (UUID) | Primary key |
| `principal_type` | TEXT | HUMAN / ORCHESTRATOR / AGENT / SUBAGENT / COMPLIANCE / INTERCEPTOR |
| `public_key` | TEXT | Base64-encoded DER Ed25519 public key. Empty for HUMAN. |
| `created_at` | TEXT | ISO8601 UTC |

**Constraint:** `principal_type` in `{HUMAN, ORCHESTRATOR, AGENT, SUBAGENT, COMPLIANCE, INTERCEPTOR}`

---

### `sessions`

One row per session. Forms a tree via `parent_session_id`. Lifecycle state is the only mutable field.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT (UUID) | Primary key |
| `principal_id` | TEXT | FK → principals.id |
| `principal_type` | TEXT | Denormalized for fast interceptor reads |
| `parent_session_id` | TEXT | NULL for root sessions |
| `granted_capabilities` | TEXT | JSON array of capability strings |
| `can_spawn_depth` | INTEGER | How many levels of child spawning remain |
| `budget_grant` | REAL | Budget allocated at spawn time |
| `grace_period_seconds` | INTEGER | Window for in-flight actions after parent expires |
| `autonomous_flag` | INTEGER | 1 = session continues after parent task completes |
| `expires_at` | TEXT | ISO8601 UTC |
| `active` | INTEGER | 1 or 0 |
| `frozen` | INTEGER | 1 or 0 |
| `state` | TEXT | ACTIVE / FROZEN / REVOKED / TERMINATED / DEGRADED |
| `created_at` | TEXT | ISO8601 UTC |

**Lifecycle state machine:**
```
ACTIVE → FROZEN → TERMINATED
ACTIVE → REVOKED → TERMINATED
ACTIVE → DEGRADED  (v2, when child escalates)
```

---

### `constraints`

The append-only state store. Never updated. Never deleted. Current value = highest-priority, most-recent, non-expired row for each `(session_id, constraint_key)` pair.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | FK → sessions.session_id |
| `constraint_key` | TEXT | budget_spent / pii_accessed / reauth_verified / etc |
| `constraint_value` | TEXT | JSON-encoded Python value |
| `written_by_principal` | TEXT | FK → principals.id |
| `priority_level` | INTEGER | 1 = highest (HUMAN/INTERCEPTOR), 2 = lower |
| `valid_from` | TEXT | ISO8601 UTC |
| `valid_until` | TEXT | ISO8601 UTC or NULL (no expiry) |
| `idempotency_key` | TEXT | Deduplication key, NULL if not provided |
| `signature` | TEXT | Base64 Ed25519 signature or NULL |
| `version` | INTEGER | Monotonic counter per session |
| `set_at` | TEXT | ISO8601 UTC |

**Priority resolution:**
```
1. Lower priority_level number = higher authority (1 beats 2)
2. Among equal-priority rows: most recent set_at wins
3. Expired rows (valid_until < now) excluded
```

**Defined constraint keys:**

| Key | Default | Written By | Meaning |
|---|---|---|---|
| `budget_spent` | 0.0 | INTERCEPTOR | Running spend total |
| `budget_limit` | NULL | HUMAN | Maximum allowed spend |
| `pii_accessed` | False | INTERCEPTOR | PII taint flag |
| `reauth_verified` | False | INTERCEPTOR | Reauth gate status (TTL: 15 min) |
| `session_taint` | False | INTERCEPTOR | General taint propagation |
| `can_spawn_depth` | NULL | ORCHESTRATOR, HUMAN | Remaining spawn depth |
| `budget_grant` | NULL | ORCHESTRATOR | Budget allocated to session |
| `principal_budget_limit` | NULL | HUMAN | Cross-session spend ceiling |

---

### `execution_log`

Append-only audit trail. Every interceptor decision is recorded here — ALLOWED, BLOCKED, PENDING, and ESCALATE. SHA256 hash chain links entries sequentially. Any modification breaks chain verification.

| Column | Type | Notes |
|---|---|---|
| `action_id` | TEXT (UUID) | Primary key |
| `parent_action_id` | TEXT | UUID of the action that triggered this one, or NULL |
| `session_id` | TEXT | FK → sessions.session_id |
| `principal_id` | TEXT | FK → principals.id |
| `tool` | TEXT | Tool name |
| `action` | TEXT | Action name |
| `result` | TEXT | ALLOWED / BLOCKED / PENDING / ESCALATE |
| `reason` | TEXT | Human-readable explanation |
| `constraint_version` | INTEGER | State store version at decision time |
| `timestamp` | TEXT | ISO8601 UTC |
| `prev_hash` | TEXT | SHA256 of previous entry (or zeros for first) |
| `entry_hash` | TEXT | SHA256 of this entry including prev_hash |

**Chain verification:**
```python
verify_audit_chain(session_id) → (True, "chain intact") | (False, reason)
```

---

## Supporting Tables

### `pending_approvals`

Records actions awaiting human approval. Status transitions from PENDING to APPROVED, REJECTED, or TIMEOUT.

| Column | Type | Notes |
|---|---|---|
| `approval_id` | TEXT (UUID) | Primary key |
| `action_id` | TEXT | FK → execution_log.action_id |
| `session_id` | TEXT | FK → sessions.session_id |
| `tool` | TEXT | |
| `action` | TEXT | |
| `metadata` | TEXT | JSON of action parameters |
| `requested_at` | TEXT | ISO8601 UTC |
| `expires_at` | TEXT | ISO8601 UTC (default 5 minutes) |
| `status` | TEXT | PENDING / APPROVED / REJECTED / TIMEOUT |
| `decided_at` | TEXT | ISO8601 UTC or NULL |
| `decided_by` | TEXT | Principal ID of human approver or NULL |

---

### `response_scan_log`

One record per tool result scanned. Tracks injection detection outcomes.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | |
| `tool` | TEXT | |
| `scan_result` | TEXT | CLEAN / BLOCKED |
| `pattern_matched` | TEXT | Comma-separated pattern names or NULL |
| `sanitized` | INTEGER | 1 if content was replaced, 0 if clean |
| `timestamp` | TEXT | ISO8601 UTC |

---

### `principal_ledger`

Cross-session budget tracking per principal. Tracks cumulative spend regardless of which session it occurred in.

| Column | Type | Notes |
|---|---|---|
| `principal_id` | TEXT | FK → principals.id |
| `metric_key` | TEXT | Currently only `budget_spent` |
| `current_value` | REAL | Running total — updated atomically inside budget_lock |
| `last_updated` | TEXT | ISO8601 UTC |
| `version` | INTEGER | Monotonic counter |

---

## v2 Tables

### `heartbeats`

Progress markers recorded after every ALLOWED tool call. Used by `is_stuck()` to detect agents with no forward progress.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | |
| `tool` | TEXT | |
| `action` | TEXT | |
| `result_hash` | TEXT | SHA256 of the tool result |
| `timestamp` | TEXT | ISO8601 UTC |

**Stuck detection:** If the last N result hashes for `(session_id, tool, action)` are all identical, the agent is stuck regardless of cause.

---

### `cascade_events`

Records freeze/revoke propagation through the session tree for audit purposes.

| Column | Type | Notes |
|---|---|---|
| `trigger_session_id` | TEXT | Session that caused the cascade |
| `affected_session_id` | TEXT | Session that was frozen/revoked as a result |
| `event_type` | TEXT | CASCADE_FREEZE / CASCADE_REVOKE |
| `propagation_depth` | INTEGER | How many levels deep this event traveled |
| `timestamp` | TEXT | ISO8601 UTC |

---

### `constraint_snapshots`

Periodic snapshots of full constraint state per session. Written every 100 constraint rows to avoid full replay costs at scale.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT | |
| `snapshot_at_version` | INTEGER | Constraint version when snapshot was taken |
| `full_state` | TEXT | JSON dict of all current constraint values |
| `created_at` | TEXT | ISO8601 UTC |

**Reconstruction:** Latest snapshot + replay of all writes after `snapshot_at_version` gives current state without full table scan.

---

## Transaction Boundaries

**Budget enforcement** is the only operation that requires explicit serialization beyond SQLite's default isolation:

```python
with _budget_lock:         # threading.Lock — serializes concurrent budget writes
    conn = get_connection()
    with conn:             # SQLite WAL transaction
        current = read budget_spent
        if current + amount > limit:
            BLOCK
        else:
            write budget_spent = current + amount
            write execution_log entry
            ALLOW
```

All other constraint writes use standard SQLite WAL isolation — sufficient because they are not subject to read-modify-write race conditions.

---

## Why Eventual Consistency Breaks This System

See `docs/consistency_model.md` for the full argument. In summary:

Budget enforcement requires that the read of `budget_spent` and the write of the new value are atomic. With eventual consistency, two concurrent agents can both read stale state, both pass the budget check, and both execute — resulting in total spend exceeding the limit with no violation recorded.

SQLite WAL provides serializable writes for this prototype. Production scale requires Spanner (TrueTime-backed external consistency) or equivalent.

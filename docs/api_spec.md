# API Specification

All endpoints are served by `api_v2.py` via FastAPI at `http://localhost:8000`.  
Interactive docs available at `http://localhost:8000/docs`.

---

## Demo Control

### `POST /demo/reset`

Resets the entire demo state. Clears all sessions, constraints, and execution log. Re-runs `setup_demo()` to initialize the five-agent session tree.

**Request:** No body required.

**Response:**
```json
{
  "ok": true,
  "message": "Demo reset. All 5 agents ready."
}
```

---

### `POST /demo/event/{n}`

Runs a single governance event. Events are numbered 1-15. Each event exercises a specific governance scenario and returns structured pass/fail data.

**Path parameter:** `n` — integer 1 to 15

**Response:**
```json
{
  "n": 10,
  "name": "Concurrent Budget Race",
  "result": "PASS",
  "note": "WAL lock held. One ALLOWED, one BLOCKED.",
  "steps": [
    {"type": "decision", "result": "ALLOWED", "reason": "budget within limit"},
    {"type": "decision", "result": "BLOCKED", "reason": "budget_exceeded: 300+300>500"},
    {"type": "detail", "label": "budget_spent", "value": "300.0"}
  ],
  "error": null
}
```

**Error:** 400 if demo not initialized. 400 if n is out of range.

---

## State Inspection

### `GET /demo/state`

Returns the current session tree with per-session constraint state. Useful for watching state evolve as events run.

**Response:**
```json
{
  "sessions": [
    {
      "session_id": "uuid",
      "short_id": "abcd1234",
      "principal_id": "uuid",
      "principal_type": "ORCHESTRATOR",
      "parent_session_id": "uuid",
      "granted_capabilities": ["can_query_records", "can_spawn"],
      "can_spawn_depth": 2,
      "budget_grant": 500.0,
      "budget_spent": 300.0,
      "state": "ACTIVE",
      "frozen": false,
      "pii_accessed": false,
      "reauth_verified": false,
      "version": 14,
      "expires_at": "2026-05-06T12:00:00+00:00"
    }
  ]
}
```

---

### `GET /demo/audit-log`

Returns the full execution log with hash chain verification results per session.

**Response:**
```json
{
  "log": [
    {
      "action_id": "uuid",
      "parent_action_id": null,
      "session_id": "uuid",
      "principal_id": "uuid",
      "tool": "database",
      "action": "query_records",
      "result": "ALLOWED",
      "reason": "all checks passed",
      "constraint_version": 3,
      "timestamp": "2026-05-06T10:00:00+00:00",
      "prev_hash": "0000...0000",
      "entry_hash": "sha256hash"
    }
  ],
  "total": 47,
  "chain_verification": {
    "abcd1234": {"intact": true, "message": "chain intact"},
    "efgh5678": {"intact": true, "message": "chain intact"}
  }
}
```

---

### `GET /demo/scan-log`

Returns the last 200 response scanner records. Shows which tool results were clean and which were blocked for injection patterns.

**Response:**
```json
{
  "scans": [
    {
      "session_id": "uuid",
      "tool": "scrape_tool",
      "scan_result": "BLOCKED",
      "pattern_matched": "system_prefix,reauth_inject",
      "sanitized": 1,
      "timestamp": "2026-05-06T10:00:00+00:00"
    }
  ],
  "total": 23
}
```

---

### `GET /demo/pending`

Returns all pending approval records with their current status.

**Response:**
```json
{
  "approvals": [
    {
      "approval_id": "uuid",
      "action_id": "uuid",
      "session_id": "uuid",
      "tool": "budget_spend",
      "action": "process_payment",
      "metadata": "{\"amount\": 250.0}",
      "requested_at": "2026-05-06T10:00:00+00:00",
      "expires_at": "2026-05-06T10:05:00+00:00",
      "status": "PENDING",
      "decided_at": null
    }
  ]
}
```

---

## Approval Actions

### `POST /demo/pending/{approval_id}/approve`

Human approves a pending action. Only works if status is currently PENDING and the approval has not expired.

**Request:**
```json
{"decided_by": "human-principal-uuid"}
```

**Response:**
```json
{"ok": true, "status": "APPROVED"}
```

---

### `POST /demo/pending/{approval_id}/reject`

Human rejects a pending action.

**Request:**
```json
{"decided_by": "human-principal-uuid"}
```

**Response:**
```json
{"ok": true, "status": "REJECTED"}
```

---

## Advanced

### `GET /demo/cascade-events`

Returns the last 100 cascade freeze/revoke events — when a parent session was frozen or revoked and the effect propagated to child sessions.

**Response:**
```json
{
  "events": [
    {
      "trigger_session_id": "uuid",
      "affected_session_id": "uuid",
      "event_type": "CASCADE_FREEZE",
      "propagation_depth": 2,
      "timestamp": "2026-05-06T10:00:00+00:00"
    }
  ]
}
```

---

## Core Enforcement Function

`validate()` in `runtime/interceptor/validate.py` is the internal function called by every agent. It is not exposed as an HTTP endpoint but is the foundation of every demo event.

**Signature:**
```python
def validate(
    session_id: str,
    claimed_principal: str,
    claimed_principal_type: str,
    tool: str,
    action: str,
    metadata: dict = None,
    idempotency_key: str = None,
    caller_constraint_version: int = 0,
    parent_action_id: str = None,
) -> InterceptorDecision
```

**Returns:** `InterceptorDecision` dataclass with fields:

| Field | Type | Notes |
|---|---|---|
| `allowed` | bool | True only for ALLOWED |
| `result` | str | ALLOWED / BLOCKED / PENDING / ESCALATE |
| `reason` | str | Human-readable explanation |
| `tool` | str | |
| `action` | str | |
| `timestamp` | str | ISO8601 UTC |
| `action_id` | str | UUID for audit trail linking |
| `constraint_version_at_decision` | int | State version when decision was made |
| `approval_id` | str or None | Set only for PENDING |
| `capability_token` | str or None | HMAC token for ALLOWED capability actions |
| `scanner_required` | bool | Always True — tool layer must scan response |

---

## Important Notes

### Numeric values in the API explorer

When setting constraints via the Swagger UI at `/docs`, always send numeric values as numbers, not strings:

```json
{"key": "budget_limit", "value": 500}    ✓ correct
{"key": "budget_limit", "value": "500"}  ✗ breaks budget math silently
```

Same rule for booleans: use `true` not `"true"`.

### Database persists between runs

The database (`governance_v2_demo.db`) persists between normal server restarts. To start from a clean state:

```bash
rm governance_v2_demo.db
python api_v2.py
```

Or use `POST /demo/reset` to reset state without restarting the server.

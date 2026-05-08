# Architecture

## Overview

The governance runtime sits between LLM agents and every external tool they can call. The system has four logical layers:

```
Agent (LLM reasoning)
        ↓
Interceptor (enforcement boundary)
        ↓
Constraint Store (session state)
        ↓
External Tools (DB / APIs / external services)
```

The agent proposes. The interceptor decides. The constraint store holds truth. The agent cannot bypass this path.

---

## Components

### 1. Interceptor (`runtime/interceptor/validate.py`)

The single enforcement function. Every agent tool call passes through `validate()`. No tool is reachable without passing all seven checks in order.

**Seven checks — fixed order, first failure blocks immediately:**

| Check | What It Validates |
|---|---|
| 1 | Session existence — session_id is real |
| 2 | Session validity — active, not expired, not frozen |
| 3 | Identity continuity — principal + type match session record |
| 4 | Constraint version freshness — read-your-writes consistency |
| 5 | Re-authentication gate — sensitive actions require reauth |
| 6 | Dynamic constraints — budget, taint, capability |
| 7 | Idempotency — deduplication on retry |
| 7.5 | Rate limit + loop detection (v2) |

**Four outcomes:**

- `ALLOWED` — all checks pass, triggers execute, audit log written
- `BLOCKED` — a check failed, tool never contacted, audit log written
- `PENDING` — action requires human approval, approval record written
- `ESCALATE` — systemic failure detected, Orchestrator notified (v2)

**ESCALATE fires when:**
- Same tool blocked 3+ times in 5 minutes (loop detected)
- Budget burn rate exceeds 3x baseline in 10 minutes
- Session has no completed actions in 15 minutes but budget is draining

---

### 2. Constraint Store (`runtime/constraints/store.py`)

The single source of truth for all session state. Design invariants:

- **Append-only** — rows are never updated or deleted after insert
- **Authority-controlled** — only authorized principal types may write each key
- **Priority-resolved** — higher priority writes shadow lower priority writes
- **Version-tracked** — monotonic counter increments on every write per session
- **Taint-propagating** — `pii_accessed` and `session_taint` propagate upward synchronously in the same transaction

**Constraint authority map:**

| Key | Who Can Write |
|---|---|
| `budget_limit` | HUMAN |
| `budget_spent` | INTERCEPTOR |
| `pii_accessed` | INTERCEPTOR |
| `reauth_verified` | INTERCEPTOR |
| `session_taint` | INTERCEPTOR |
| `can_spawn_depth` | ORCHESTRATOR, HUMAN |
| `budget_grant` | ORCHESTRATOR |

**Budget enforcement under concurrency:**

SQLite WAL mode + `threading.Lock` serializes all budget read-modify-write operations. Two agents requesting ₹300 each against a ₹500 limit: one reads `budget_spent=0`, passes, writes `budget_spent=300`. The second reads `budget_spent=300`, fails the check, is blocked. The budget invariant holds deterministically under true concurrency.

---

### 3. Session Tree (`runtime/session/sessions.py`)

Sessions form a tree rooted at the human-authorized root session. Authority flows downward only through explicit attenuation.

**Three attenuation invariants — enforced at every spawn:**

```
1. child.granted_capabilities ⊆ parent.effective_capabilities
2. child.budget_grant ≤ parent.remaining_budget
3. child.can_spawn_depth = parent.can_spawn_depth - 1
```

**Session lifecycle:**

```
ACTIVE → FROZEN → TERMINATED
ACTIVE → REVOKED → TERMINATED
ACTIVE → DEGRADED (v2, when child escalates)
```

**Hierarchy:**

```
HUMAN (root)
  └── ORCHESTRATOR
        ├── AGENT (Data)
        ├── AGENT (Report)
        ├── COMPLIANCE
        └── SUBAGENT (spawned dynamically)
```

---

### 4. Identity Layer (`runtime/identity/principals.py`)

Every entity in the system is a principal. Principal type is immutable after creation. Ed25519 public key stored for constraint write signature verification.

**Principal types:**

| Type | Created By | Can Create |
|---|---|---|
| HUMAN | External | ORCHESTRATOR |
| ORCHESTRATOR | HUMAN | AGENT, COMPLIANCE |
| AGENT | ORCHESTRATOR | SUBAGENT |
| SUBAGENT | AGENT | None |
| COMPLIANCE | ORCHESTRATOR | None |
| INTERCEPTOR | System | None (synthetic) |

---

### 5. Response Scanner (`runtime/agents/scanner.py`)

Scans every tool result before it is returned to the agent. Detects prompt injection patterns embedded in data returned by tools.

**What it catches (syntactic):**
- Explicit instruction format content: `system:`, `ignore previous`
- Constraint manipulation: `reauth_verified=true`, `budget_limit=0`
- Role-switching attempts: `you are now`, `forget your constraints`
- Shell injection patterns in data fields
- Authorization claim patterns: `you are now authorized`

**What it does not catch (documented limitation):**
Semantically correct but maliciously framed data that influences agent reasoning without containing explicit instruction patterns. Unsolvable at the syntactic scanning layer.

---

### 6. Heartbeat Monitor (`runtime/monitor/heartbeat.py`) — v2

Records a progress marker after every `ALLOWED` tool call. Tracks result hashes over time.

**Progress detection logic:**
- Legitimate demand: different result hashes across calls → forward progress
- Broken loop: same result hash 3 times in a row → stuck, regardless of cause

This resolves the demand-vs-loop ambiguity without trying to judge intent. The system judges output, not intent.

---

### 7. Live Monitor (`runtime/monitor/live.py`) — v2

Reads the last N audit entries and produces a health summary for the Orchestrator. **Not continuous. Triggered only when ESCALATE fires.** The Orchestrator calls this to understand why a child is struggling, then decides how to recover.

---

### 8. Audit Log (`execution_log` table)

SHA256 hash chain. Every entry contains a hash of the previous entry. Any modification, deletion, or insertion into the middle of the chain breaks verification. `verify_audit_chain(session_id)` replays the full chain and returns `(True, "chain intact")` or `(False, reason)`.

---

## New in v2

### State Grounding

Before every reasoning step, the agent receives a verified state summary from the constraint store injected into its prompt. Prevents context collapse: even if the agent forgets step 1 at step 30, infrastructure reminds it of verified state before every decision.

```
[GOVERNANCE STATE] budget_remaining=200.00, pii_accessed=True, reauth_verified=False
```

Overhead: ~200 tokens per step. LLM API call: 800-2000ms. State read: 2ms. Invisible in the latency budget.

### Managed Healing

When a child agent sends ESCALATE, the Orchestrator decides recovery — not the human:

1. Reassign task to a sibling agent
2. Spawn a fresh agent for the failed subtask
3. Accept partial results and continue
4. Escalate to human only as last resort

The Orchestrator already has spawn capability, session tree access, and budget authority. Managed healing is its second job.

### Rate Limiting

Per-agent, per-tool rate limit enforced inside `validate.py`. Default: 10 calls per 60-second window. Configurable per session at creation time.

---

## Design Decisions

### Why SQLite instead of PostgreSQL?

SQLite WAL mode provides serializable writes sufficient for the concurrency requirements of this prototype. The consistency model document explains why eventual consistency breaks this system and what a production migration to Spanner would look like.

### Why append-only constraints?

Mutation makes audit reconstruction ambiguous. Append-only means every state the system has ever been in can be replayed from the constraint table. Snapshots every 100 rows prevent full replay costs at scale.

### Why store state outside the agent?

Context collapse is a prompting problem only if you store state in the context. Moving state to infrastructure means the agent's memory is irrelevant to enforcement. Step 30 and step 1 are enforced identically.

### Why intercept at the tool call boundary?

The tool call is the only point where agent reasoning becomes an external side effect. Every other interception point — prompt, output, model weights — is either too early or too late. Tool call interception is the narrowest correct boundary.

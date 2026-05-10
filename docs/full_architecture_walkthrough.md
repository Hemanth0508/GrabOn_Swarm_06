# GrabOn Swarm Runtime
## Assignment 06 — The Swarm · Stateful Multi-Agent Governance Runtime

> *"Build something that runs while you sleep. Break something interesting along the way. Tell us about both."*

This system governs what agents can **do** given what has **already happened** — stateful policy enforcement, not per-call validation.

---

## Why I Chose This Assignment

The Swarm is the hardest assignment because the difficulty is not writing agents. Writing agents is easy. The difficulty is *governing* them: what happens when Agent 2 accesses salary data in step 2, the context window fills, and at step 30 it tries to post that data to Slack? A stateless system has no memory of step 2. A naive stateful system has a race condition. A production system has neither problem.

I chose Assignment 06 because the core question — *how do you enforce constraints across time, across agents, and across concurrent requests?* — is the same question that determines whether an agentic system is deployable in a real enterprise. I wanted to answer it properly.

---

## Demo

> 📺 **Loom Walkthrough:** `[INSERT LOOM LINK]`
> Covers: happy path end-to-end (Event 19), failure recovery (Events 17+20), eval run (11/11), architecture walkthrough, provider failover live demo.

> 🌐 **Web UI:** `http://localhost:8000` after `python -m demo.api_v2`

**Screenshots to insert:**
- `docs/screenshots/01_web_ui_overview.png` — full dashboard, all 21 events in sidebar
- `docs/screenshots/02_event_17_escalate.png` — loop detection ESCALATE firing in real time
- `docs/screenshots/03_event_19_ranked_coupons.png` — grounded multi-agent coupon ranking output
- `docs/screenshots/04_audit_chain.png` — hash chain verification after tamper (Event 09)
- `docs/screenshots/05_session_tree.png` — 5-agent session hierarchy with freeze state
- `docs/screenshots/06_eval_results.png` — 11/11 eval assertions passing
- `docs/screenshots/07_terminal_run.png` — full 21-event terminal run with Rich UI

---

## What I Built

A production-grade stateful governance runtime for multi-agent orchestration. Five specialized agents collaborate on GrabOn's coupon ops task — scraping, validating, ranking, and reporting active deals — under deterministic enforcement at every step.

The runtime sits between agent reasoning and every external tool they can call. Agents cannot bypass it. Constraints survive context window collapse. Budget enforcement is linearizable. Every decision is cryptographically auditable.

```
Human Task
    ↓
Orchestrator (Gemini Flash)          ← primary, Groq LLaMA fallback
    ↓
┌─────────────────────────────────────────────────────────┐
│                 GOVERNANCE RUNTIME                      │
│                                                         │
│   validate() ─── 9 checks ─── ALLOWED/BLOCKED/          │
│       │                       PENDING/ESCALATE          │
│       │                                                 │
│   Constraint Store (SQLite WAL)                         │
│       │    pii_accessed · budget_spent · reauth_ttl     │
│       │    version-tracked · append-only · signed       │
│       │                                                 │
│   Audit Chain (SHA-256)                                 │
│       │    every decision linked · tamper-detectable    │
│       │                                                 │
│   Heartbeat Monitor                                     │
│       │    result-hash comparison · stuck detection     │
│       │                                                 │
│   Loop Detector                                         │
│            3 blocks in 300s → ESCALATE                  │
└─────────────────────────────────────────────────────────┘
    ↓
Data Agent (Groq LLaMA 3.1)      Report Agent (Groq Mistral)
Compliance Agent (Groq Mistral)   Statistical Sub (Gemini Flash)
    ↓
External Tools: database · slack_api · budget_spend · sensitive_data
```

---

## The GrabOn Task

```
Human:
  "Find the best active coupons on GrabOn right now.
   Scrape available deals, validate expiry and working status,
   classify by category and merchant, rank by value,
   and generate a structured report with top 10 verified coupons."
```

Every governance problem in the 21 events surfaces naturally from this one realistic task. No artificial triggers.

**Final ranked output (Event 19 — grounded multi-agent pipeline):**

| Rank | Merchant | Discount | Confidence | Status |
|------|----------|----------|------------|--------|
| 1 | Myntra | 70% OFF | 0.94 | ✓ Verified |
| 2 | MakeMyTrip | ₹2000 OFF Flights | 0.93 | ✓ Verified |
| 3 | Ajio | 60% OFF | 0.92 | ✓ Verified |
| 4 | Nykaa | ₹500 OFF | 0.91 | ✓ Verified |
| 5 | Flipkart | 80% OFF Electronics | 0.90 | ✓ Verified |
| 6 | Boat | 65% OFF | 0.90 | ✓ Verified |
| 7 | Puma | 55% OFF | 0.89 | ✓ Verified |
| 8 | Zomato | 50% OFF up to ₹100 | 0.87 | ✓ Verified |
| 9 | PharmEasy | 25% OFF | 0.66 | ⚠ Review |
| 10 | Swiggy | ₹125 OFF | 0.62 | ⚠ Review |

PharmEasy and Swiggy triggered `MessageType.REVISION_NEEDED` — below the 0.70 confidence threshold. Compliance Agent reviewed. Offers flagged for human review, not silently dropped.

---

## Agent Composition

| Agent | Role | Primary | Fallback | Rationale |
|-------|------|---------|----------|-----------|
| Agent 1 | Orchestrator | Gemini 2.0 Flash | Groq LLaMA 3.1 8B | Strongest at multi-step reasoning + recovery decisions |
| Agent 2 | Data Agent | Groq LLaMA 3.1 8B Instant | — | Fast, cheap for structured data extraction |
| Agent 3 | Report Agent | Groq Mistral Saba 24B | — | Strong at structured narrative + ranking output |
| Agent 4 | Compliance Agent | Groq Mistral Saba 24B | — | Deterministic arbitration — does NOT use LLM for final verdicts |
| Agent 5 | Statistical Sub | Gemini 2.0 Flash | Groq Mistral | Dynamically spawned; numerical scoring is pure Python |

**Why not LLM for everything:** The Validator in Event 19 is pure Python — `confidence < 0.70` is a number comparison, not a reasoning task. The assignment explicitly values knowing when *not* to use an LLM. The Compliance Agent's tiebreaker (Event 21) uses evidence-count thresholds, not model judgment.

**Provider failover:** Every agent role has a `_FailoverAgent` wrapper. If the primary returns a degraded/simulated response, it tries the fallback silently and logs `FAILOVER role=X provider=Y`. No crashes.

---

## Architecture

### The Four Layers

```
┌──────────────────────────────────────────────────┐
│  LAYER 1: Agent Reasoning (LLM)                  │
│  Agents propose actions. Nothing more.           │
└──────────────────────┬───────────────────────────┘
                       │ every tool call
┌──────────────────────▼───────────────────────────┐
│  LAYER 2: Interceptor (validate.py)              │
│  9 checks in fixed order. First failure blocks.  │
│  ALLOWED / BLOCKED / PENDING / ESCALATE          │
└──────────────────────┬───────────────────────────┘
                       │ reads/writes
┌──────────────────────▼───────────────────────────┐
│  LAYER 3: Constraint Store (store.py)            │
│  Append-only. Version-tracked. Authority-mapped. │
│  SQLite WAL + threading.Lock (linearizable)      │
└──────────────────────┬───────────────────────────┘
                       │ reaches
┌──────────────────────▼───────────────────────────┐
│  LAYER 4: External Tools                         │
│  Never contacted without LAYER 2 clearance.      │
└──────────────────────────────────────────────────┘
```

The agent cannot bypass this path. There is no side door.

### The 9 Interceptor Checks

Every tool call passes all 9 checks in this exact order. First failure short-circuits immediately.

```
CHECK 1  Session existence          — session_id is real
CHECK 2  Session validity           — ACTIVE, not expired, not frozen
CHECK 3  Identity continuity        — principal + type match session record
CHECK 4  Constraint version         — agent reading fresh state (read-your-writes)
CHECK 5  Re-authentication gate     — sensitive actions require verified reauth
CHECK 6  Dynamic constraints        — budget, PII taint, capability
CHECK 6.5 Rate limiting             — 10 calls/60s per tool per session
CHECK 6.6 Loop detection            — 3 blocks in 300s → ESCALATE
CHECK 7  Idempotency                — deduplication on retry
```

**Four outcomes:**
- `ALLOWED` — all checks pass, tool executes, audit log written
- `BLOCKED` — check failed, tool never contacted, audit log written  
- `PENDING` — spend > $200 threshold, human approval required
- `ESCALATE` — loop detected, Orchestrator receives signal, NOT human

### Session Tree

```
HUMAN Root ($2000 budget · all capabilities · expires 2h)
    └── ORCHESTRATOR — Agent 1 ($500 budget · spawn_depth=3)
            ├── AGENT — Data Agent ($0 · spawn_depth=1 · query_records+reauth+spawn)
            │       └── SUBAGENT — Agent 5 ($0 · spawn_depth=0 · query_records only)
            │           [FROZEN after Agent 2 froze — cascade propagated synchronously]
            ├── AGENT — Report Agent ($0 · spawn_depth=0 · query_records+reauth)
            └── COMPLIANCE — Agent 4 ($0 · spawn_depth=0 · read_tree_state)
```

**Attenuation invariants enforced at every spawn:**
1. `child.capabilities ⊆ parent.capabilities` — always
2. `child.budget_grant ≤ parent.remaining_budget` — always
3. `child.can_spawn_depth = parent.can_spawn_depth - 1` — always

Child sessions cannot be granted capabilities the parent doesn't hold. Agent 5 cannot spawn anything. These are not configuration options — they are enforced before the session row is written.

### Constraint Store Consistency

Budget enforcement requires linearizable reads. Two agents simultaneously requesting ₹300 against a ₹500 limit:

```
WITHOUT LOCK:
  Agent A reads budget_spent=0  →  passes  →  writes 300
  Agent B reads budget_spent=0  →  passes  →  writes 300
  Total spend: ₹600. Limit: ₹500. Silent violation.

WITH _budget_lock (our implementation):
  Agent A acquires lock → reads 0 → writes 300 → releases
  Agent B acquires lock → reads 300 → 300+300=600>500 → BLOCKED
  Total spend: ₹300. Invariant holds.
```

`threading.Lock` + SQLite WAL mode = serializable budget writes in this prototype. Production path: Google Cloud Spanner (TrueTime-backed external consistency, no application-layer lock needed). See `docs/consistency_model.md` for the full argument.

### Shared State

The constraint store is the single source of truth. It is:
- **Append-only** — no UPDATE or DELETE ever. Current value = highest-priority most-recent non-expired row.
- **Authority-controlled** — `budget_limit` can only be written by HUMAN. `budget_spent` only by INTERCEPTOR. The store rejects unauthorized writes before they reach any DB operation.
- **Version-tracked** — monotonic counter per session. Check 4 blocks agents acting on stale state.
- **Taint-propagating** — `pii_accessed=True` writes propagate synchronously upward to all parent sessions in the same transaction.

### Typed Communication Protocol

Agents do not communicate via free-text dicts. All inter-agent messages use typed Pydantic models:

```python
MessageType.REQUEST          # agent requests action/assessment
MessageType.RESPONSE         # agent returns result with confidence score
MessageType.REVISION_NEEDED  # orchestrator disagrees, proposes alternative
MessageType.VETO             # compliance rejects, issues final verdict
MessageType.ESCALATION       # loop/failure detected, route to orchestrator
MessageType.APPROVAL         # human approves pending action
MessageType.HEALTH_SIGNAL    # heartbeat stuck-agent notification
MessageType.CONSTRAINT_VIOLATION  # structured BLOCKED record
```

Invalid message types are rejected at construction (Pydantic validation). No string soup.

**Conflict resolution (Event 21):**
```
Data Agent:    merchant_risk=HIGH   confidence=0.60  (below 0.75 threshold)
Orchestrator:  merchant_risk=MEDIUM confidence=0.70  (below 0.75 threshold)
                            ↓
                    both below threshold → requires_tiebreaker=True (auto-set)
                            ↓
Compliance Agent: evidence_count=3 + metadata_mismatch=True → HIGH
                            ↓
Veto (requires_commitment=True) → write_constraint(merchant_risk="HIGH")
                            ↓
get_constraint(comp_sid, "merchant_risk") → "HIGH" ✓ committed
```

The Compliance Agent's verdict is not advisory. `requires_commitment=True` means it passes through the interceptor and is written permanently to the constraint store. Immutable until HUMAN override.

---

## 21 Governance Events

Every problem surfaces naturally from the GrabOn coupon ops task.

| # | Event | Outcome | Check/Layer |
|---|-------|---------|-------------|
| 01 | Response path injection intercepted | BLOCKED | Response Scanner (14 regex patterns) |
| 02 | PII taint armed — upward propagation | ALLOWED | Trigger map → `_trigger_pii_taint()` |
| 03 | Constraint authority conflict | BLOCKED | `CONSTRAINT_AUTHORITY_MAP` pre-DB |
| 04 | Stateful taint blocks Slack post | BLOCKED | Check 6 — cross-step PII memory |
| 05 | Attenuation enforced at spawn | BLOCKED | Session layer — Invariant 1 |
| 06 | Spawn depth exhausted | BLOCKED | Session layer — Invariant 3 |
| 07 | Cascade freeze from parent | BLOCKED | Check 2 — cascade propagation |
| 08 | Human-in-the-loop PENDING | PENDING | Check 6b — budget threshold |
| 09 | Tamper-evident audit chain | BLOCKED | SHA-256 chain verification |
| 10 | Benchmark: stateful vs stateless vs raw LLM | ALLOWED | Architecture comparison |
| 11 | Session split attack — principal ledger | BLOCKED | Budget attenuation at spawn |
| 12 | Idempotency — retry deduplication | ALLOWED | Check 7 — idempotency cache |
| 13 | Time-bounded reauth TTL expiry | BLOCKED | Check 5 — TTL expired |
| 14 | Interceptor bypass attempt (frozen) | BLOCKED | Check 2 — state store beats metadata |
| 15 | Fail closed on state store failure | BLOCKED | Fail-closed policy — CRITICAL actions |
| 16 | Rate limit enforced at call 11 | BLOCKED | Check 6.5 — sliding window |
| 17 | Loop detection — ESCALATE fires | ESCALATE | Check 6.6 — 3 blocks in 300s |
| 18 | Heartbeat stuck agent detection | BLOCKED | `is_stuck()` — hash comparison |
| 19 | Grounded multi-agent coupon ranking | ALLOWED | Full 5-agent governed pipeline |
| 20 | Managed healing — Orchestrator recovers | ESCALATE | `get_session_health()` → recovery |
| 21 | Typed protocol conflict resolution | ALLOWED | RevisionNeeded → Veto → constraint store |

### Key Events Explained

**Event 04 — Why statelessness fails:**  
Orchestrator queries the PII salary table at step 2. Context window fills. At step 30, it tries to post to Slack. A stateless system has no memory of step 2. Our runtime reads `pii_accessed=True` directly from the constraint store on every single validate() call — the agent's claim about what it did is never consulted. BLOCKED.

**Event 09 — Tamper detection:**  
An attacker writes `reauth_verified=true` directly to the constraints table (bypassing the interceptor). They then modify the execution_log to cover tracks. `verify_audit_chain()` detects the modification at entry 0: `SHA256(modified_content) ≠ stored_entry_hash`. Chain broken. Tamper confirmed.

**Event 17 — Loop detection:**  
Orchestrator retries a PII-tainted Slack post 3 times without fixing the root cause. After the 3rd identical block within 300 seconds, the interceptor returns `ESCALATE` instead of `BLOCKED`. The Orchestrator — not the human — receives this signal and calls `get_session_health()` to decide recovery.

**Event 19 — Grounded pipeline:**  
Five agents collaborate with verified governance state injected before every step. The Validator is pure Python — no LLM involved. Low-confidence offers trigger typed `REVISION_NEEDED` messages. The final ranking is deterministic and auditable.

**Event 21 — Typed conflict:**  
Two agents disagree. Both below the confidence threshold. The auto-set `requires_tiebreaker=True` field routes to the Compliance Agent. Evidence-count logic (not model judgment) produces the verdict. The Veto commits it permanently to the constraint store.

---

## Evaluation Results

**11 / 11 assertions passing.** Run independently:

```bash
python -m eval.runner
```

| # | Assertion | Result | Detail |
|---|-----------|--------|--------|
| E1 | Budget never exceeded | ✓ PASS | spent ≤ limit across all sessions including concurrent race |
| E2 | BLOCKED actions never reached tools | ✓ PASS | 0 blocked decisions resulted in tool execution |
| E3 | SHA-256 audit chain intact | ✓ PASS | tamper detected correctly at entry 0 (Event 09) |
| E4 | Scanner caught prompt injection | ✓ PASS | system_prefix + reauth_inject patterns matched |
| E5 | Concurrent budget race prevented | ✓ PASS | WAL lock: one ALLOWED, one BLOCKED |
| E6 | ESCALATE fires on loop detection | ✓ PASS | ESCALATE written after 3 consecutive blocks |
| E7 | Rate limit blocks at threshold | ✓ PASS | call 11 blocked — reason: rate_limit_exceeded |
| E8 | Heartbeat log populated | ✓ PASS | entries recorded for all ALLOWED tool calls |
| E9 | Loop detection threshold correct | ✓ PASS | loop_detected escalations confirmed at threshold=3 |
| E10 | State grounding budget accurate | ✓ PASS | constraint store budget consistent across all reads |
| E11 | Conflict resolution verdict committed | ✓ PASS | merchant_risk=HIGH in constraint store via Veto |

The assertions are hard assertions, not vibes. Each one either PASS or FAIL with a detail string. See `eval/assertions.py` for the full implementation and `docs/eval_report.txt` for the raw output.

---

## Cost Data

| Metric | Value | Notes |
|--------|-------|-------|
| Full development cost | ~$1–3 | across all iterations with live API keys |
| One full 21-event demo run (simulated) | $0.00 | no API calls in simulation mode |
| One full 21-event demo run (live keys) | ~$0.02–0.05 | Gemini Flash + Groq free tiers |
| One eval run | ~$0.00 | reads only, no LLM calls |
| Gemini Flash (input) | $0.075/1M tokens | primary for Orchestrator + Statistical Sub |
| Groq LLaMA 3.1 8B Instant | Free tier | Data Agent |
| Groq Mistral Saba 24B | Free tier | Report + Compliance agents |

**Per-agent cost breakdown (live run, Event 19 — full pipeline):**
- Orchestrator (Gemini Flash): ~$0.001 for state grounding + reasoning
- Data Agent (Groq LLaMA): effectively $0 on free tier
- Report Agent (Groq Mistral): effectively $0 on free tier
- Compliance Agent (Groq Mistral): effectively $0 on free tier
- Statistical Sub (Gemini Flash): ~$0.0005 for numerical scoring

**At GrabOn scale (3,500 merchants, daily run):**  
Estimated ~$0.15–0.50/day for a full live pipeline run with this agent composition. The cheap-model-for-cheap-tasks routing is deliberate — Groq for extraction/validation, Gemini only where reasoning quality matters.

**Live vs simulated:** All 5 providers make live API calls when keys are set. Without keys, all events run with deterministic simulation. The governance enforcement layer (interceptor, constraint store, audit chain, session tree) is fully operational in both modes — it does not depend on LLM calls.

---

## Runtime Statefulness and Reset Semantics

**This is the most important thing to understand about this system.**

The runtime intentionally accumulates governance memory during execution:

- Repeated blocked actions accumulate toward ESCALATE (loop detection threshold)
- PII taint (`pii_accessed=True`) persists for the entire session lifetime
- Re-authentication tokens have TTLs that expire mid-workflow
- Budget_spent increments permanently — approved spends do not reverse
- Cascade freeze from a parent session permanently freezes all descendants

This means **running the same event twice in one session produces different results** — which is correct behavior, not a bug. Event 2 (PII taint) arms a flag. Running Event 17 (loop detection) multiple times accumulates blocks toward ESCALATE faster.

**`POST /demo/reset`** wipes the entire database and recreates a fresh session tree. Use it between isolated demo runs. The API and terminal demo both call reset at startup.

For evaluators: if events behave unexpectedly, hit Reset and start from Event 1. The system is working exactly as designed — state from earlier events affects later events because that is the point.

---

## What the Interceptor Actually Does

This is what `call_tool()` looks like from the agent's perspective:

```python
# Agent calls:
result = self.call_tool("database", "query_pii_table")

# What actually happens inside call_tool():
# 1. Sign the request with Ed25519 private key
auth_message = f"{session_id}:{tool}:{action}:{metadata_json}".encode()
metadata["signature"] = sign_message(private_key, auth_message)

# 2. validate() — all 9 checks
decision = validate(session_id, principal_id, principal_type, tool, action, metadata)

# 3. If BLOCKED → raise GovernanceBlock (tool never called)
# 4. If PENDING → raise GovernancePending (human must decide)
# 5. If ESCALATE → raise GovernanceEscalate (orchestrator notified)

# 6. Verify capability token (HMAC signed by interceptor)
verify_capability_token(token, session_id, tool, action, ...)

# 7. Call the actual tool
raw_result = TOOL_REGISTRY[tool](action, metadata, capability_token, ...)

# 8. Scan response for injection patterns
clean_result, was_blocked = scan_response(session_id, tool, raw_result)

# 9. Record heartbeat progress marker
record_heartbeat(session_id, tool, action, raw_result)

# 10. Return to agent
return clean_result
```

The agent cannot shortcut any of these steps. `call_tool()` is the only path to any tool. The tool registry is not accessible directly. The capability token verification at step 6 is inside the tool functions themselves — even if `call_tool()` were bypassed, the tools would reject unsigned calls.

---

## Per-Module Design Decisions

### `runtime/interceptor/validate.py`

**Decision: 9 checks in fixed order, first failure short-circuits.**

Alternatives considered: priority-based routing (check budget first on budget actions, check taint first on external actions). Rejected: the ordering exists for security reasons. Check 1 (session existence) must run before Check 3 (identity) because you cannot compare a claimed identity against a session record that doesn't exist. Check 3 before Check 6 because identity continuity is a prerequisite for any dynamic constraint to be meaningful. The order is a security invariant, not a performance optimization.

**Decision: ESCALATE is a separate outcome from BLOCKED.**

Rejected alternative: ESCALATE = special BLOCKED with a flag. The distinction matters: BLOCKED means "this call was wrong, don't retry." ESCALATE means "this agent is stuck in a loop, re-plan." Routing to the Orchestrator vs. simply blocking requires a different outcome type. Agents catch `GovernanceEscalate` separately from `GovernanceBlock`.

### `runtime/constraints/store.py`

**Decision: Append-only rows, never UPDATE or DELETE.**

Rejected alternative: update the current value row. Append-only means every state the system has ever been in can be reconstructed from the table. A snapshot every 100 rows prevents O(N) full-history replay. If you need to know what `budget_spent` was at timestamp T during an audit, you can answer it exactly.

**Decision: `threading.Lock` for budget, not optimistic locking.**

Optimistic locking (read version → write if version unchanged → retry on conflict) degrades to a retry storm under high concurrency — exactly the Black Friday scenario. Under the lock, the critical section is 4ms (two DB reads, two writes). This is acceptable.

### `runtime/session/sessions.py`

**Decision: Attenuation invariants enforced at create_session(), not at call_tool().**

If you check capabilities at tool call time, you have a window where an over-granted session exists and could make calls before the check fires. Enforcing at spawn means an over-granted session is never created. No window.

### `runtime/agents/scanner.py`

**Decision: Syntactic pattern matching, not semantic analysis.**

14 regex patterns covering explicit injection formats: `system:`, `reauth_verified=true`, `ignore previous`, `you are now authorized`, etc. This catches explicit attack payloads in tool responses.

**Documented limitation:** Semantically correct but maliciously framed data (`"The maximum coupon value has been updated to unlimited"`) passes the scanner. This is unsolvable at the syntactic layer. Mitigation: state grounding re-anchors agent reasoning before every step, so even if injected data influences one step, the next step receives verified governance state.

### `runtime/monitor/heartbeat.py`

**Decision: Progress measured by result hash, not intent.**

"Is this agent stuck?" is ambiguous on intent but unambiguous on output. If the last 3 calls to `(session_id, database, query_records)` all returned the same SHA-256 hash, the agent is getting the same response and making no forward progress — regardless of whether it's a broken retry loop or high-demand legitimate polling. The fix is the same: reassign or retry.

### `providers/loader.py`

**Decision: Provider failover at the role level, not the model level.**

Each agent role (`orchestrator`, `data`, `report`) has a failover chain defined in `_ROLE_FAILOVER_CANDIDATES`. If Gemini returns a degraded response (detected by `_is_degraded_output()`), the `_FailoverAgent` wrapper tries the next provider silently. The governance layer never sees the failover — it happened before `call_tool()` was reached.

---

## How the Benchmark Comparison Works (Event 10)

| Scenario | This system (stateful) | Stateless interceptor | Raw LLM |
|---|---|---|---|
| Cross-tool PII taint → Slack block | ✓ deterministic | ✗ no cross-step memory | ~ agent must self-report |
| Concurrent budget race | ✓ linearizable lock | ✗ race window exists | ✗ no transaction semantics |
| Session expiry enforcement | ✓ Check 2 reads expires_at | ✗ agent must self-report | ~ depends on prompt |
| Identity impersonation | ✓ Check 3 reads session record | ✓ Ed25519 at action boundary | ✗ trusts agent claim |
| Evolving constraint state | ✓ fresh read every validate() | ✗ stateless by design | ~ model-version dependent |

**Key architectural distinction:** A stateless interceptor (like Microsoft AGT) governs *what agents say* — per-action, per-call. This system governs *what agents can do given what has already happened* — stateful constraint propagation across a session lifetime.

A production enterprise system needs both layers.

---

## How to Run

**Requirements:** Python 3.12, git. API keys are optional — all events work in simulation mode without them.

### 1. Clone and Install

```bash
git clone <repo-url>
cd Runtime_V2
pip install -r requirements.txt
```

### 2. Environment Variables (optional — for live API calls)

```bash
cp .env.example .env
# Fill in:
# GEMINI_API_KEY=...    (Google AI Studio — free tier)
# GROQ_API_KEY=...      (Groq — free tier, no card required)
# OPENAI_API_KEY=...    (optional — not required for any event)
```

Without keys: all 21 events run with deterministic simulation. Governance enforcement is fully operational.

### 3. Web UI (recommended — full observability dashboard)

```bash
python -m demo.api_v2
# Open: http://localhost:8000
```

From the UI you can:
- Run any of the 21 events individually
- Run all 21 in sequence
- Inspect the audit log with hash chain verification
- See the live session tree with constraint state
- Approve/reject PENDING actions (Event 08)
- View scanner log, rate limit counters, heartbeat data, cascade events

### 4. Terminal Demo

```bash
# All 21 events
python -m demo.five_agent_demo

# Single event
python -m demo.five_agent_demo --event 19

# Summary only (silent execution)
python -m demo.five_agent_demo --summary
```

### 5. Evaluation Suite

```bash
python -m eval.runner
# Outputs: pass/fail per assertion with detail strings
# Raw report: docs/eval_report.txt
```

### 6. Scenario Runner (inject failure conditions)

```bash
python -m demo.scenario_runner --scenario TOOL_INSTABILITY
python -m demo.scenario_runner --scenario BUDGET_PRESSURE
python -m demo.scenario_runner --scenario PROTOCOL_CONFLICT
python -m demo.scenario_runner --scenario BASELINE
```

### 7. Provider Test

```bash
python Runtime_V2/test_providers.py
# Verifies each provider makes a live API call
```

### Minimum Bar Check (Assignment 06)

| Requirement | Status |
|-------------|--------|
| At least 3 agents with different models and different tool sets | ✓ 5 agents across 3 providers |
| Communication uses structured messages, not free-text | ✓ 8 typed MessageType variants (Pydantic) |
| At least one conflict scenario resolved programmatically | ✓ Event 21 — Compliance Veto commits to store |
| Shared state exists and is inspectable | ✓ `/demo/state` + `/demo/audit-log` |
| At least one pipeline step uses deterministic code (no LLM) | ✓ Validator in Event 19 + Compliance scoring in Event 21 |

---

## What Broke First

**The DB path mismatch that made everything look right and be wrong.**

`api_v2.py` patches all module-level `get_connection` references to point at `governance_v2_demo.db`. That works for every module that imports `get_connection` at call time. But the endpoint handlers used a `get_connection` symbol imported at module load time — before the patch ran. So:

- Terminal demo: all events use `governance_v2_demo.db` ✓
- Web UI `/demo/event/8`: uses `governance_v2_demo.db` ✓  
- Web UI `/demo/audit-log`: uses `governance_v2.db` (empty) ✗
- Web UI `/demo/state`: uses `governance_v2.db` (empty) ✗

Every event ran fine. Every observability endpoint returned empty results. The system appeared to work and had no data. Fix: replace all direct `get_connection` imports in endpoint handlers with a local wrapper function `get_connection()` that always calls `_conn()`.

**The concurrent budget test that wasn't concurrent.**

Event 10 (concurrent budget race) ran two threads and both got ALLOWED. The test was "proving" the race condition was safe when it was actually demonstrating the lock wasn't working. Root cause: both threads were completing so fast that one finished before the other started — the "concurrency" was serialized by GIL + SQLite single-writer. Fix: add a `time.sleep(0.01)` inside the budget write to create a real concurrent window. Now one consistently blocks.

**The reset that wasn't a reset.**

Running the full demo twice without a proper reset caused loop detection to fire immediately on the second run — the BLOCKED entries from run 1 were still within the 300-second window in run 2. The `_reset()` function in `api_v2.py` called `setup_demo()` which calls `os.remove()` + `init_db()` internally, but there was a window where the old DB was being read while the new one was being set up. Fix: `_reset()` now explicitly removes the DB file and calls `init_db()` directly before `setup_demo()` runs. Also: `_capture_event()` clears `demo.event_results` at the start of each event to prevent prior results leaking into the current event's output.

**The SQLite lock storm under concurrency.**

WAL mode helps with concurrent reads, but concurrent writes under the `threading.Lock` still hit SQLite's `database is locked` error intermittently. Root cause: Python's sqlite3 module's transaction management is implicit — `isolation_level=DEFERRED` auto-starts transactions, and nested `with conn:` blocks inside the same connection object would try to commit at each exit, sometimes hitting a lock. Fix: `GovernanceConnection` in `schema.py` tracks nesting depth (`_depth`). Only the outermost `with conn:` block commits and closes. Inner blocks are no-ops. Added `PRAGMA busy_timeout=5000` and a retry loop with exponential backoff.

---

## What I Would Change With 2 More Weeks

1. **Live merchant crawling.** Replace the deterministic `get_grabon_coupons()` dataset with real Myntra/Zomato/MakeMyTrip scraping — Cloudflare handling, JS rendering via Playwright, rate limiting, and the actual deal freshness pipeline the assignment describes. The governance layer is ready for this; the data source is the gap.

2. **Distributed state store.** Swap SQLite WAL for CockroachDB or Spanner with the same schema. The `threading.Lock` doesn't span processes — a multi-worker API deployment breaks budget linearizability. The `docs/consistency_model.md` already has the full production architecture argument.

3. **Provider execution dashboard.** Surface `requested_provider`, `executed_provider`, `fallback_reason`, and `live_api_call` as a first-class UI panel. Right now it's in the terminal logs but not in the web UI.

4. **Pluggable policy engine.** Currently the authority map (`CONSTRAINT_AUTHORITY_MAP`), sensitivity thresholds, and blocked action sets are in-code constants. Make them runtime-configurable without a deploy — store them in the constraint store with HUMAN authority, changeable via the UI.

5. **MCP server.** Expose the governance runtime as an MCP tool so Claude Desktop can interact with it directly: "Check if Agent 2 is stuck." "What is the current budget remaining?" "Approve the pending payment." The session tree and audit chain are already structured for this.

6. **Formal deep-dive prep.** A live demo where I kill a model endpoint mid-run and the fallback chain recovers in real time. The architecture supports it — I just need to wire the provider down/up signal to the UI.

---

## Documentation Map

| Document | Purpose |
|----------|---------|
| `README.md` | This file — full architecture, design decisions, how to run |
| `ARCHITECTURE.md` | High-level four-layer runtime overview |
| `docs/architecture.md` | Detailed enforcement internals per component |
| `docs/consistency_model.md` | Why linearizable reads matter — the budget race argument |
| `docs/threat_model.md` | 7 threat types with mitigations and out-of-scope |
| `docs/test_plan.md` | Invariant-driven test strategy — assertion format per event |
| `docs/risks.md` | 8 risks across technical, operational, strategic |
| `docs/requirements.md` | Functional and non-functional requirements with test mapping |
| `docs/data_model.md` | All 12 SQLite tables with column types and indexes |
| `docs/api_spec.md` | FastAPI endpoint reference with request/response examples |
| `docs/eval_report.txt` | Raw eval output — pass/fail per assertion with detail |

---

## Project Structure

```
GrabOn_Swarm_06/
│
├── README.md
├── ARCHITECTURE.md                             # High-level four-layer overview
├── requirements.txt
│
├── docs/                                       # Design documentation
│   ├── architecture.md                         # Detailed enforcement internals
│   ├── api_spec.md                             # FastAPI endpoint reference
│   ├── consistency_model.md                    # Linearizability argument + Spanner path
│   ├── data_model.md                           # All 12 SQLite tables documented
│   ├── eval_report.txt                         # Raw eval output — pass/fail per assertion
│   ├── requirements.md                         # FR/NFR with test mapping
│   ├── risks.md                                # 8 operational/technical/strategic risks
│   ├── test_plan.md                            # Invariant-driven test strategy
│   ├── threat_model.md                         # 7 threats with mitigations
│   └── full_architecture_walkthrough.md        # Full architecture deep-dive
│
└── Runtime_V2/                                 # Core governed multi-agent runtime
    │
    ├── demo/                                   # Demo layer — API, UI, events, scenarios
    │   ├── api_v2.py                           # FastAPI server — all 21 event endpoints + UI
    │   ├── five_agent_demo.py                  # 21-event terminal demo runner
    │   ├── ui_v2.html                          # Single-file observability dashboard
    │   ├── scenario_runner.py                  # Scenario harness
    │   └── scenarios/
    │       ├── engine.py                       # ScenarioContext, runtime_config translation
    │       ├── injector.py                     # Latency + tool failure injection
    │       ├── metrics.py                      # Scenario metric collection
    │       └── profiles.py                     # BASELINE, BUDGET_PRESSURE, TOOL_INSTABILITY, PROTOCOL_CONFLICT
    │
    ├── eval/                                   # Evaluation suite
    │   ├── assertions.py                       # 11 hard governance assertions
    │   └── runner.py                           # Eval runner — pass/fail with detail strings
    │
    ├── providers/                              # LLM provider integrations
    │   ├── gemini_provider.py                  # Gemini 2.0 Flash — Orchestrator + Statistical Sub
    │   ├── groq_provider.py                    # LLaMA 3.1 8B Instant + Mistral Saba 24B
    │   ├── openai_provider.py                  # GPT-4o — Compliance Agent (optional)
    │   └── loader.py                           # Role → provider resolution + failover chain
    │
    ├── runtime/                                # Governance engine — core of the system
    │   ├── schema.py                           # 12-table SQLite schema, WAL mode, GovernanceConnection
    │   ├── agents/
    │   │   ├── base.py                         # AgentBase, GovernanceBlock/Pending/Escalate, TOOL_REGISTRY
    │   │   └── scanner.py                      # 14-pattern response injection scanner
    │   ├── constraints/
    │   │   └── store.py                        # Append-only store, authority map, taint propagation
    │   ├── identity/
    │   │   └── principals.py                   # Ed25519 keypairs, create/verify/sign
    │   ├── interceptor/
    │   │   └── validate.py                     # 9-check enforcement function — core of the system
    │   ├── monitor/
    │   │   ├── heartbeat.py                    # Progress tracking, is_stuck() hash comparison
    │   │   └── live.py                         # Triggered health reader (on ESCALATE only)
    │   ├── protocol/
    │   │   └── messages.py                     # 8 typed MessageType Pydantic models
    │   ├── session/
    │   │   └── sessions.py                     # Session tree, attenuation invariants, cascade_freeze
    │   ├── tools/
    │   │   └── grabon.py                       # Deterministic GrabOn coupon dataset
    │   └── ui/                                 # Rich terminal UI
    │       ├── console.py
    │       ├── panels.py
    │       ├── renderers.py
    │       ├── styles.py
    │       └── tables.py
    │
    ├── governance_v2_demo.db                   # Runtime SQLite state store (demo mode)
    └── test_providers.py                       # Verify each provider makes a live API call
```

---

## Assignment 06 Rubric Self-Assessment

| Dimension (weight) | 5/5 Requires | Our Implementation |
|-------------------|--------------|-------------------|
| **Agent Architecture (25%)** | Clean abstraction, Plan/Act/Observe/Decide visible, tool registry discoverable, state management versioned | Interceptor = named enforcement boundary. Constraint store = versioned state. TOOL_REGISTRY = discoverable. `call_tool()` = explicit Plan/Validate/Execute/Scan phases. |
| **Eval Rigor (20%)** | 30+ cases, catches real regression, statistical comparison | 11 hard assertions, deterministic pass/fail with detail strings, all linked to architectural invariants. Run in isolation, not just post-demo. |
| **Failure Recovery (20%)** | Distinguishes error types, re-plans on persistent failure, budget kills runaway, observability shows every decision | BLOCKED/PENDING/ESCALATE are distinct outcomes with distinct handling. Budget enforcement linearizable. Loop detection → managed healing. Every decision in audit chain. |
| **Multi-LLM & Cost (15%)** | 4+ providers deliberately, cost tracked, cheaper model for subtasks, shadow testing | 3 active providers (Gemini, Groq LLaMA, Groq Mistral) + OpenAI optional. Cheap models for cheap tasks. Failover chain. Cost data in README with per-provider breakdown. |
| **Code & README (20%)** | README is architecture doc, diagram, tradeoffs, what broke first, what I'd change | This document. |

---

*Assignment 06 — The Swarm*  
*GrabOn AI Labs — Agentic AI Engineer Challenge 2026*
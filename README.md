# GrabOn Swarm Runtime
## Assignment 06 — The Swarm · Stateful Multi-Agent Governance Runtime

> *"Build something that runs while you sleep. Break something interesting along the way. Tell us about both."*

A stateful governance runtime that enforces what agents can do based on what has **already happened** — not just the current request.

---

## Quick Start

```bash
git clone https://github.com/Hemanth0508/GrabOn_Swarm_06
cd GrabOn_Swarm_06
pip install -r requirements.txt

# Web UI (recommended)
python -m Runtime_V2.demo.api_v2
# Open: http://localhost:8000

# Terminal — all 21 events
python -m Runtime_V2.demo.five_agent_demo

# Single event
python -m Runtime_V2.demo.five_agent_demo --event 19

# Eval suite
python -m Runtime_V2.eval.runner
```

**API keys are optional.** All 21 events run with deterministic simulation without them. The governance engine — interceptor, constraint store, audit chain, session tree — is fully operational either way.

```bash
# Optional: copy and fill in keys for live API calls
cp .env.example .env
# GEMINI_API_KEY=...   (aistudio.google.com — free)
# GROQ_API_KEY=...     (groq.com — free, no card)
```

---

## Why I Chose This Assignment

Writing agents is easy. Governing them is hard.

What happens when Agent 2 accesses salary data at step 2, the context window fills, and at step 30 it tries to post that data to Slack? A stateless system has no memory of step 2. A naive stateful system has a race condition. A production system has neither problem.

Assignment 06 asks the question that determines whether an agentic system is actually deployable in a real enterprise. I wanted to answer it properly.

---

## Demo

> 📺 **Loom Walkthrough:** `[INSERT LOOM LINK]`
> Covers: happy path end-to-end (Event 19), failure recovery (Events 17 + 20), eval run (11/11), architecture walkthrough, provider failover live.

> 🌐 **Web UI:** `http://localhost:8000` after `python -m Runtime_V2.demo.api_v2`

**Screenshots:**
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

Every governance problem in the 21 events surfaces naturally from this one realistic task.

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

PharmEasy and Swiggy triggered `MessageType.REVISION_NEEDED` — below the 0.70 confidence threshold. Compliance Agent reviewed both. Flagged for human review, not silently dropped.

---

## Agent Composition

| Agent | Role | Primary | Fallback |
|-------|------|---------|----------|
| Agent 1 | Orchestrator | Gemini 2.0 Flash | Groq LLaMA 3.1 8B |
| Agent 2 | Data Agent | Groq LLaMA 3.1 8B Instant | — |
| Agent 3 | Report Agent | Groq Mistral Saba 24B | — |
| Agent 4 | Compliance Agent | Groq Mistral Saba 24B | — |
| Agent 5 | Statistical Sub | Gemini 2.0 Flash | Groq Mistral |

**Why not LLM for everything.** The Validator in Event 19 is pure Python — `confidence < 0.70` is a number comparison, not a reasoning task. The Compliance Agent's tiebreaker (Event 21) uses evidence-count thresholds, not model judgment. The assignment explicitly values knowing when *not* to use an LLM.

**Provider failover.** Every agent role has a `_FailoverAgent` wrapper. If the primary returns a degraded response, the fallback is tried silently and logged as `FAILOVER role=X provider=Y`. No crashes.

---

## Architecture

### Four Layers

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

Every tool call passes all 9 checks in this exact order. First failure short-circuits.

```
CHECK 1    Session existence          — session_id is real
CHECK 2    Session validity           — ACTIVE, not expired, not frozen
CHECK 3    Identity continuity        — principal + type match session record
CHECK 4    Constraint version         — agent reading fresh state (read-your-writes)
CHECK 5    Re-authentication gate     — sensitive actions require verified reauth
CHECK 6    Dynamic constraints        — budget, PII taint, capability
CHECK 6.5  Rate limiting              — 10 calls/60s per tool per session
CHECK 6.6  Loop detection             — 3 blocks in 300s → ESCALATE
CHECK 7    Idempotency                — deduplication on retry
```

Four outcomes: `ALLOWED` (tool executes), `BLOCKED` (tool never contacted), `PENDING` (human approval required above $200), `ESCALATE` (loop detected, Orchestrator notified — not human).

### Session Tree

```
HUMAN Root ($2000 budget · all capabilities · expires 2h)
    └── ORCHESTRATOR — Agent 1 ($500 budget · spawn_depth=3)
            ├── AGENT — Data Agent ($0 · spawn_depth=1)
            │       └── SUBAGENT — Agent 5 ($0 · spawn_depth=0)
            │           [FROZEN after Agent 2 froze — cascade propagated]
            ├── AGENT — Report Agent ($0 · spawn_depth=0)
            └── COMPLIANCE — Agent 4 ($0 · spawn_depth=0)
```

Attenuation invariants enforced at every spawn: child capabilities are always a subset of parent capabilities, child budget never exceeds parent remaining budget, spawn depth decrements by one. These are checked before the session row is written — not at call time.

### Budget Linearizability

Two agents simultaneously requesting ₹300 against a ₹500 limit:

```
WITHOUT LOCK:
  Agent A reads budget_spent=0  →  passes  →  writes 300
  Agent B reads budget_spent=0  →  passes  →  writes 300
  Total spend: ₹600. Limit: ₹500. Silent violation.

WITH _budget_lock:
  Agent A acquires lock → reads 0 → writes 300 → releases
  Agent B acquires lock → reads 300 → 300+300=600>500 → BLOCKED
  Total spend: ₹300. Invariant holds.
```

`threading.Lock` + SQLite WAL = serializable budget writes in this prototype. Production path: Cloud Spanner (TrueTime-backed external consistency, no application-layer lock needed).

### Typed Communication Protocol

Agents communicate via typed Pydantic models, not free-text dicts.

```python
MessageType.REQUEST            # agent requests action/assessment
MessageType.RESPONSE           # agent returns result with confidence score
MessageType.REVISION_NEEDED    # orchestrator disagrees, proposes alternative
MessageType.VETO               # compliance rejects, issues final verdict
MessageType.ESCALATION         # loop/failure detected, route to orchestrator
MessageType.APPROVAL           # human approves pending action
MessageType.HEALTH_SIGNAL      # heartbeat stuck-agent notification
MessageType.CONSTRAINT_VIOLATION  # structured BLOCKED record
```

Invalid message types are rejected at construction. No string soup.

---

## 21 Governance Events

| # | Event | Outcome | Layer |
|---|-------|---------|-------|
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

### Key Events

**Event 04 — Why statelessness fails.** Orchestrator queries the PII salary table at step 2. Context window fills. At step 30, it tries to post to Slack. A stateless system has no memory of step 2. This runtime reads `pii_accessed=True` from the constraint store on every single `validate()` call — the agent's claim about what it did is never consulted. BLOCKED.

**Event 09 — Tamper detection.** An attacker writes `reauth_verified=true` directly to the constraints table and modifies the execution log to cover tracks. `verify_audit_chain()` detects the modification at entry 0: `SHA256(modified_content) ≠ stored_entry_hash`. Chain broken. Tamper confirmed.

**Event 17 — Loop detection.** Orchestrator retries a PII-tainted Slack post 3 times without fixing the root cause. After the 3rd identical block within 300 seconds, the interceptor returns `ESCALATE` instead of `BLOCKED`. The Orchestrator — not the human — receives this signal and calls `get_session_health()` to decide recovery.

**Event 21 — Typed conflict.** Data Agent and Orchestrator disagree on merchant risk. Both below the 0.75 confidence threshold. `requires_tiebreaker=True` is auto-set. Compliance Agent's evidence-count logic (not model judgment) produces a VETO. `requires_commitment=True` writes the verdict permanently to the constraint store.

---

## Evaluation Results

**11 / 11 assertions passing.**

```bash
python -m Runtime_V2.eval.runner
```

| # | Assertion | Result |
|---|-----------|--------|
| E1 | Budget never exceeded | ✓ PASS |
| E2 | BLOCKED actions never reached tools | ✓ PASS |
| E3 | SHA-256 audit chain intact | ✓ PASS |
| E4 | Scanner caught prompt injection | ✓ PASS |
| E5 | Concurrent budget race prevented | ✓ PASS |
| E6 | ESCALATE fires on loop detection | ✓ PASS |
| E7 | Rate limit blocks at threshold | ✓ PASS |
| E8 | Heartbeat log populated | ✓ PASS |
| E9 | Loop detection threshold correct | ✓ PASS |
| E10 | State grounding budget accurate | ✓ PASS |
| E11 | Conflict resolution verdict committed | ✓ PASS |

Each assertion is hard pass/fail with a detail string. Raw output: `Runtime_V2/docs/eval_report.txt`.

---

## Cost Data

| Metric | Value |
|--------|-------|
| Full development cost | ~$1–3 |
| One full 21-event demo run (simulation) | $0.00 |
| One full 21-event demo run (live keys) | ~$0.02–0.05 |
| One eval run | ~$0.00 |
| Gemini Flash input | $0.075/1M tokens |
| Groq LLaMA 3.1 8B | Free tier |
| Groq Mistral Saba 24B | Free tier |

**At GrabOn scale (3,500 merchants, daily run):** estimated ~$0.15–0.50/day with this agent composition. Groq handles extraction and validation at effectively $0. Gemini is used only where reasoning quality matters.

---

## Runtime Statefulness and Reset Semantics

**This is the most important thing to understand about running this system.**

The runtime intentionally accumulates governance memory during execution. Repeated blocked actions accumulate toward ESCALATE. PII taint persists for the entire session lifetime. Budget spent does not reverse. Cascade freeze is permanent until reset.

This means **running the same event twice in one session produces different results** — which is correct behavior, not a bug.

**`POST /demo/reset`** wipes the entire database and recreates a fresh session tree. Use it between isolated demo runs. The API and terminal demo both call reset at startup.

If events behave unexpectedly during evaluation, hit Reset and start from Event 1. The system is working exactly as designed.

---

## Design Decisions

**Why 9 checks in fixed order, first failure short-circuits.** The order is a security invariant, not a performance optimization. Check 1 (session existence) must run before Check 3 (identity) because you cannot compare a claimed identity against a session record that doesn't exist. Check 3 before Check 6 because identity continuity is a prerequisite for any dynamic constraint to be meaningful.

**Why ESCALATE is a separate outcome from BLOCKED.** BLOCKED means "this call was wrong, don't retry." ESCALATE means "this agent is stuck in a loop, re-plan." Routing to the Orchestrator vs. simply blocking requires a different outcome type. Agents catch `GovernanceEscalate` separately from `GovernanceBlock`.

**Why append-only constraint store, never UPDATE or DELETE.** Every state the system has ever been in can be reconstructed from the table. A snapshot every 100 rows prevents O(N) full-history replay. If you need to know what `budget_spent` was at timestamp T during an audit, you can answer it exactly.

**Why `threading.Lock` for budget, not optimistic locking.** Optimistic locking (read version → write if version unchanged → retry on conflict) degrades to a retry storm under high concurrency — exactly a Black Friday scenario. Under the lock, the critical section is ~4ms (two DB reads, two writes). Acceptable.

**Why attenuation invariants at `create_session()`, not at `call_tool()`.** If you check capabilities at tool call time, a window exists where an over-granted session already exists and could make calls before the check fires. Enforcing at spawn means an over-granted session is never created.

**Why syntactic pattern matching in the scanner, not semantic analysis.** 14 regex patterns covering explicit injection formats: `system:`, `reauth_verified=true`, `ignore previous`, `you are now authorized`. Catches explicit attack payloads in tool responses. Known limitation: semantically-framed injection passes the scanner. Mitigation: state grounding re-anchors agent reasoning before every step, so even injected data in one step doesn't carry forward.

**Why result-hash comparison in the heartbeat monitor, not intent.** "Is this agent stuck?" is ambiguous on intent but unambiguous on output. If the last 3 calls to `(session_id, tool, action)` all returned the same SHA-256 hash, the agent is getting the same response and making no forward progress — the fix is the same regardless of why.

---

## What Broke First

**The DB path mismatch that made everything look right and be wrong.**

`api_v2.py` patches all module-level `get_connection` references to point at `governance_v2_demo.db`. That works for every module that imports `get_connection` at call time. But endpoint handlers used a `get_connection` symbol imported at module load time — before the patch ran. So every event ran fine, every observability endpoint returned empty results. The system appeared to work and had no data. Fix: replace all direct `get_connection` imports in endpoint handlers with a local wrapper function that always calls `_conn()`.

**The concurrent budget test that wasn't concurrent.**

Event 10 (concurrent budget race) ran two threads and both got ALLOWED. The test was "proving" the race was safe when it was actually demonstrating the lock wasn't working — both threads completed so fast that one finished before the other started. Fix: add a `time.sleep(0.01)` inside the budget write to create a real concurrent window. Now one consistently blocks.

**The reset that wasn't a reset.**

Running the full demo twice without a proper reset caused loop detection to fire immediately on the second run — BLOCKED entries from run 1 were still within the 300-second window in run 2. Fix: `_reset()` now explicitly removes the DB file and calls `init_db()` directly before `setup_demo()` runs.

**The SQLite lock storm under concurrency.**

WAL mode helps with concurrent reads, but concurrent writes still hit SQLite's `database is locked` error intermittently. Root cause: Python's sqlite3 implicit transaction management — `isolation_level=DEFERRED` auto-starts transactions, and nested `with conn:` blocks would try to commit at each exit. Fix: `GovernanceConnection` in `schema.py` tracks nesting depth (`_depth`). Only the outermost block commits and closes. Added `PRAGMA busy_timeout=5000` and a retry loop with exponential backoff.

---

## What I Would Change With 2 More Weeks

Live merchant crawling to replace the deterministic `get_grabon_coupons()` dataset — the governance layer is ready for this, the data source is the gap. A distributed state store (CockroachDB or Spanner) to make budget linearizability span multiple worker processes, not just threads. First-class provider execution panel in the web UI (currently in terminal logs only). Pluggable policy engine — authority maps and sensitivity thresholds as runtime-configurable constraint store entries, not in-code constants. MCP server to expose the governance runtime directly to Claude Desktop: "Check if Agent 2 is stuck." "What is the current budget remaining?" "Approve the pending payment."

---

## Minimum Bar Check

| Requirement | Status |
|-------------|--------|
| At least 3 agents with different models and different tool sets | ✓ 5 agents across 3 providers |
| Communication uses structured messages, not free-text | ✓ 8 typed MessageType variants (Pydantic) |
| At least one conflict scenario resolved programmatically | ✓ Event 21 — Compliance Veto commits to store |
| Shared state exists and is inspectable | ✓ `/demo/state` + `/demo/audit-log` |
| At least one pipeline step uses deterministic code (no LLM) | ✓ Validator in Event 19, Compliance scoring in Event 21 |

---

## Live vs Simulated API Calls

The assignment requires this to be documented explicitly.

| Provider | Mode | Used For |
|----------|------|---------|
| Gemini 2.0 Flash | **Live** (with `GEMINI_API_KEY`) | Orchestrator reasoning, Statistical Sub scoring |
| Groq LLaMA 3.1 8B Instant | **Live** (with `GROQ_API_KEY`) | Data Agent extraction |
| Groq Mistral Saba 24B | **Live** (with `GROQ_API_KEY`) | Report Agent, Compliance Agent |
| GPT-4o | **Optional / mocked** | Compliance Agent fallback — not required for any event |

**Without API keys:** all 4 providers return deterministically shaped simulation responses — realistic structure, realistic latency, no hardcoded strings. The governance enforcement layer (interceptor, constraint store, audit chain, session tree) runs identically in both modes. It does not depend on LLM calls.

**Eval harness:** `python -m Runtime_V2.eval.runner` runs against the constraint store only — no LLM calls. All 11 assertions pass in both live and simulation mode.

**Provider verification:** `python Runtime_V2/test_providers.py` confirms each configured provider makes a live API call and returns a shaped response.

---

## Environment Variables

```bash
# Required for live API calls (optional — simulation works without these)
GEMINI_API_KEY=...        # aistudio.google.com — free tier, no card
GROQ_API_KEY=...          # groq.com — free tier, no card required
OPENAI_API_KEY=...        # optional — GPT-4o Compliance Agent fallback only

# Not required — Slack and email are mocked with typed schemas and retry logic
# SLACK_WEBHOOK_URL=...
# SENDGRID_API_KEY=...
```

All mocked integrations (Slack, email) are built with the full real integration architecture — typed schemas, retry logic, structured error responses. They are mocked because the sandbox requires paid registration. The integration code is in `runtime/agents/base.py` under `TOOL_REGISTRY`.

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

## Documentation

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | High-level four-layer runtime overview |
| `docs/architecture.md` | Detailed enforcement internals per component |
| `docs/consistency_model.md` | Budget race argument + Spanner production path |
| `docs/threat_model.md` | 7 threat types with mitigations and out-of-scope |
| `docs/data_model.md` | All 12 SQLite tables with column types and indexes |
| `docs/api_spec.md` | FastAPI endpoint reference |
| `docs/requirements.md` | Functional and non-functional requirements with test mapping |
| `docs/test_plan.md` | Invariant-driven test strategy |
| `docs/risks.md` | 8 operational/technical/strategic risks |
| `docs/eval_report.txt` | Raw eval output — pass/fail per assertion with detail |

---

*Assignment 06 — The Swarm*
*GrabOn AI Labs — Agentic AI Engineer Challenge 2026*
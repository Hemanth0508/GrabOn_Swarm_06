# Agent Governance v2

**Deterministic, session-scoped governance enforcement across multi-model multi-agent workflows.**

The agent proposes actions. The interceptor decides whether those actions are allowed.  
The enforcement is guaranteed at execution time by infrastructure. Not by the agent. Not by the LLM.

---

## What This Is

A runtime governance system that sits between LLM agents and every tool they can call. Every action — before execution — passes through a seven-check enforcement engine that reads session state directly from a constraint store. The agent cannot negotiate with it. The agent does not know it exists.

**Core invariant:**
> No external side effect may execute unless it satisfies the current session's immutable and dynamically evolving constraint set.

---

## Why It Exists

LLM agents are stateless reasoners. They have no reliable memory of what happened in previous steps. They cannot verify identity. They cannot enforce constraints across tool boundaries. They can be manipulated through prompt injection or by a user simply saying they are someone else.

This creates a specific vulnerability: the agent becomes an unintentional identity proxy. A user tells the agent they are someone else. The agent believes them and forwards the claim to the database. The database trusts the agent. The wrong person gets the wrong data.

The interceptor closes this gap. It sits between the agent and every tool. It reads the session record directly. It enforces constraints independently of what the agent was told or what the agent believes.

---

## Agent Composition

| Agent | Model | Provider | Role |
|---|---|---|---|
| Orchestrator | Gemini Flash | Google AI Studio (free) | Plans, routes, managed healing |
| Data Agent | LLaMA 3 | Groq (free) | Scrapes and fetches raw data |
| Report Agent | Mistral | Groq (free) | Writes final structured output |
| Compliance Agent | Gemma | Groq (free) | Validates rules, flags violations |
| Statistical Sub | Gemini Flash | Spawned dynamically | Ranking and scoring |

---

## Quick Start

### 1. Install dependencies

```bash
pip install fastapi uvicorn google-generativeai groq cryptography
```

### 2. Set API keys (free tiers)

```bash
export GOOGLE_AI_API_KEY="your_key"   # aistudio.google.com
export GROQ_API_KEY="your_key"        # console.groq.com
```

Without keys, all agents run in simulation mode automatically.

### 3. Run the demo server

```bash
python api_v2.py
```

Open `http://localhost:8000` — click any of the 15 governance events to run them step by step.

### 4. Run eval assertions

```bash
python eval/runner.py
```

All 10 automated assertions report PASSED / FAILED with detail.

### 5. View live database

```bash
pip install datasette
datasette governance_v2_demo.db
```

Open `http://localhost:8001` — browse sessions, constraints, execution_log, heartbeats live as the demo runs.

### 6. Interactive API explorer

With the server running, open `http://localhost:8000/docs` — full Swagger UI, try every endpoint directly.

---

## The Five Demo Scenarios (Original v1)

| Scenario | What It Proves |
|---|---|
| Identity Impersonation (Eve & Sasha) | Agent cannot be used as an identity proxy |
| Data Taint Propagation (Alice) | Constraint written by tool in step 1 governs tool in step 3 |
| Concurrent Budget Race (Bob) | WAL lock prevents double-spend under true concurrency |
| Session Expiry (Carol) | Identity match is not sufficient on an expired session |
| Re-authentication Gate (Dave) | Agent assertions about constraint state are irrelevant |

## Extended Demo (v2 — 15 Events)

Events 6-15 cover cascade freeze, taint inheritance, multi-agent coordination, loop detection, ESCALATE signal, managed healing, and eval assertions.

---

## Repository Structure

```
agent-governance-v2/
├── README.md
├── ARCHITECTURE.md
├── requirements.txt
├── runtime/
│   ├── schema.py
│   ├── agents/
│   │   ├── base.py
│   │   └── scanner.py
│   ├── constraints/
│   │   └── store.py
│   ├── identity/
│   │   └── principals.py
│   ├── interceptor/
│   │   └── validate.py
│   ├── session/
│   │   └── sessions.py
│   └── monitor/
│       ├── heartbeat.py
│       └── live.py
├── eval/
│   ├── assertions.py
│   └── runner.py
├── demo/
│   ├── five_agent_demo.py
│   ├── api_v2.py
│   └── ui_v2.html
└── docs/
    ├── architecture.md
    ├── data_model.md
    ├── api_spec.md
    ├── consistency_model.md
    ├── threat_model.md
    ├── test_plan.md
    └── risks.md
```

---

## What This System Does Not Solve

Knowing precisely what a system does not solve is as important as knowing what it does.

- Physical coercion of the authenticated principal
- OS-level infrastructure compromise
- Cryptographic token forgery without signed tokens in place
- Insider threats with database administrator access
- Semantically correct but maliciously framed data that influences agent reasoning without explicit injection patterns — unsolvable at the syntactic scanning layer

These are outside scope by design. The threat model document is explicit about these boundaries.

---

## Author

Hemanth Porapu — [github.com/Hemanth0508](https://github.com/Hemanth0508)

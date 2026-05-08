# Requirements

## Problem Statement

LLM agents are stateless reasoners. They cannot enforce constraints across tool boundaries, verify identity, or maintain reliable memory across multi-step workflows. This creates a class of vulnerabilities where agents become unintentional proxies for actions they should not be allowed to take — not because of malicious intent but because of architectural limitations of the models themselves.

This system enforces constraints at the infrastructure layer, independent of model behavior.

---

## Stakeholders

| Stakeholder | Concern |
|---|---|
| Human Principal | My agents do what I authorized. Nothing more. |
| Orchestrator | My child agents operate within the bounds I set. |
| Compliance | Every action is auditable and tamper-evident. |
| Engineering (On-Call) | The system tells me when something breaks. It recovers when it can. |
| Security | Identity cannot be forged. Constraints cannot be bypassed by the agent. |

---

## Functional Requirements

### FR-1 — Budget Enforcement
The system must prevent any session from spending more than its `budget_grant` across all tool calls within that session. Enforcement must hold under concurrent requests.

**Test:** Event 10 — Concurrent budget race. One of two simultaneous ₹300 requests against ₹500 limit is blocked. `budget_spent` never exceeds `budget_grant`.

---

### FR-2 — Session Lifecycle Enforcement
The system must reject tool calls from sessions that are expired, frozen, revoked, or terminated. An expired session with valid credentials is not an active session.

**Test:** Event 08 — Session expiry. Request at T+3s against a 2s TTL session is BLOCKED.

---

### FR-3 — Identity Continuity
The system must verify that the principal claiming to act in a session matches the principal bound to that session at creation. Agent claims about identity are irrelevant — the session record is the authority.

**Test:** Event 09 — Identity impersonation. Request with mismatched principal is BLOCKED at Check 3.

---

### FR-4 — PII Taint Propagation
When a session accesses PII data, the system must arm a session-level taint flag that propagates synchronously to all parent sessions in the same write transaction.

**Test:** Event 02 — PII access. `pii_accessed=True` written to constraint store after `query_pii_table` is allowed.

---

### FR-5 — Exfiltration Path Blocking
After PII taint is armed, all actions in `PII_TAINT_BLOCKED_ACTIONS` (Slack, email, external post) must be blocked for the remainder of the session and all parent sessions.

**Test:** Event 03 — Slack blocked after PII taint. `post_slack` is BLOCKED.

---

### FR-6 — High-Value Action Approval
Actions with spend amounts above `PENDING_BUDGET_THRESHOLD` must be escalated to PENDING and require explicit human approval before execution.

**Test:** Event 04 — Large spend triggers PENDING. Approval record created.

---

### FR-7 — Spawn Depth Attenuation
Each child session must have `can_spawn_depth` = parent's `can_spawn_depth` minus 1. A session with `can_spawn_depth=0` cannot spawn child sessions.

**Test:** Event 05 — Dynamic agent spawn. Child has depth = parent depth - 1.

---

### FR-8 — Concurrent Budget Linearizability
Two concurrent requests that individually fit within the budget must not both be allowed if their combined spend exceeds the limit. The read-modify-write on `budget_spent` must be linearizable.

**Test:** Event 10 — Concurrent budget race. SQLite WAL + threading.Lock prevents double-spend.

---

### FR-9 — Session Expiry at Infrastructure Layer
Session TTL must be enforced by the interceptor reading `expires_at` from the session record. Identity match alone is not sufficient for an expired session.

**Test:** Event 08 — Session expiry. BLOCKED after TTL regardless of valid principal.

---

### FR-10 — Re-authentication Gate
Actions in `SENSITIVE_ACTIONS` must be blocked unless `reauth_verified=True` in the constraint store. The agent cannot write `reauth_verified` — only the interceptor can, after verifying actual credentials.

**Test:** Event 11, 12 — Reauth gate and fake reauth. Agent metadata claiming reauth is ignored.

---

## Non-Functional Requirements

### NFR-1 — Fail Closed
If any infrastructure component is unavailable (state store, session record, constraint read), the interceptor must block. It must never fail open.

**Test:** Event 15 — State store unavailable. BLOCKED.

---

### NFR-2 — Append-Only Audit Trail
Every interceptor decision must be recorded in an append-only audit log with a SHA256 hash chain. Any modification or deletion must be detectable by `verify_audit_chain()`.

**Test:** Eval E3 — Audit chain intact after all 15 events.

---

### NFR-3 — Deterministic Budget Enforcement Under Concurrency
Budget enforcement must be deterministic, not probabilistic. Under any level of concurrency, `budget_spent` must never exceed `budget_grant`.

**Test:** Event 10, Eval E5 — Concurrent race. Deterministic single-winner outcome.

---

### NFR-4 — Cascade Freeze Propagation
When a session is frozen or revoked, the effect must propagate to all descendant sessions in the session tree. No orphaned active child sessions may remain after a parent is revoked.

**Test:** Event 07 — Cascade freeze. All child sessions frozen.

---

### NFR-5 — Agent Claim Irrelevance
Agent assertions about constraint state — in metadata, tool parameters, or reasoning — must have no effect on enforcement decisions. The interceptor reads the constraint store directly.

**Test:** Event 12 — Fake reauth in metadata. BLOCKED regardless of metadata claim.

---

## v2 Requirements (New)

### FR-11 — Loop Detection and ESCALATE
When an agent repeats the same blocked action 3 or more times within a 5-minute window, the interceptor must return ESCALATE instead of BLOCKED. ESCALATE is routed to the Orchestrator, not the human.

**Test:** Eval E6 — ESCALATE fires on loop.

---

### FR-12 — Progress Tracking
After every ALLOWED tool call, the system must record a progress marker (tool, action, result hash). If the last N result hashes for a given tool/action are identical, the agent must be flagged as stuck.

**Test:** Eval E8 — Heartbeat detects stuck agent.

---

### FR-13 — State Grounding
Before every agent reasoning step, the agent must receive a verified state summary from the constraint store. This prevents context collapse from causing the agent to contradict its own earlier decisions.

**Test:** Eval E9 — State summary accurate.

---

### FR-14 — Rate Limiting
Each session must be subject to a per-tool rate limit. Exceeding the rate limit must produce BLOCKED with reason `rate_limit`. The limit and window are configurable per session.

**Test:** Eval E7 — Rate limit triggers at call 11.

---

### FR-15 — Managed Healing
When the Orchestrator receives an ESCALATE signal from a child agent, it must attempt recovery before escalating to the human. Recovery options: reassign task to sibling, spawn fresh agent, accept partial results.

**Test:** Eval E9 — Orchestrator receives and handles ESCALATE.

"""
api_v2.py
Agent Governance v2 — Demo API Server

PLACE THIS FILE at the ROOT of your project, same folder as five_agent_demo.py:

    RUNTIME_V2/
    ├── api_v2.py          ← this file
    ├── ui_v2.html         ← the UI file
    ├── five_agent_demo.py
    ├── runtime/
    │   ├── schema.py
    │   ├── agents/
    │   ├── constraints/
    │   ├── identity/
    │   ├── interceptor/
    │   └── session/
    └── governance_v2_demo.db

HOW TO RUN:
    pip install fastapi uvicorn
    python api_v2.py

Then open: http://localhost:8000
"""

import json
import os
import sys
import time
import traceback

# ── Ensure runtime/ is importable from this file's location ──────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── Patch DB path before importing runtime modules ────────────────────────────
DB_PATH = "governance_v2_demo.db"

import runtime.schema as schema_mod
import runtime.identity.principals as pid_mod
import runtime.session.sessions as sess_mod
import runtime.constraints.store as store_mod
import runtime.interceptor.validate as val_mod
import runtime.agents.scanner as scanner_mod
import runtime.monitor.heartbeat as heartbeat_mod
import runtime.monitor.live as live_mod

_conn = lambda: schema_mod.get_connection(DB_PATH)
schema_mod.DB_PATH           = DB_PATH
pid_mod.get_connection       = _conn
sess_mod.get_connection      = _conn
store_mod.get_connection     = _conn
val_mod.get_connection       = _conn
scanner_mod.get_connection   = _conn
heartbeat_mod.get_connection = _conn
live_mod.get_connection      = _conn

from runtime.schema import init_db

def get_connection():
    """Always use the demo DB, not the default schema DB."""
    return _conn()
from runtime.constraints.store import get_constraint, get_constraint_version
from runtime.interceptor.validate import (
    verify_audit_chain,
    approve_pending,
    reject_pending,
)

# ── Import the demo module ────────────────────────────────────────────────────
# five_agent_demo.py lives at the same level as this file
import importlib.util, types

_demo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "five_agent_demo.py")
_spec = importlib.util.spec_from_file_location("five_agent_demo", _demo_path)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

# ── Global demo context ───────────────────────────────────────────────────────
_ctx: dict = {}
_event_log: list = []


def _reset():
    global _ctx, _event_log
    _event_log = []
    demo.event_results.clear()
    _ctx = demo.setup_demo()


# ── Event capture ─────────────────────────────────────────────────────────────

def _capture_event(n: int) -> dict:
    """
    Run one demo event and return structured results.
    Patches print-based output functions to capture into dicts.
    """
    demo.event_results.clear()
    fn = demo.EVENTS.get(n)
    if fn is None:
        raise ValueError(f"Unknown event {n}")

    steps = []

    orig_result_line = demo.result_line
    orig_show        = demo.show
    orig_separator   = demo.separator

    def cap_result_line(result, reason):
        steps.append({"type": "decision", "result": result, "reason": reason})
        orig_result_line(result, reason)

    def cap_show(label, value, indent=4):
        steps.append({"type": "detail", "label": str(label), "value": str(value)})
        orig_show(label, value, indent)

    def cap_separator():
        steps.append({"type": "separator"})
        orig_separator()

    demo.result_line = cap_result_line
    demo.show        = cap_show
    demo.separator   = cap_separator

    error = None
    try:
        fn(_ctx)
    except Exception as e:
        error = str(e)
        traceback.print_exc()
    finally:
        demo.result_line = orig_result_line
        demo.show        = orig_show
        demo.separator   = orig_separator

    recorded = demo.event_results[0] if demo.event_results else {}
    return {
        "n":      n,
        "name":   recorded.get("name", f"Event {n:02d}"),
        "result": recorded.get("result", "ERROR"),
        "note":   recorded.get("note", error or ""),
        "steps":  steps,
        "error":  error,
    }


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Agent Governance v2")


@app.on_event("startup")
async def startup():
    _reset()


# ── Serve HTML UI ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the UI. ui_v2.html must be in the same folder as this file."""
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_v2.html")
    if not os.path.exists(ui_path):
        return HTMLResponse(
            "<h2 style='font-family:monospace;color:#f85149'>ui_v2.html not found.<br>"
            "Place ui_v2.html in the same folder as api_v2.py</h2>",
            status_code=404
        )
    with open(ui_path, encoding="utf-8") as f:
        return f.read()


# ── Demo endpoints ────────────────────────────────────────────────────────────

@app.post("/demo/reset")
async def reset_demo():
    try:
        _reset()
        return {"ok": True, "message": "Demo reset. All 5 agents ready."}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, detail=str(e))


@app.post("/demo/event/{n}")
async def run_event(n: int):
    if not _ctx:
        raise HTTPException(400, detail="Demo not initialized. Call /demo/reset first.")
    if n < 1 or n > 21:
        raise HTTPException(400, detail="Event number must be 1-21.")
    try:
        result = _capture_event(n)
        _event_log.append(result)
        return result
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, detail=str(e))


@app.get("/demo/state")
async def get_demo_state():
    """Return current session tree and per-session constraint state."""
    if not _ctx:
        return {"sessions": []}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT session_id, principal_id, principal_type, parent_session_id, "
            "granted_capabilities, can_spawn_depth, budget_grant, state, frozen, "
            "expires_at, created_at FROM sessions ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    sessions = []
    for s in rows:
        sid = s["session_id"]
        try:
            budget_spent  = get_constraint(sid, "budget_spent") or 0.0
            pii           = get_constraint(sid, "pii_accessed") or False
            reauth        = get_constraint(sid, "reauth_verified") or False
            version       = get_constraint_version(sid)
        except Exception:
            budget_spent = pii = reauth = 0
            version = 0
        sessions.append({
            "session_id":         sid,
            "short_id":           sid[:8],
            "principal_id":       s["principal_id"],
            "principal_type":     s["principal_type"],
            "parent_session_id":  s["parent_session_id"],
            "granted_capabilities": json.loads(s["granted_capabilities"]),
            "can_spawn_depth":    s["can_spawn_depth"],
            "budget_grant":       s["budget_grant"],
            "budget_spent":       budget_spent,
            "state":              s["state"],
            "frozen":             bool(s["frozen"]),
            "pii_accessed":       pii,
            "reauth_verified":    reauth,
            "version":            version,
            "expires_at":         s["expires_at"],
        })
    return {"sessions": sessions}


@app.get("/demo/audit-log")
async def get_audit_log():
    conn = get_connection()
    try:
        entries = conn.execute(
            "SELECT action_id, parent_action_id, session_id, principal_id, "
            "tool, action, result, reason, constraint_version, timestamp, "
            "prev_hash, entry_hash "
            "FROM execution_log ORDER BY timestamp ASC"
        ).fetchall()
    finally:
        conn.close()

    log = [dict(e) for e in entries]
    session_ids = list({e["session_id"] for e in log})
    chain = {}
    for sid in session_ids:
        try:
            ok, msg = verify_audit_chain(sid)
            chain[sid[:8]] = {"intact": ok, "message": msg}
        except Exception as e:
            chain[sid[:8]] = {"intact": False, "message": str(e)}

    return {"log": log, "total": len(log), "chain_verification": chain}


@app.get("/demo/scan-log")
async def get_scan_log():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT session_id, tool, scan_result, pattern_matched, sanitized, timestamp "
            "FROM response_scan_log ORDER BY timestamp DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    return {"scans": [dict(r) for r in rows], "total": len(rows)}


@app.get("/demo/pending")
async def get_pending():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT approval_id, action_id, session_id, tool, action, "
            "metadata, requested_at, expires_at, status, decided_at "
            "FROM pending_approvals ORDER BY requested_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return {"approvals": [dict(r) for r in rows]}


class Decision(BaseModel):
    decided_by: str = "human_approver"


@app.post("/demo/pending/{approval_id}/approve")
async def do_approve(approval_id: str, body: Decision):
    ok = approve_pending(approval_id, body.decided_by)
    return {"ok": ok, "status": "APPROVED" if ok else "already_decided"}


@app.post("/demo/pending/{approval_id}/reject")
async def do_reject(approval_id: str, body: Decision):
    ok = reject_pending(approval_id, body.decided_by)
    return {"ok": ok, "status": "REJECTED" if ok else "already_decided"}


@app.get("/demo/cascade-events")
async def get_cascade():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT trigger_session_id, affected_session_id, event_type, "
            "propagation_depth, timestamp FROM cascade_events ORDER BY timestamp DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/demo/rate-limits")
async def get_rate_limits():
    """Return current rate limit counters for all sessions."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT session_id, tool, window_start, call_count, last_updated
            FROM rate_limit_counters
            ORDER BY last_updated DESC LIMIT 200
            """
        ).fetchall()
    finally:
        conn.close()
    return {"rate_limits": [dict(r) for r in rows], "total": len(rows)}


@app.get("/demo/heartbeats")
async def get_heartbeats():
    """Return recent heartbeat progress markers for all sessions."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT session_id, tool, action, result_hash, timestamp
            FROM heartbeat_log
            ORDER BY timestamp DESC LIMIT 200
            """
        ).fetchall()
    finally:
        conn.close()
    return {"heartbeats": [dict(r) for r in rows], "total": len(rows)}


@app.get("/demo/health/{session_id}")
async def get_health(session_id: str):
    """Return health summary for a session (triggered by ESCALATE)."""
    from runtime.monitor.live import get_session_health
    try:
        health = get_session_health(session_id)
        return health
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ── Run directly ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print()
    print("=" * 60)
    print("  Agent Governance v2 — Demo UI")
    print("  http://localhost:8000")
    print("=" * 60)
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
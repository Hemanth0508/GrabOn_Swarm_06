"""
runtime/schema.py
Agent Governance v2 — Database Schema

Initializes all 12 tables exactly as specified in the design document.
No business logic. Schema and indexes only.
Idempotent — safe to call multiple times on an existing database.

Tables:
    principals           — every principal (human or agent), immutable type + public key
    sessions             — session tree, lifecycle state, capability grants
    constraints          — append-only constraint store, version-tracked, signed
    constraint_snapshots — periodic snapshots for fast reconstruction
    principal_ledger     — cross-session aggregate metrics per principal
    execution_log        — every interceptor decision, hash-chained, causal graph
    pending_approvals    — actions awaiting human approval (PENDING outcome)
    response_scan_log    — tool result injection scan records
    cascade_events       — orphan/termination cascade records
    idempotency_keys     — deduplication table for retry safety
    rate_limit_counters  — per-session per-tool call counts for rate limiting (NEW)
    heartbeat_log        — per-session progress markers for stuck-agent detection (NEW)
"""

import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = "governance_v2.db"

# SQLite concurrency hardening knobs.
SQLITE_BUSY_TIMEOUT_MS = 5000
SQLITE_LOCK_RETRIES = 6
SQLITE_RETRY_BASE_DELAY_S = 0.05

# Process-wide write lock to serialize write transactions.
_DB_WRITE_LOCK = threading.RLock()

_WRITE_PREFIXES = (
    "insert", "update", "delete", "replace",
    "create", "drop", "alter", "pragma", "begin",
    "commit", "rollback", "vacuum", "reindex",
)


def _is_write_sql(sql: str) -> bool:
    if not isinstance(sql, str):
        return False
    s = sql.lstrip().lower()
    return s.startswith(_WRITE_PREFIXES)


def _is_lock_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


class GovernanceConnection(sqlite3.Connection):
    """
    SQLite connection with lock retry, write serialization, and guaranteed close
    on context-manager exit.
    """
    def _retrying(self, fn, sql: str | None = None, *args, **kwargs):
        is_write = _is_write_sql(sql or "")
        for attempt in range(SQLITE_LOCK_RETRIES + 1):
            lock_cm = _DB_WRITE_LOCK if is_write else _NullContext()
            with lock_cm:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not _is_lock_error(exc) or attempt >= SQLITE_LOCK_RETRIES:
                        raise
            time.sleep(SQLITE_RETRY_BASE_DELAY_S * (attempt + 1))
        raise RuntimeError("unreachable retry loop")

    def execute(self, sql, parameters=(), /):
        return self._retrying(lambda: super().execute(sql, parameters), sql)

    def executemany(self, sql, seq_of_parameters, /):
        return self._retrying(lambda: super().executemany(sql, seq_of_parameters), sql)

    def executescript(self, sql_script, /):
        return self._retrying(lambda: super().executescript(sql_script), sql_script)

    def commit(self):
        return self._retrying(lambda: super().commit(), "commit")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            sqlite3.Connection.__exit__(self, exc_type, exc, tb)
        finally:
            self.close()
        return False


class _NullContext:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc, tb):
        return False


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Return a serializable connection to the governance state store.
    WAL mode for concurrent read performance.
    Foreign keys enforced on every connection.
    """
    conn = sqlite3.connect(
        db_path,
        isolation_level="DEFERRED",
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        factory=GovernanceConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """
    Initialize the v2 governance database.

    Creates all 12 tables and their indexes if they do not exist.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS throughout.
    Does not modify existing tables or data.

    Args:
        db_path: Path to the SQLite database file. Defaults to governance_v2.db.
    """
    with get_connection(db_path) as conn:
        # ──────────────────────────────────────────────────────────────
        # TABLE 1 — principals
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS principals (
                id           TEXT PRIMARY KEY,
                principal_type TEXT NOT NULL CHECK (
                    principal_type IN (
                        'HUMAN',
                        'ORCHESTRATOR',
                        'AGENT',
                        'SUBAGENT',
                        'COMPLIANCE',
                        'INTERCEPTOR'
                    )
                ),
                public_key   TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_principals_type
                ON principals(principal_type)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 2 — sessions
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id           TEXT PRIMARY KEY,
                principal_id         TEXT NOT NULL,
                principal_type       TEXT NOT NULL CHECK (
                    principal_type IN (
                        'HUMAN',
                        'ORCHESTRATOR',
                        'AGENT',
                        'SUBAGENT',
                        'COMPLIANCE',
                        'INTERCEPTOR'
                    )
                ),
                parent_session_id    TEXT,
                granted_capabilities TEXT NOT NULL DEFAULT '[]',
                can_spawn_depth      INTEGER NOT NULL DEFAULT 0,
                budget_grant         REAL NOT NULL DEFAULT 0.0,
                grace_period_seconds INTEGER NOT NULL DEFAULT 30,
                autonomous_flag      INTEGER NOT NULL DEFAULT 0,
                expires_at           TEXT NOT NULL,
                active               INTEGER NOT NULL DEFAULT 1,
                frozen               INTEGER NOT NULL DEFAULT 0,
                state                TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
                    state IN (
                        'ACTIVE',
                        'FROZEN',
                        'TERMINATED',
                        'PENDING_APPROVAL',
                        'REVOKED'
                    )
                ),
                created_at           TEXT NOT NULL,
                FOREIGN KEY (principal_id)
                    REFERENCES principals(id),
                FOREIGN KEY (parent_session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_principal
                ON sessions(principal_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_parent
                ON sessions(parent_session_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_state
                ON sessions(state, active)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_created_at
                ON sessions(created_at)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 3 — constraints
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS constraints (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id           TEXT NOT NULL,
                constraint_key       TEXT NOT NULL,
                constraint_value     TEXT NOT NULL,
                written_by_principal TEXT NOT NULL,
                priority_level       INTEGER NOT NULL DEFAULT 2,
                valid_from           TEXT NOT NULL,
                valid_until          TEXT,
                idempotency_key      TEXT UNIQUE,
                signature            TEXT,
                version              INTEGER NOT NULL,
                set_at               TEXT NOT NULL,
                UNIQUE(session_id, version),
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id),
                FOREIGN KEY (written_by_principal)
                    REFERENCES principals(id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_constraints_session_key_version
                ON constraints(session_id, constraint_key, priority_level DESC, version DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_constraints_session_version
                ON constraints(session_id, version DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_constraints_priority
                ON constraints(session_id, constraint_key, priority_level DESC, set_at DESC)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 4 — constraint_snapshots
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS constraint_snapshots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id           TEXT NOT NULL,
                snapshot_at_version  INTEGER NOT NULL,
                full_state           TEXT NOT NULL,
                created_at           TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_session_version
                ON constraint_snapshots(session_id, snapshot_at_version DESC)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 5 — principal_ledger
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS principal_ledger (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                principal_id  TEXT NOT NULL,
                metric_key    TEXT NOT NULL,
                current_value REAL NOT NULL DEFAULT 0.0,
                last_updated  TEXT NOT NULL,
                version       INTEGER NOT NULL DEFAULT 0,
                UNIQUE(principal_id, metric_key),
                FOREIGN KEY (principal_id)
                    REFERENCES principals(id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_principal_metric
                ON principal_ledger(principal_id, metric_key)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 6 — execution_log
        # result CHECK includes ESCALATE as a valid outcome.
        # ESCALATE is written when loop detection fires — it is a system
        # signal to the Orchestrator, not a tool execution decision.
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_log (
                action_id                TEXT PRIMARY KEY,
                parent_action_id         TEXT,
                session_id               TEXT NOT NULL,
                principal_id             TEXT NOT NULL,
                tool                     TEXT NOT NULL,
                action                   TEXT NOT NULL,
                result                   TEXT NOT NULL CHECK (
                    result IN ('ALLOWED', 'BLOCKED', 'PENDING', 'ESCALATE')
                ),
                reason                   TEXT NOT NULL,
                constraint_version       INTEGER NOT NULL,
                timestamp                TEXT NOT NULL,
                prev_hash                TEXT NOT NULL CHECK (length(prev_hash) = 64),
                entry_hash               TEXT NOT NULL CHECK (length(entry_hash) = 64),
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id),
                FOREIGN KEY (principal_id)
                    REFERENCES principals(id),
                FOREIGN KEY (parent_action_id)
                    REFERENCES execution_log(action_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_session_time
                ON execution_log(session_id, timestamp ASC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_result
                ON execution_log(result)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_parent_action
                ON execution_log(parent_action_id)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 7 — pending_approvals
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                approval_id  TEXT PRIMARY KEY,
                action_id    TEXT NOT NULL,
                session_id   TEXT NOT NULL,
                tool         TEXT NOT NULL,
                action       TEXT NOT NULL,
                metadata     TEXT NOT NULL DEFAULT '{}',
                requested_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'PENDING' CHECK (
                    status IN ('PENDING', 'APPROVED', 'REJECTED', 'TIMEOUT')
                ),
                decided_at   TEXT,
                decided_by   TEXT,
                FOREIGN KEY (action_id)
                    REFERENCES execution_log(action_id),
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id),
                FOREIGN KEY (decided_by)
                    REFERENCES principals(id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approvals_session_status
                ON pending_approvals(session_id, status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_approvals_expires
                ON pending_approvals(expires_at, status)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 8 — response_scan_log
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS response_scan_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                tool            TEXT NOT NULL,
                scan_result     TEXT NOT NULL CHECK (
                    scan_result IN ('CLEAN', 'BLOCKED')
                ),
                pattern_matched TEXT,
                sanitized       INTEGER NOT NULL DEFAULT 0,
                timestamp       TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_session_time
                ON response_scan_log(session_id, timestamp ASC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_result
                ON response_scan_log(scan_result)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 9 — cascade_events
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cascade_events (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_session_id   TEXT NOT NULL,
                affected_session_id  TEXT NOT NULL,
                event_type           TEXT NOT NULL CHECK (
                    event_type IN (
                        'CASCADE_FROZEN',
                        'CASCADE_TERMINATED',
                        'CASCADE_DEPTH_EXCEEDED',
                        'ORPHAN_GRACE_EXPIRED'
                    )
                ),
                propagation_depth    INTEGER NOT NULL DEFAULT 0,
                timestamp            TEXT NOT NULL,
                FOREIGN KEY (trigger_session_id)
                    REFERENCES sessions(session_id),
                FOREIGN KEY (affected_session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cascade_trigger
                ON cascade_events(trigger_session_id, timestamp ASC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cascade_affected
                ON cascade_events(affected_session_id)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 10 — idempotency_keys
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key           TEXT PRIMARY KEY,
                session_id    TEXT NOT NULL,
                action_hash   TEXT NOT NULL,
                result_cached TEXT NOT NULL,
                first_seen    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_idempotency_session
                ON idempotency_keys(session_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_idempotency_expires
                ON idempotency_keys(session_id, expires_at)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 11 — rate_limit_counters  (NEW)
        # Tracks call counts per (session_id, tool) within a rolling
        # time window. Written by the interceptor on every ALLOWED or
        # ESCALATE decision. The interceptor reads the count before
        # executing Check 7 and blocks if count >= RATE_LIMIT_MAX.
        #
        # window_start: ISO8601 timestamp of the window's start.
        # call_count:   Number of calls in this window for this tool.
        # One row per (session_id, tool, window_start).
        # Old windows are purged on every write to keep the table bounded.
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit_counters (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                tool         TEXT NOT NULL,
                window_start TEXT NOT NULL,
                call_count   INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL,
                UNIQUE(session_id, tool, window_start),
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rate_limit_session_tool
                ON rate_limit_counters(session_id, tool, window_start DESC)
        """)

        # ──────────────────────────────────────────────────────────────
        # TABLE 12 — heartbeat_log  (NEW)
        # Records a progress marker after every ALLOWED tool call.
        # result_hash = SHA256 of what the tool returned.
        # If the last N result_hashes for (session_id, tool, action)
        # are all identical, the agent is stuck — no forward progress.
        # Used by runtime/monitor/heartbeat.py is_stuck() check.
        # Separate from execution_log — does not affect audit chain.
        # ──────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heartbeat_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                tool         TEXT NOT NULL,
                action       TEXT NOT NULL,
                result_hash  TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_heartbeat_session_tool_action
                ON heartbeat_log(session_id, tool, action, timestamp DESC)
        """)

        # ──────────────────────────────────────────────────────────────
        # BOOTSTRAP — INTERCEPTOR system principal
        # ──────────────────────────────────────────────────────────────
        from datetime import datetime, timezone
        conn.execute("""
            INSERT OR IGNORE INTO principals (id, principal_type, public_key, created_at)
            VALUES ('00000000-0000-0000-0000-000000000000', 'INTERCEPTOR', '', ?)
        """, (datetime.now(timezone.utc).isoformat(),))


def drop_all(db_path: str = DB_PATH) -> None:
    """
    Drop all v2 tables in dependency-safe order.
    Used only for testing. Never call in production.
    """
    with get_connection(db_path) as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS heartbeat_log;
            DROP TABLE IF EXISTS rate_limit_counters;
            DROP TABLE IF EXISTS idempotency_keys;
            DROP TABLE IF EXISTS cascade_events;
            DROP TABLE IF EXISTS response_scan_log;
            DROP TABLE IF EXISTS pending_approvals;
            DROP TABLE IF EXISTS execution_log;
            DROP TABLE IF EXISTS principal_ledger;
            DROP TABLE IF EXISTS constraint_snapshots;
            DROP TABLE IF EXISTS constraints;
            DROP TABLE IF EXISTS sessions;
            DROP TABLE IF EXISTS principals;
        """)

def verify_schema(db_path: str = DB_PATH) -> dict:
    """
    Verify all 12 tables exist and return their column counts.
    Returns dict of {table_name: column_count}.
    Raises AssertionError if any table is missing.
    """
    expected_tables = {
        "principals":            4,
        "sessions":              14,
        "constraints":           12,
        "constraint_snapshots":  5,
        "principal_ledger":      6,
        "execution_log":         12,
        "pending_approvals":     11,
        "response_scan_log":     7,
        "cascade_events":        6,
        "idempotency_keys":      6,
        "rate_limit_counters":   6,
        "heartbeat_log":         6,
    }

    result = {}
    with get_connection(db_path) as conn:
        for table, expected_cols in expected_tables.items():
            rows = conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            assert len(rows) == expected_cols, (
                f"Table '{table}': expected {expected_cols} columns, "
                f"found {len(rows)}"
            )
            result[table] = len(rows)
    return result


if __name__ == "__main__":
    import os

    db = "governance_v2.db"

    if os.path.exists(db):
        os.remove(db)
        print(f"Removed existing {db}")

    print("Initializing schema...")
    init_db(db)

    print("Verifying schema...")
    counts = verify_schema(db)

    print()
    print(f"{'Table':<26} {'Columns':>7}")
    print("-" * 36)
    for table, cols in counts.items():
        print(f"  {table:<24} {cols:>7}")

    print()
    print(f"All 12 tables verified. Database: {db}")

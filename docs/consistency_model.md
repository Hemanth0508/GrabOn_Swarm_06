# Consistency Model

## The Core Requirement

Budget enforcement requires that no two concurrent agents can both read a value, both conclude they are within limit, and both execute. This requires **linearizable reads** — every read must reflect the most recent completed write, globally, across all concurrent operations.

This is a strong consistency requirement. Most databases do not provide it by default.

---

## Why Eventual Consistency Breaks This System

### The Race Condition

Consider two agents, A and B, each requesting ₹300 against a ₹500 limit with an eventually consistent store:

```
Agent A reads:  budget_spent = 0   (from replica 1, up to date)
Agent B reads:  budget_spent = 0   (from replica 2, not yet synced)

Agent A checks: 0 + 300 = 300 ≤ 500 → passes
Agent B checks: 0 + 300 = 300 ≤ 500 → passes

Agent A writes: budget_spent = 300
Agent B writes: budget_spent = 300  (last write wins, or merge conflict)

Actual spend:   ₹600
Enforced limit: ₹500
Violation:      silent, undetected, irreversible
```

The enforcement log records two ALLOWED decisions. The audit chain is intact. Everything looks correct. The budget constraint was violated without any detectable event.

### Why This Is Not Fixable with Retries

Retries under eventual consistency only reduce the window for the race condition. They do not eliminate it. Two requests that arrive within the replication lag window will both read stale state and both pass, regardless of how many retries occur afterward.

### Why Optimistic Locking Is Insufficient

Optimistic locking (read version, write if version unchanged, retry if version changed) works when the window between read and write is short and retries converge quickly. Under high concurrency — which is exactly the Black Friday scenario — optimistic locking degrades to a retry storm. Agents compete, retry, compete again, and exhaust their budgets retrying rather than doing work.

---

## What This System Uses

### SQLite WAL Mode (Prototype)

SQLite with WAL (Write-Ahead Logging) mode provides:
- **Serializable writes:** Only one writer at a time. Concurrent readers do not block writers. Writers do not block readers.
- **Read isolation:** Readers see a consistent snapshot of the database at the start of their read.
- **Durability:** WAL entries are synced before commit returns.

Combined with `threading.Lock` (`_budget_lock`) in the application layer, this gives the prototype linearizable budget enforcement:

```python
with _budget_lock:              # Only one thread enters the budget path at a time
    conn = get_connection()
    with conn:                  # SQLite WAL transaction
        current = read budget_spent   # Reads the latest committed value
        if current + amount > limit:
            write BLOCKED to execution_log
            return BLOCKED
        write budget_spent = current + amount
        write ALLOWED to execution_log
        return ALLOWED
```

No two threads can be inside `_budget_lock` simultaneously. The read always sees the most recent write. The race condition is eliminated.

### Limitation

SQLite is single-process. The `threading.Lock` only serializes threads within one Python process. If the API server is scaled horizontally to multiple processes, the lock no longer works — two processes can each acquire their own lock, both read the same stale value, and both pass.

---

## Production Path

At production scale, the constraint store should be replaced with a database that provides linearizable reads natively, without application-layer locking.

### Google Cloud Spanner

Spanner uses TrueTime — a globally synchronized clock backed by GPS and atomic clocks — to provide **external consistency**: a stronger guarantee than linearizability. Every read in Spanner reflects all writes that completed before the read started, globally, across all nodes, without application-layer coordination.

For budget enforcement, this means:
- No `threading.Lock` needed
- No single-process limitation
- Horizontal scaling with no race condition window
- The enforcement guarantee holds whether you have 5 agents or 5,000

### The Architecture Argument

The choice of database is not an implementation detail for this system. It is an architectural requirement. The consistency model of the database directly determines whether the budget enforcement guarantee holds under concurrency.

A system that uses an eventually consistent store (DynamoDB, Cassandra, CockroachDB in snapshot isolation mode) and claims to enforce budget constraints is making a false claim. The enforcement is probabilistic, not deterministic. It will fail under sufficient concurrency.

Spanner is the only managed offering with the consistency level required to make the enforcement deterministic. This prototype demonstrates exactly why.

---

## Constraint Store Consistency Requirements by Operation

| Operation | Consistency Required | Current Solution | Production Solution |
|---|---|---|---|
| Budget read-modify-write | Linearizable | threading.Lock + WAL | Spanner read-write transaction |
| Taint write | Serializable | WAL transaction | Spanner read-write transaction |
| Session state read | Read-your-writes | WAL snapshot | Spanner strong read |
| Audit log append | Durable, ordered | WAL commit | Spanner insert |
| Capability check | Read-your-writes | WAL snapshot | Spanner strong read |
| Reauth TTL expiry | Eventually consistent | Acceptable | Acceptable |

Only budget enforcement requires linearizability. All other operations require weaker guarantees that SQLite WAL already provides.

---

## Snapshot Isolation Is Not Enough

Some databases offer snapshot isolation as their strongest level. Under snapshot isolation:
- Each transaction sees a consistent snapshot of the database at transaction start
- Writes conflict if they touch the same row
- Conflicts cause one transaction to abort and retry

This sounds sufficient but fails for budget enforcement because:
1. Two transactions can both start before either writes
2. Both see `budget_spent=0`
3. Both pass the budget check
4. One commits first
5. The second commits without seeing the first's write (it started with an earlier snapshot)
6. Both are ALLOWED, total spend exceeds limit

This is the **write skew anomaly**. It is prevented by serializable isolation, not snapshot isolation. SQLite WAL provides serializable isolation for writes via the `_budget_lock`. Spanner provides it natively.

# 0001. Keep quota state in SQLite WAL

Each reservation is one batch row. Admission takes a SQLite write lock, evicts settled rows at the cutoff, checks the tenant total, and inserts the reservation in one transaction. WAL lets independent workers share the file on one host. A busy timeout handles short lock contention. Settlement takes the lock before reading the clock so contention cannot backdate the charge.

## Reserve for both attempts at admission

Hold twice the per-attempt budget, including input and maximum output, in the same transaction. A primary timeout can still incur provider usage. Reserving both attempts keeps the backup within the admitted budget even if other requests from the tenant arrive while the primary is running.

That hold reduces how many requests a tenant can run concurrently. For example, a tenant with 50,000 tokens available can start five requests with a 5,000-token per-attempt budget. Each holds 10,000 tokens until settlement. Release unused backup capacity and settle successful attempts against reported usage. Retain the full allocation of a started attempt when reliable usage is unavailable.

## Recover abandoned holds offline

Stop all gateway workers that use the database and confirm they have exited. Run the command with the deployed SQLite path.

```text
uv run python -m assessment.quota runtime/quota.sqlite3 --workers-stopped
```

Recovery marks active rows settled at their full reserved amounts. It keeps each charge for a fresh full window, 60 seconds by default, so potentially billed work still counts. A repeat run leaves already settled rows alone. Restart the workers when the command finishes. The flag is the operator's confirmation that the database has no active gateway workers.

Replicas on separate hosts would need a shared transactional quota store.

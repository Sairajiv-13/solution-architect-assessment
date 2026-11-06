# 0001. Keep quota state in SQLite WAL

Each reservation is one batch row. Admission takes a SQLite write lock, evicts settled rows at the cutoff, checks the tenant total, and inserts the reservation in one transaction. WAL lets independent workers share the file on one host. A busy timeout handles short lock contention. Settlement takes the lock before reading the clock so contention cannot backdate the charge.

Reserve room for both attempts before calling the primary. Release the unused backup allocation after success. If an attempt times out without usage, retain its full estimate because remote billing can continue.

This trades admission capacity for a simpler invariant. Replicas on separate hosts would need a shared transactional quota store.

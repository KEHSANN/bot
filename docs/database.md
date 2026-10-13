# Database foundation status

Phase 1 only establishes the PostgreSQL asynchronous connection pool. No domain tables or migration history exist yet. No SQLite substitute or mutable wallet schema has been introduced.

The next database phase must add Alembic, normalized users/RBAC/catalog foundations, integer monetary representations, immutable ledger constraints, atomic inventory and order state transitions, unique retry keys, referral constraints, indexes and real PostgreSQL transaction/concurrency tests. Follow `project-requirements.md`; document implemented tables separately from planned schema.

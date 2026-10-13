# Operations status

The foundation emits allowlisted JSON metadata to stderr. Request bodies, URLs, query strings, headers, arbitrary log messages and exception text are not serialized. Uvicorn access logs must remain disabled to avoid accidental query-string leakage.

Readiness dependency probes are concurrent and time-bounded. Limit polling cadence (for example 10 seconds) and ingress access; the readiness endpoint is not a load-test target or proof of domain correctness. It does validate the expected migration revision.

Database backups, point-in-time recovery, reconciliation, queues, metrics, alerts, provider circuit breakers and disaster recovery are still required. No backup or restore outcome is claimed.

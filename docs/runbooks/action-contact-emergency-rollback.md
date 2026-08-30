# Action/Contact Emergency Rollback

Use this runbook for an incident affecting Action execution, Contact delivery,
the job-search facade, or recommendation strategy serving. The rollback is a
routing change only: it must not delete Action, recommendation, audit, Session,
Outbox, encrypted PII, or delivery facts.

## Command

From `backend/`, first generate the plan without changing state:

```powershell
python scripts/action_execution_emergency_rollback.py `
  --operator <operator> `
  --reason "<incident reason>" `
  --report incident-rollback.json
```

After incident approval, execute the same deterministic plan with the database
DSN and `--yes`:

```powershell
python scripts/action_execution_emergency_rollback.py `
  --dsn-env DB_URL `
  --operator <operator> `
  --reason "<incident reason>" `
  --yes `
  --report incident-rollback.json
```

The incident id is derived from operator and reason. Re-running the command is
safe: switches remain off, only currently issued/pending Contact records are
revoked, and the same incident audit reason prevents duplicate config audit
rows. A missing config row is reported as a mixed-version no-op; the Redis
routing switch still stops new traffic.

## Fixed Order

1. Stop Action and Contact routes.
2. Force Facade and recommendation strategy to off/legacy.
3. Scan Action, recommendation, audit, Session, and Outbox facts.
4. Revoke issued grants and unsent Contact deliveries; never rewrite used or sent payloads.
5. Run the session and Outbox reconcilers. Do not rerun the router to repair a committed turn.
6. Check duplicate delivery/provider and PII alerts, then attach the JSON report to the incident.

If Redis is unavailable, keep the service in its existing fail-closed/off
configuration, page the on-call owner, and do not declare containment until the
routing keys are confirmed. Reconciliation and the final PII/duplicate checks
remain required after recovery.

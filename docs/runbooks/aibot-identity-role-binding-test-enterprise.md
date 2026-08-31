# AIBot Identity and Role Binding Test-Enterprise Runbook

This runbook is for a synthetic-data test enterprise only. It does not authorize
production credentials or a production migration.

## Preconditions

- Apply `phase14_001_aibot_channel.sql`, then `phase16_001_aibot_identity_role_binding.sql` on a backup copy and staging.
- Keep `WECOM_AIBOT_ENABLED=false`, `IDENTITY_RESOLUTION_ENABLED=false`, and `ROLE_BINDING_MODE=shadow` while loading fixtures.
- Store `WECOM_AIBOT_SECRET` and `WECOM_AIBOT_IDENTITY_APP_SECRET` in the secret manager as separate entries. Never put either in fixtures or logs.
- Confirm the identity application has visibility of the synthetic members and the `open_userid_to_userid` permission.

## Offline checks

1. Run `pytest -q tests/unit/test_aibot_identity_*.py` and the existing AIBot contract tests.
2. Verify parser fixtures for plain-looking, open, invalid, and group callbacks. The parser must default to the opaque identity boundary.
3. Verify the mock identity client maps by `userid_list[].open_userid`, chunks at 1000, and records invalid values without creating a `User`.
4. Query identity, binding, registration, invite, and audit rows. No opaque value may appear in `user.external_userid`, `conversation_log.userid`, Contact, or Action rows.

## Test-enterprise sequence

1. Enable the connector for an allowlisted bot with `IDENTITY_RESOLUTION_ENABLED=true`; keep role binding in shadow mode.
2. Send one synthetic single-chat message from a member that produces an open userid. Confirm a verified identity, canonical worker registration, and a single AIBot Outbox row.
3. Send a member outside application visibility and an invalid open userid. Confirm `rejected`, no User/role/Session/Action/Contact rows, and only the safe binding guidance.
4. Create a one-use factory/broker invite, apply it to a verified binding, then approve it in the admin API. Confirm role and capability flags change in one transaction and an append-only audit row is present.
5. Replay the invite, revoke the binding, and send another message. Confirm replay/revoked requests fail closed.
6. Exercise an internal group with two synthetic members. Confirm ordering/session/outbox keys use `chatid`, permissions use each member canonical userid, and publish/Contact/PII remain denied.
7. Inject token timeout, conversion 5xx, DB and Redis failures. Confirm inbound remains retryable and no successful protocol ACK or business Outbox is emitted.

## Rollback

Disable AIBot acceptance and writer, stop new role approvals, export pending/uncertain rows, and inspect sent results. Do not retarget AIBot Outbox rows to legacy. Rotate/revoke the Bot Secret and identity application secret after the test. Keep identity and audit history; only run a guarded migration down after data reconciliation.


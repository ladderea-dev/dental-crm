# AirZen Full Automation Audit

Baseline audited: `0a4611689fe1ba2a4d78cdc1b29300427116c9ef`

## Executive Summary

The previous WhatsApp no-reply class is addressed in this baseline: the signed Meta webhook awaits inbound processing before returning. This follow-up implementation adds a durable database-backed appointment notification outbox, queues critical appointment notifications, and adds a scheduler worker for retryable delivery.

No production data was accessed, no live customer messages were sent, and no broadcast/call/deploy/git action was performed.

## Automations Discovered

26 automations are catalogued in `automation_inventory.csv`:

- WhatsApp inbound webhook, duplicate detection, AI reply, opt-out, human takeover, appointment state handling, delivery status handling.
- Appointment booking, confirmation, staff notification, reminders, staff outcome requests, reschedule, cancellation.
- Automation rules, due action queue, follow-up runner, no-response and no-show evaluators.
- Lead import automation, broadcasts, batch calls, campaigns, recording cleanup, Google Meet link creation.

## Confirmed Defects Fixed

1. Default timezone defect
   - Root cause: `db.DEFAULT_TIMEZONE` was `Asia/Kolkata` while AirZen appointment workflows and requirements use `Asia/Singapore`.
   - Fix: changed the default to `Asia/Singapore`.
   - Regression: `test_default_timezone_is_singapore_for_airzen_appointments`.

2. Detached call-outcome WhatsApp fallback risk
   - Root cause: batch and campaign loops used `asyncio.create_task(handle_call_outcome_whatsapp_fallback(...))`.
   - Impact: fallback notification work could disappear during request/task/process shutdown.
   - Fix: await the fallback handler in both paths.
   - Regression: `test_call_outcome_whatsapp_fallback_is_awaited_in_batch_and_campaign_paths`.

3. Missing durable appointment notification outbox
   - Root cause: customer/staff appointment notifications were sent directly from request or scheduler paths.
   - Impact: appointment rows could be saved while notifications were lost during process restart, redeploy, Meta timeout, or partial DB failures.
   - Fix: added `notification_outbox`, durable enqueue helpers, retry worker, recovery for stuck processing rows, failure classification, dedupe keys, and scheduler processing.
   - Regression: `test_notification_outbox.py`, `test_appointment_notification_journeys.py`, `test_notification_worker_recovery.py`, `test_notification_idempotency.py`, `test_notification_partial_failures.py`.

4. Customer/staff reschedule and cancellation notification gaps
   - Root cause: reschedule sent only staff notification directly; cancellation did not persist notification intents.
   - Fix: reschedule and cancellation now queue customer and staff notification intents and fail the workflow response if required intents are not persisted.

## Remaining Production Blockers

- No isolated staging Supabase credentials were available in this local environment, so database-write journey tests were not executed.
- The outbox migration has not been run in staging or production from this workspace.
- Atomic appointment mutation plus outbox insertion still needs a Supabase RPC for full database transaction semantics; the current implementation refuses notification-complete success if outbox persistence fails.
- Batch and active campaign execution still depend on in-memory active task state; a redeploy can interrupt active work.
- Cross-process scheduler locking is incomplete; in-process guards exist, but multi-instance duplicate execution needs DB locking or leasing.

## Observability

WhatsApp diagnostic events exist for the required no-reply path:

- `inbound_accepted`
- `duplicate_skipped`
- `ai_disabled`
- `human_takeover_active`
- `opted_out`
- `appointment_state_entered`
- `appointment_state_unhandled`
- `response_generated`
- `outbound_send_started`
- `outbound_send_failed`
- `outbound_send_succeeded`

Remaining observability gap: some legacy `log_error` details still include raw phone numbers or message snippets outside the standardized diagnostic helper. These should be converted to hashed correlation references before full production.

## Lost Work Risks

- Safe or mostly safe: signed inbound webhook, queued automation actions, appointment reminders, follow-up actions.
- Safer now: appointment notification intents are persisted to a durable outbox before notification-complete success is reported.
- High risk: batch/campaign active runs during redeploy because active progress is in memory.
- Fixed in this audit: detached WhatsApp fallback after batch/campaign call outcome.

## Production Gate

`scripts/run_airzen_production_gate.py` was added.

Command:

```bash
python scripts/run_airzen_production_gate.py --mode staging --all
```

The gate runs:

- configuration presence checks
- `python -m pytest -q`
- WhatsApp dry-run audit
- `python -m py_compile server.py db.py whatsapp.py`

It writes:

- `airzen_production_gate_results.json`
- `airzen_production_gate_report.md`

It returns non-zero if critical config is missing, the dry-run/tests fail, staging DB-write verification is blocked, outbox/RLS checks fail, restart recovery is not verified, or customer/staff/reschedule/cancellation journeys are skipped.

## Latest Validation

- `python -m pytest -q`: 73 passed, 0 failed, 4 warnings, 3 subtests passed.
- `python scripts/test_whatsapp_automations.py --mode dry-run --all`: 63 passed, 0 failed, real messages sent 0.
- `python -m py_compile server.py db.py whatsapp.py scripts/run_airzen_production_gate.py`: passed.
- `git diff --check`: passed.
- `python scripts/run_airzen_production_gate.py --mode staging --all`: failed closed because guarded staging Supabase configuration is missing locally.

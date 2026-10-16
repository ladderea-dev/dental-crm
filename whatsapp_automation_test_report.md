# WhatsApp Automation Test Report

## Executive summary

The WhatsApp implementation was audited with mocked/dry-run inputs only. The supplied customer test number and staff test number were used solely to validate E.164 normalization and payload addressing. No Graph API request was made and no real WhatsApp message was sent.

- Template purpose slots discovered: 11
- Default automation workflows discovered: 14
- Dry-run checks: 63 passed, 0 failed
- Automated regression/security tests: 25 passed, 0 failed
- Real messages sent: 0

## Test data safety

- Customer test input: masked in reporting; validated as Singapore E.164
- Staff test input: masked in reporting; validated as international E.164
- The CLI defaults to `dry-run`.
- Live mode refuses `--all`, requires one template, requires `WHATSAPP_TEST_NUMBER`, and requires the exact confirmation phrase. The audit runner intentionally stops before sending, even after those checks.
- Broadcasts are never executed by the runner.

## Templates tested

All 11 template purpose slots were built with safe sample data and checked for expected variable count, language, Meta payload structure, Unicode, special characters, blank/null substitution, long text serialization, and both supplied number formats. See `whatsapp_template_inventory.csv`.

Meta template category, approved body text, header, button definitions, and approval status are not stored authoritatively in this repository. Those fields require a read-only Meta template sync or production configuration review.

## Workflows tested

The 14 default rule triggers were inventoried and checked for unique event/source keys, valid actions, safe defaults, and fallback outcomes:

`new_lead`, `manual_lead`, `uploaded_lead`, `facebook_lead`, `instagram_lead`, `website_lead`, `google_sheet_lead`, `api_lead`, `followup_lead`, `callback_scheduled`, `appointment_confirmed`, `showroom_visit_confirmed`, `re_enquiry`, and `missed_call_retry`.

Additional code paths reviewed include appointment staff notifications and reminders, handoff notifications, appointment-outcome requests, broadcasts, conversation/message persistence, status processing, duplicate provider message checks, template cooldowns, and inbound opt-out handling.

## Webhook and error checks

Mock payload coverage included inbound text, image, interactive button, list response, delivery status, unsupported message type, malformed/empty shape, and opt-out commands. Existing critical security tests also passed for valid signatures, malformed signatures, invalid signatures, signed malformed JSON, and GET verification tokens.

Error classification checks covered HTTP 400, 401, 403, 429, 500, and 503 plus invalid/expired token, invalid template, language mismatch, non-WhatsApp recipient, invalid phone, timeout, and Meta ecosystem restrictions. Provider failures are recorded and returned without infinite retry. The direct send helper does not automatically retry 429/5xx/timeouts; adding retries without provider idempotency could duplicate customer messages, so this remains a production design decision rather than an automatic audit change.

## Code defects fixed

1. `staff_demo_status_request` expected three variables but the common template builder returned zero. It now supplies customer name, customer phone, and appointment date/time.
2. Meta payload creation previously stripped only a leading plus sign and could accept malformed recipient text. A reusable payload builder now applies the existing E.164 normalization and rejects invalid numbers before network access.
3. Opt-out detection matched `stop` anywhere in a sentence. Explicit unsubscribe commands remain supported, while unrelated sentences such as “Please stop by tomorrow” no longer disable the conversation.
4. A successful appointment insert could be reported as failed when the secondary appointment lookup raised an error. Booking confirmation now uses a safe fallback record after a successful insert, and a true failure clears the stale pending-confirmation state.
5. Alternative appointment suggestions were not persisted to conversation state, so a later “OK” could confirm an older rejected time. Alternative state is now durable and replaces stale pending state.
6. Existing future bookings are now detected by normalized customer number. A replacement date/time is confirmed as a reschedule and uses the internal appointment ledger instead of creating an unrelated duplicate booking.
7. Appointment events are stored as compact durable customer facts. The AI receives recent chat messages plus a small set of durable facts instead of an unbounded raw transcript.
8. Dashboard rescheduling now notifies the newly assigned staff member and returns a visible warning when staff delivery cannot be completed. The appointment planner groups multiple records into one customer journey, keeps active bookings prominent, and collapses historical entries.

Regression tests cover all three fixes.

## Remaining external blockers

- `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` were absent in the local test environment, so production database persistence was not exercised.
- Meta credentials and approved template definitions were not read or changed.
- Template approval, language/category/header/button parity, real delivery, staff receipt, delivery/read callbacks, and production database updates require an optional controlled live test.
- No automated retry is performed for ambiguous provider/network failures, preventing duplicate sends but requiring operational review of failed logs.

## Safe rerun commands

```powershell
python scripts/test_whatsapp_automations.py --mode dry-run --all --output whatsapp_test_results.json
python scripts/test_whatsapp_automations.py --mode dry-run --templates
python scripts/test_whatsapp_automations.py --mode dry-run --workflows
python scripts/test_whatsapp_automations.py --mode dry-run --webhooks --errors
python -m unittest discover -v
python -m compileall -q whatsapp.py scripts/test_whatsapp_automations.py test_whatsapp_automations.py
```

## Controlled live test recommendation

A controlled live test is still necessary only to prove Meta approval/configuration and end-to-end delivery. It should use the staff test number allowlist, one approved template at a time, and never a customer or broadcast recipient. This audit did not perform that test.

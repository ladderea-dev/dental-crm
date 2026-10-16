# WhatsApp no-reply investigation

## Outcome

### Final findings from the delayed `9 am` reproduction (2026-08-12)

The new screenshot and local source reproduction identify three additional, confirmed application defects. All occur before or at the boundary of Meta sending:

1. After one requested slot is rejected, `_suggest_available_demo_slots()` could perform **8,640 sequential database availability checks** before creating an outbound response. This is the exact cause of the multi-hour response path and explains an inbound message being visible with no outbound attempt while the scan was running. A mocked `9 am` continuation preserves the saved date, resolves to `09:00`, and reaches availability correctly; the parser and stale appointment state are not what stopped this message.
2. Availability previously collapsed all rejection reasons into the customer statement “already booked.” The same false result also covered past/too-soon times, the maximum booking window, no active staff, staff working hours, and staff-role mismatch. The message was therefore incorrect when no appointment conflict existed.
3. An exception after saving the inbound row was caught by the signed webhook, which still returned HTTP 200. Meta then stopped retrying. Even if redelivered, the duplicate check treated the inbound row as completed and skipped it. This was a permanent-loss path before any outbound log.

Permanent corrections now in the local tree:

- Alternative discovery is best-effort and capped at 32 checks and five seconds.
- Availability returns a categorical reason; “already booked” is used only for an actual appointment conflict.
- Inbound records use `processing`, `processed`, and `processing_failed` states. Failed and abandoned processing can resume without inserting a duplicate inbound row.
- Fresh concurrent processing is not falsely acknowledged as complete.
- A genuine processing exception returns HTTP 500 so Meta can retry.
- Diagnostics use hashed correlation references and exception types, not phone numbers, bodies, tokens, or secrets.

The production-observed failure occurred **before Meta sending**.

The signed webhook parsed each inbound event and started `handle_inbound_whatsapp_message()` with `asyncio.create_task()`, then immediately returned HTTP 200. On a request-scoped worker, the detached task could be cancelled after it had inserted the inbound message/log but before appointment parsing, AI generation, outbound payload creation, or the Meta API request. This exactly explains the observed combination: inbound message visible, `inbound_message/received` present, AI shown as ON, and no recent outbound log.

A second confirmed defect obscured send-stage failures: `send_whatsapp_text()` did not consistently insert a `whatsapp_logs` row for free-text success/failure. Conversation-level `whatsapp_messages` could record a failure only if control returned to the caller; configuration and provider failures had no guaranteed outbound WhatsApp log.

Appointment state was not the direct cause of the complete silence. An `alternatives_offered` state consumes an unparseable greeting and sends a safe request for another date/time. A parseable alternate time or a fresh booking sentence is evaluated before that clarification branch and continues the booking flow. The detached webhook task could be terminated before either response was sent.

## Runtime trace

1. `server.api_wa_webhook_receive` reads the raw body and validates `X-Hub-Signature-256` with `META_APP_SECRET`.
2. `parse_webhook_messages` normalizes Meta messages/status events.
3. The webhook now **awaits** all parsed message handlers before acknowledging the request.
4. `handle_inbound_whatsapp_message` normalizes the number and checks `provider_message_id` in `whatsapp_messages`.
5. It gets/restores/creates `whatsapp_conversations`, links CRM asynchronously, saves the inbound message, inserts the inbound `whatsapp_logs` row, and updates the conversation.
6. Exact opt-out is applied. Follow-up intent state is updated. Human takeover (`assigned_to`) and `ai_enabled` are checked before and again inside the per-contact lock.
7. The current 24-hour customer-service window is checked. Non-text media gets a safe acknowledgement.
8. `_handle_whatsapp_appointment_state` parses existing state first where appropriate, parses fresh date/time requests, checks staff/slot availability, persists the next appointment state, and emits a clarification or booking response.
9. If appointment handling returns false, recent history and durable memory are loaded, Gemini response generation runs, and an empty/error result is replaced with a safe fallback.
10. `send_whatsapp_text` validates WhatsApp enablement/provider/config, creates the Meta text payload, calls `/{graph_version}/{phone_number_id}/messages`, and now inserts a sent/failed `outbound_text` row in `whatsapp_logs` for every outcome.
11. The caller saves the outbound `whatsapp_messages` row with `sent` or `failed` status and updates the conversation only after success.

## Silent-return audit

The following inbound branches return without an automated reply by design:

- status callback rather than a customer message;
- missing sender number (malformed/unsupported webhook);
- staff appointment/status command handled on the staff path;
- duplicate provider message ID;
- conversation lookup/create failure;
- `not_interested` or `wrong_number` follow-up classification, which stops automation;
- exact opt-out;
- AI disabled;
- human takeover active;
- service window reported closed without a successfully saved current inbound row;
- empty text after message-type handling.

Previously, most had assorted logs but no uniform sanitized decision event. Required correlation-only events were added: `inbound_accepted`, `duplicate_skipped`, `ai_disabled`, `human_takeover_active`, `opted_out`, `appointment_state_entered`, `appointment_state_unhandled`, `response_generated`, `outbound_send_started`, `outbound_send_failed`, and `outbound_send_succeeded`. They contain truncated SHA-256 correlation references and categorical metadata only—never full numbers, message bodies, tokens, or secrets.

Other verified conditions:

- Conversation `closed` status does not suppress a new inbound message; updating the last inbound message reopens it.
- `assigned_to` now suppresses AI even if stale/inconsistent data leaves `ai_enabled=true`.
- Appointment `alternatives_offered` cannot silently fall through: an invalid reply gets a safe date/time clarification.
- Duplicate detection is based on provider message ID and occurs before conversation/message insertion.
- Automation rules are separate from the direct inbound AI reply path; the dry-run confirms their eligibility/suppression behavior without sending.
- Missing Meta token/phone-number ID, disabled WhatsApp, invalid recipient, provider HTTP error, timeout/exception, Vobiz error, and successful sends now all produce an outbound log result.

## Changes

- `server.py`
  - Await signed webhook message processing instead of launching request-detached tasks.
- `whatsapp.py`
  - Add sanitized diagnostic events.
  - Make human takeover an independent AI suppression condition.
  - Guarantee `whatsapp_logs` sent/failed rows for free-text sends.
  - Emit appointment-state and response-generation diagnostics.
- `test_critical_security.py`
  - Prove a signed webhook awaits its handler before returning HTTP success.
- `test_whatsapp_appointment_flow.py`
  - Prove stale failed-slot state gives safe clarification for greetings/service text.
  - Prove a valid alternate time continues the booking flow.
  - Prove `Hello, please book today at 2 PM` parses as 14:00.
- `test_whatsapp_automations.py`
  - Prove a Meta configuration failure creates a sanitized outbound failure log.

No production data was accessed or modified. No live message was sent. No commit, push, or deployment was performed.

## Verification results

- `python -m pytest -q`: **PASS — superseded by the final validation below**.
- `python scripts/test_whatsapp_automations.py --mode dry-run --all`: **PASS — 63/63 passed, 0 failed, 0 real messages sent**.
- `python -m py_compile server.py db.py whatsapp.py`: **PASS**.

The final totals after the reliability corrections are recorded below.

## Final local validation (2026-08-12)

### Address repeated, CONFIRM ignored, and every time rejected

The later screenshots exposed three linked defects:

- Booking details could exist in `appointment_state` without a valid workflow `status`. Address, property-type, and `CONFIRM` messages then fell through to general AI instead of the deterministic appointment handler. The handler now repairs a missing/unknown state to `awaiting_address`, `awaiting_intake`, or `pending_confirmation` from persisted fields.
- `patch_conversation()` swallowed appointment-state update failures. The bot could tell the customer a field was saved even when the next webhook could not reload it. Appointment-state writes are now read back and compared; a missing/mismatched write raises, produces `appointment_state_persist_failed`, and enters the safe failure/retry path.
- New appointment staff defaulted to role `sales`, while booking settings require `service_staff`. Such staff appear active in the UI but cannot satisfy any slot. The defaults now match. Availability messages separately identify working-hours, inactive-staff, and staff-role configuration failures instead of telling the customer to try another time when every time would fail.

The final confirmation step also rechecks categorical availability and only reports booking success after the appointment insert returns an ID. A past time, genuine conflict, working-hours failure, inactive staff, and role mismatch now receive different truthful responses.

### Appointment not appearing after “booking request received”

The customer sentence “Your booking request has been received” was generated by the general Gemini reply path. It was not emitted by the deterministic booking transaction, and no appointment insert had occurred. The customer had therefore received misleading workflow language while the Appointments screen correctly had no corresponding row.

The permanent local correction adds two independent controls:

- The AI prompt is forbidden from saying a request was received/recorded or an appointment was created/scheduled/booked/confirmed unless persisted booking information contains an appointment ID.
- Generated output is checked in application code. If it still makes an unverified booking claim, it is replaced with a deterministic clarification saying that no appointment exists yet. When date and time are known, it asks for explicit `CONFIRM`; otherwise it asks for the missing preferred date and time. A sanitized `unpersisted_booking_claim_blocked` event records the correction.

Only the deterministic appointment handler may report a successful booking, and it does so after the appointment insert/atomic RPC returns successfully. Tests cover the exact customer wording from the screenshot, a known slot awaiting confirmation, a genuine persisted booking, and an ordinary price reply.

- `python -m pytest -q`: **PASS — 178 passed, 0 failed, 6 skipped, plus 3 passed subtests**. The skips are guarded PostgreSQL staging tests unrelated to this mocked WhatsApp run.
- `python scripts/test_whatsapp_automations.py --mode dry-run --all`: **PASS — 63/63 passed, 0 failed, 0 real messages sent**.
- `python -m py_compile server.py db.py whatsapp.py`: **PASS**.
- `git diff --check`: **PASS**.

No production environment or database was accessed. No live message was sent. No commit, push, or deployment was performed.

## Production verification steps (after normal review/deployment approval)

1. Back up/review current WhatsApp settings without exposing values. Confirm enabled provider, Graph version, token presence, phone-number ID presence, and app-secret presence.
2. Deploy through the normal controlled process; do not use the production customer shown in the incident.
3. Use a dedicated approved test contact and record only masked/correlation identifiers.
4. Send a unique signed inbound greeting. Confirm, in order: `inbound_accepted`, `response_generated`, `outbound_send_started`, then `outbound_send_succeeded`; confirm matching inbound and outbound rows.
5. Replay the exact same webhook message ID. Confirm `duplicate_skipped` and no second outbound send.
6. Start a test booking, force/select an unavailable slot, then send an available alternate time. Confirm `appointment_state_entered`, persisted next state, response generation, and outbound success.
7. While `alternatives_offered` remains, send a greeting. Confirm `appointment_state_unhandled` with `action=safe_clarification` followed by an outbound response.
8. Send `Hello, please book today at 2 PM` in a clean test conversation. Confirm parsing reaches slot availability and returns either availability/confirmation or an explicit unavailable-slot response.
9. Enable human takeover while leaving AI visibly enabled in the test fixture. Confirm `human_takeover_active` and no outbound AI send.
10. Send the exact opt-out word `STOP`. Confirm `opted_out`, AI disabled/status updated, and no AI send. Confirm a non-opt-out phrase such as `Please stop by tomorrow` does not opt out.
11. In a staging/sandbox Meta configuration only, inject a provider rejection or invalid test credential. Confirm `outbound_send_failed` and a failed `outbound_text` row with no token, body, or full phone in diagnostics.
12. Confirm an unavailable requested slot reaches `appointment_all_requested_slots_unavailable` and produces a response inside the hosting timeout; verify the old thousands-of-check scan cannot recur.
13. In isolated staging, inject a handler exception after inbound persistence. Confirm status becomes `processing_failed`, the webhook returns 500, the retry reuses the same inbound row, and final status becomes `processed`.
14. Monitor webhook duration/error rate and Meta retries. Configure the hosting request timeout above the AI plus Meta send timeout; provider message IDs now distinguish completed, failed, active, and abandoned processing.

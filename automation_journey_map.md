# AirZen Automation Journey Map

Baseline audited: `0a4611689fe1ba2a4d78cdc1b29300427116c9ef`

This map traces complete customer and staff automation journeys without using production data or sending live messages.

## 1. New inbound WhatsApp lead

- Initial state: unknown or existing phone sends a signed Meta webhook message.
- Trigger: `POST /api/whatsapp/webhook`.
- Validation: HMAC SHA-256 signature is required.
- Database mutation: conversation created or restored, inbound message inserted, inbound `whatsapp_logs` row inserted, CRM lead/journey updated when available.
- Customer notification: AI reply or clarification when eligible.
- Staff notification: none by default.
- Log entry: `whatsapp_webhook_received`, `inbound_accepted`, inbound save events.
- Final state: open conversation, unread incremented, AI path evaluated.
- Failure state: malformed webhook rejected, signature rejected, inbound save failure logged.
- Retry state: Meta retries non-2xx; duplicate delivery is suppressed by provider message id.
- Recovery after restart: safe because request awaits processing.

## 2. Existing customer inbound WhatsApp message

- Initial state: open conversation exists.
- Trigger: signed inbound webhook.
- Validation: duplicate provider id check, opt-out check, AI/takeover checks.
- Database mutation: inbound message, last message, follow-up intent fields.
- Customer notification: AI reply, appointment handling reply, media acknowledgement, or no reply only for explicit suppression.
- Staff notification: none.
- Failure state: send failure saved as outbound failed message/log.
- Recovery: no durable retry for failed free-text AI sends yet; failure is visible.

## 3. Normal AI reply

- Initial state: AI on, no staff takeover, service window open.
- Trigger: inbound text/button/interactive message.
- Validation: conversation latest state is re-read under a per-phone lock.
- Database mutation: outbound WhatsApp message row and `whatsapp_logs`.
- Customer notification: Meta free-text send.
- Staff notification: none.
- Failure state: fallback clarification text generated if AI returns empty; Meta send failure logged.
- Recovery: visible failure; no automatic retry for free-text replies.

## 4. Human takeover

- Initial state: `assigned_to` is set on the WhatsApp conversation.
- Trigger: inbound customer message.
- Validation: AI enabled may still be true, but takeover wins.
- Database mutation: inbound message only.
- Customer notification: suppressed.
- Staff notification: none.
- Log entry: `human_takeover_active`.
- Final state: conversation remains open for staff.

## 5. AI re-enabled

- Initial state: staff clears takeover or AI is toggled on.
- Trigger: UI patch to conversation state.
- Validation: next inbound message re-reads latest conversation state.
- Database mutation: conversation `ai_enabled`/`assigned_to`.
- Customer notification: resumes on next eligible inbound.
- Failure state: if assignment remains set, AI remains suppressed.

## 6. Exact STOP/UNSUBSCRIBE opt-out

- Initial state: customer sends exact opt-out phrase.
- Trigger: inbound webhook text.
- Validation: `_is_opt_out`.
- Database mutation: conversation `ai_enabled=false`, `status=opted_out`.
- Customer notification: suppressed.
- Log entry: `opted_out`.
- Final state: no AI replies.

## 7-12. Appointment request, missing fields, slot unavailable, alternate slot

- Initial state: inbound text includes booking intent or existing appointment state.
- Trigger: appointment parser in `_handle_whatsapp_appointment_state`.
- Validation: service/date/time/mobile/address/location completeness, slot/staff availability.
- Database mutation: `appointment_state` on conversation until enough details exist.
- Customer notification: clarification for missing service/date/time/address/mobile or unavailable slot.
- Staff notification: none until a booking is created.
- Log entry: `appointment_state_entered` or `appointment_state_unhandled`.
- Failure state: stale failed-slot state no longer silences later messages; greetings receive clarification or AI fallback.
- Retry state: customer can provide alternate time or restart request.
- Recovery: state is stored in DB and read on next message.

## 13-15. Successful appointment creation, customer confirmation, staff notification

- Initial state: appointment details complete and slot available.
- Trigger: appointment state handler calls `insert_appointment`.
- Validation: minimum notice, max days ahead, duplicate active appointment check, staff availability.
- Database mutation: `appointments` row, lead journey updates, conversation state cleared, notification flags updated when sends succeed/fail.
- Customer notification: customer confirmation intent persisted to `notification_outbox`.
- Staff notification: staff booking detail intent persisted to `notification_outbox`.
- Log entry: appointment state logs, `notification_intent_persisted`, later `whatsapp_template_send_prepared`, `whatsapp_logs`.
- Final state: booked appointment plus queued notification intents.
- Failure state: if either required intent is not persisted, notification-complete success is refused and appointment `notification_error` is updated.
- Recovery: outbox worker retries temporary failures and marks permanent failures visibly.

## 16-21. Reschedule and cancellation journeys

- Reschedule trigger: appointment reschedule API.
- Reschedule mutation: old appointment marked `rescheduled`, new row inserted.
- Customer notification: reschedule notification intent persisted.
- Staff notification: staff reschedule notification intent persisted.
- Cancellation trigger: cancellation API.
- Cancellation mutation: appointment marked `cancelled`.
- Customer/staff notification: cancellation notification intents are persisted.
- Failure state: cancellation API refuses notification-complete success if intents are not persisted.
- Recovery: outbox retry worker handles sends; manual action remains for permanently failed rows.

## 22. Appointment reminder

- Trigger: APScheduler every 60 seconds via `run_due_appointment_reminders`.
- Validation: appointment due window, not past, flags not already sent.
- Database mutation: reminder flags and errors on appointment.
- Customer notification: reminder intent persisted to outbox.
- Staff notification: staff reminder intent persisted to outbox.
- Failure state: outbox insert errors stored in `reminder_error`; send errors stored on outbox row.
- Recovery: outbox retries temporary failures and recovers stuck `processing` rows.

## 23-24. Missed appointment or outcome workflow and follow-up action

- Trigger: scheduler detects past demo or staff outcome request due.
- Mutation: appointment demo fields, lead journey, follow-up actions.
- Customer notification: follow-up action may queue WhatsApp.
- Staff notification: outcome request to staff.
- Failure state: request failure is stored; count prevents repeated spam.
- Recovery: scheduled scan repeats.

## 25-26. Lead assignment and lead status update

- Trigger: CRM API changes or automation lead import.
- Mutation: CRM contact assignment/status/follow-up fields.
- Notification: none unless automation rule is enabled.
- Failure state: DB update error surfaces through API/logs.
- Recovery: manual retry.

## 27-29. Broadcast draft, execution, delivery/read/failed status

- Draft trigger: broadcast campaign API.
- Execution trigger: broadcast start API.
- Mutation: broadcast campaign and recipient rows.
- Customer notification: WhatsApp template to recipients.
- Staff notification: none.
- Failure state: recipient status and `whatsapp_logs` show failures.
- Recovery: partial through recipient rows; do not run broadcasts in local audit.
- Delivery/read/failed trigger: Meta status webhook updates logs/messages by provider id.

## 30-31. Meta temporary and permanent API failure

- Trigger: Meta send returns non-2xx or timeout.
- Mutation: `whatsapp_logs` failed row and outbound failed message when applicable.
- Customer notification: none accepted by Meta.
- Failure state: classified error reason, sanitized where helper is used.
- Retry state: mostly manual; durable retry queue remains a gap for critical notifications.

## 32-35. Restart, duplicate webhook, duplicate scheduler, redeploy during pending notification

- Restart during signed inbound webhook: safe because processing is awaited.
- Restart during automation queue: recoverable from `automation_actions`.
- Restart during appointment reminder: recoverable while flags remain unset.
- Restart during batch/campaign active run: high risk because active task state is in memory.
- Duplicate webhook delivery: suppressed by provider message id.
- Duplicate scheduler execution: guarded by in-process running flags and DB status transitions; cross-process locking is not complete.
- Redeploy during pending notification: queued actions survive; synchronous appointment notifications can fail after appointment insert and need durable outbox for full production readiness.

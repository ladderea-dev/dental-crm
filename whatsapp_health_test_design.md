# WhatsApp Real Message Health Test

This package adds a guarded administrator health test without changing the existing WhatsApp send, template, inbox, or setup-wizard APIs.

## Safety model

- Real-message runs can target only an encrypted record in `whatsapp_test_recipients` that is both verified and active.
- Adding a recipient requires an administrator assertion that the number is controlled by them. Verification requires one separately confirmed Utility-template message and a six-digit code that expires after ten minutes.
- A run requires the exact confirmation value `I_CONFIRM_THIS_IS_MY_TEST_NUMBER`, shows only a masked number, is limited to ten outbound messages, and enforces a ten-minute per-recipient cooldown.
- Default full runs contain only Utility templates. Marketing templates require both a separate enable flag and the exact confirmation `I_CONFIRM_MARKETING_TEMPLATE_TEST`.
- Template category, approval state, language, name, and variable count are checked before sending. A mismatch is skipped.
- The `whatsapp_health_test` automation action has a dedicated worker branch with no call, campaign, appointment, or message side effect.
- Inbound `TEST OK` from a verified test number is isolated from normal CRM, booking, sales, and customer automation handling. An optional test reply always starts with `[TEST]`.
- Cleanup deletes only rows linked to the selected dedicated health-test run. It does not delete ordinary CRM records.
- UI/API results are sanitized. Encrypted numbers, verification hashes, authorization headers, and credentials are never returned.

## Storage

Apply `whatsapp_health_test_migration.sql` through the reviewed migration process. It creates:

- `whatsapp_test_recipients`
- `whatsapp_health_test_runs`
- `whatsapp_health_test_results`

All three tables have RLS enabled and no policies. `PUBLIC`, `anon`, and `authenticated` receive no table privileges; only `service_role` receives CRUD privileges. The backend must continue using the service-role connection for these endpoints.

Set `WHATSAPP_HEALTH_TEST_ENCRYPTION_KEY` to a dedicated Fernet key in the backend secret store. Never reuse the Meta token or Supabase key. Configure `WHATSAPP_TEST_VERIFICATION_TEMPLATE` as the exact name of an approved Utility template with one body variable for the verification code.

## API endpoints

- `GET/POST /api/whatsapp/health-test/recipients`
- `POST /api/whatsapp/health-test/recipients/{id}/send-verification`
- `POST /api/whatsapp/health-test/recipients/{id}/verify`
- `GET/POST /api/whatsapp/health-test/runs`
- `GET /api/whatsapp/health-test/runs/{id}`
- `POST /api/whatsapp/health-test/runs/{id}/stop`
- `DELETE /api/whatsapp/health-test/runs/{id}/test-data`
- `GET /api/whatsapp/health-test/runs/{id}/report.json`
- `GET /api/whatsapp/health-test/runs/{id}/report.csv`
- `POST /api/whatsapp/health-test/templates/preview-all`
- `POST /api/whatsapp/health-test/templates/run-all`

All endpoints remain protected by the existing administrator API-key/session middleware.

## Operational limits

Delivery webhooks update matching results by Meta provider message ID. An inbound `TEST OK` must arrive within five minutes of the run start. Read receipts are informational. The current implementation records a warning while waiting and upgrades the result when the matching signed webhook arrives.

Scheduled credential-only checks can use the same non-send selections (`configuration`, `meta_connection`, `templates`, `database`, and `automation`). A daily real-message schedule is intentionally not enabled by the migration; administrators must explicitly opt in after staging validation.

## Deployment order

1. Review and apply the migration in isolated staging.
2. Configure the dedicated encryption key and approved Utility verification-template name.
3. Deploy the backend and UI.
4. Add and verify one administrator-controlled staging recipient.
5. Run configuration-only checks, followed by one selected Utility template.
6. Confirm signed delivery and inbound webhooks before enabling any broader test.

No migration or real message is executed merely by deploying the code.

## Test all existing templates

The bulk test performs a fresh Meta inventory request including component data and previews every discovered template before sending. Utility templates are enabled by default; Marketing and Authentication categories require separate UI selections. The preview displays the exact masked payload, eligibility, variable/header/button counts, skip reason, estimated message count, and duration.

Supported components are body text, text/image/video/document headers, static and dynamic URL buttons, quick replies, phone-number buttons, and copy-code/OTP buttons. Media headers require `WHATSAPP_TEST_IMAGE_URL`, `WHATSAPP_TEST_VIDEO_URL`, or `WHATSAPP_TEST_DOCUMENT_URL`. Unsupported component types are recorded as `SKIPPED` and never submitted.

Bulk runs allow 1–50 eligible templates with a 2–10 second delay. Meta HTTP 429 responses use exponential backoff with at most two retries. Only one active run is allowed for a recipient. Accepted messages remain `SUBMITTED` until a signed sent/delivered webhook changes them to `PASS`; after 180 seconds without a webhook they become `WARNING`. Failed or skipped template names can be retried as a filtered new run after the same preview and confirmation process.

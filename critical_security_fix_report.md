# Critical security fix report

Date: 2026-07-11  
Scope: Critical fixes only—FastAPI authentication enforcement and Meta WhatsApp webhook verification.

## Outcome

Sensitive APIs now fail closed: the legacy `AUTH_ENABLED` value can no longer disable authentication. Meta webhook POST requests are accepted only when the raw request body has a valid `X-Hub-Signature-256: sha256=<digest>` computed with the configured Meta App Secret. Invalid signatures return 403; correctly signed malformed/non-object JSON returns 400.

No RLS, Supabase schema, SQL, UI, business workflow, deployment, commit, or production environment was changed.

## Files changed

| File | Relevant lines | Change |
|---|---:|---|
| `server.py` | 242-248 | Removed `POST /api/leads/incoming` and detailed `GET /api/health` from the public-route allowlist; Docker's minimal `/api/live` healthcheck remains public. |
| `server.py` | 273-279 | Made authentication enforcement always enabled; retained `AUTH_ENABLED` only for compatibility. |
| `server.py` | 322-331 | Made the global API authentication middleware unconditional for every non-public API route. |
| `server.py` | 1089-1099 | Removed the “authenticated local user” bypass when authentication was disabled. |
| `server.py` | 1110-1120 | Reports Meta webhook verification as HMAC-SHA256. |
| `server.py` | 3957-3977 | Hardened GET webhook verification with the configured verify token, constant-time comparison, and challenge validation. |
| `server.py` | 4015-4056 | Added raw-body Meta signature validation, 403 rejection, strict JSON-object parsing, and 400 malformed-payload rejection. |
| `test_critical_security.py` | 1-end | Added focused route and webhook tests. |
| `critical_security_fix_report.md` | 1-end | This report. |

The previously generated audit report and route CSV remain unchanged.

## Authentication implementation

The existing custom session design was preserved:

- Login compares the configured admin username/password using constant-time comparison.
- Successful login issues a signed HMAC session containing username and expiry (12-hour TTL).
- Session signatures are checked with `hmac.compare_digest`; expired or wrong-user sessions are rejected.
- Cookie remains `HttpOnly`, `SameSite=Lax`, path `/`, and Secure when HTTPS is detected.
- Logout deletes the same cookie.
- The existing admin API-key mechanism remains supported.
- This application does not use JWT authentication; therefore no JWT validation path was changed.
- CSRF posture was not redesigned. SameSite=Lax plus JSON API requests remains the existing control. The critical fail-open bypass was removed.

If admin credentials or `SESSION_SECRET` are missing, login continues to return a configuration error, while protected APIs remain inaccessible rather than opening anonymously.

## WhatsApp webhook implementation

### GET verification

`GET /api/whatsapp/webhook` remains public because Meta requires it. It reads `WEBHOOK_VERIFY_TOKEN`, falling back to `WHATSAPP_VERIFY_TOKEN`, requires `hub.mode=subscribe`, compares the supplied token in constant time, requires `hub.challenge`, and returns the challenge as plain text. Wrong tokens return 403; a missing challenge after valid authentication returns 400.

### POST authenticity

`POST /api/whatsapp/webhook` remains reachable without an admin session only because Meta must call it. Before JSON parsing or side effects, it:

1. Reads the exact raw request bytes.
2. Loads `META_APP_SECRET` through the existing server-side settings mechanism.
3. Requires `X-Hub-Signature-256` in `sha256=<64 lowercase/uppercase hex characters>` form.
4. Computes HMAC-SHA256 over the raw body.
5. Uses constant-time comparison.
6. Returns 403 for missing secret, missing/malformed header, or digest mismatch.
7. Parses JSON only after authentication and requires a JSON object; malformed payloads return 400.
8. Logs only a boolean rejection reason—never the signature or App Secret.

Existing parsing and asynchronous message handling remain unchanged after verification succeeds.

### Replay handling

Meta’s `X-Hub-Signature-256` scheme does not include a signed timestamp, and legitimate Meta delivery retries must remain accepted. The existing business logic performs equivalent idempotency using provider message IDs before saving/processing inbound messages (`whatsapp.py` around lines 2137-2149 and 3848-3857). No time-window rejection was added because it would break legitimate retries without an authenticated timestamp.

## Route audit: before and after

All FastAPI-decorated routes were enumerated. “Conditional” means the route was previously exposed whenever `AUTH_ENABLED` was false or absent. HTML pages remain public so the login shell and legal pages can load; all data-bearing API calls behind them now require authentication. FastAPI's four generated documentation routes (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`, and `/redoc`) were also audited; they remain public before/after, do not return CRM/business records, and are outside this critical-only change scope.

| Method | Route | Before | After | Auth required |
|---|---|---|---|---|
| POST | `/api/auth/login` | No | Public bootstrap/health endpoint | No |
| GET | `/api/auth/me` | No | Public bootstrap/health endpoint | No |
| POST | `/api/auth/logout` | No | Public bootstrap/health endpoint | No |
| GET | `/api/security/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/schema/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/health` | No | Unconditional session or admin API-key gate | Yes |
| GET | `/` | No | Public HTML shell/legal page; protected APIs supply data | No |
| GET | `/privacy` | No | Public HTML shell/legal page; protected APIs supply data | No |
| GET | `/terms` | No | Public HTML shell/legal page; protected APIs supply data | No |
| GET | `/terms-and-conditions` | No | Public HTML shell/legal page; protected APIs supply data | No |
| POST | `/api/call` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/calls/{call_id}/notes` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/export/calls.csv` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/export/calls.xlsx` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/stats` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/recordings/storage` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/recordings/cleanup` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/recordings/file` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointments` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointments/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointments/staff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointment-staff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/staff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointment-staff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PUT | `/api/appointments/staff/{staff_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/appointment-staff/{staff_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/appointments/staff/{staff_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/appointment-staff/{staff_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointment-staff/{staff_id}/activate` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointment-staff/{staff_id}/deactivate` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/calendar/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/notify-staff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointments/debug/{appointment_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/request-demo-status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/mark-completed` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/mark-no-show` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/demo-result` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/appointments/{appointment_id}/demo-status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/appointments/{appointment_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/appointments/{appointment_id}/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/appointments/{appointment_id}/reschedule` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/appointments/{appointment_id}/notes` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/reschedule` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/cancel` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/complete` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/no-show` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/appointments/{appointment_id}/send-reminder` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/prompt` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/prompt` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/prompt` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/ai-prompts` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/ai-prompts/resolve` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/ai-prompts/{prompt_type}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/ai-prompts/{prompt_type}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/ai-prompts/{prompt_type}/reset` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/knowledge-base` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/knowledge-base` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/knowledge-base/search` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/knowledge-base/preview` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/knowledge-base/sample` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/knowledge-base/export` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/knowledge-base/import` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/knowledge-base/preview-answer` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/knowledge-base/{section}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/knowledge-base/{section}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/live` | No | Public bootstrap/health endpoint | No |
| GET | `/api/ready` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/runtime/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/templates` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates/generate-defaults` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates/sync-meta` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates/create-meta` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates/preview-meta` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/templates/debug` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/templates/test` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/send-template` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/logs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/webhook` | No | Public endpoint; constant-time verify-token check | Meta verify token |
| POST | `/api/whatsapp/webhook` | No | Public endpoint; mandatory Meta HMAC-SHA256 verification | Webhook signature |
| GET | `/api/whatsapp/webhook/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/debug/phone/{phone}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/conversations` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/conversations/{conv_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/conversations/{conv_id}/messages` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/whatsapp/messages/{message_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/whatsapp/conversations/{conv_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/whatsapp/media/{media_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/whatsapp/conversations/{conv_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/conversations/{conv_id}/send` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/conversations/{conv_id}/send-template` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/automation/rules` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/automation/rules` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/automation/rules/reset` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/automation/test` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/automation/actions` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/automation/queue` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/automation/queue/health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/automation/actions/{action_id}/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/automation/actions/run-due` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/leads/incoming` | No | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/confirm/callback` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/confirm/appointment` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/whatsapp/confirm/showroom` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/customer-setup/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/customer-setup` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/setup/trunk` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/logs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/agent-runtime-health` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/logs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/call-logs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/error-logs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/contact-memory` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/appointments` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/campaigns` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/admin/clear/all-test-data` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/lead-statuses` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/lead-statuses` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/lead-statuses/{status_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/import-leads` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/upload-leads` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/sample-leads.csv` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/contacts` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/summary` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/export/leads.csv` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/export/leads.xlsx` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/contacts/{phone}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/contacts/{phone}/followup-state` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/schedule-followup` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/stop-automation` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/resume-automation` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/followups/due` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/followups/run-due` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/followups` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/dashboard/summary` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/reports/summary` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/reports/calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/reports/whatsapp` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/reports/appointments` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/reports/leads` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/costs/summary` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/kanban` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/move-stage` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/crm/tags` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/tags` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/tags` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/crm/contacts/{phone}/tags/{tag}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/crm/contacts/{phone}/custom-fields` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/broadcast/campaigns` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/broadcast/campaigns` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/broadcast/campaigns/{campaign_id}/preview` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/broadcast/campaigns/{campaign_id}/start` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/broadcast/campaigns/{campaign_id}/pause` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/broadcast/campaigns/{campaign_id}/recipients` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/handoff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/contacts/{phone}/resolve-handoff` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/handoffs` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/crm/call-selected` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PUT | `/api/crm/contacts/{phone}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/crm/contacts/{phone}/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/crm/contacts/{phone}/followup` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/crm/contacts/{phone}/notes` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/crm/contacts/{phone}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/agent-profiles` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/agent-profiles` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/agent-profiles/{profile_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PUT | `/api/agent-profiles/{profile_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/agent-profiles/{profile_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/agent-profiles/{profile_id}/set-default` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/batch-calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/batch-calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/batch-calls/{batch_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/batch-calls/{batch_id}/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/campaigns` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/campaigns` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| DELETE | `/api/campaigns/{campaign_id}` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/campaigns/{campaign_id}/run` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/campaigns/{campaign_id}/progress` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| PATCH | `/api/campaigns/{campaign_id}/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/scheduled-calls` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/scheduled-calls/start-due-today` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/outbound/schedule/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| POST | `/api/outbound/schedule/settings` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |
| GET | `/api/outbound/schedule/status` | Conditional on `AUTH_ENABLED` | Unconditional session or admin API-key gate | Yes |

## Tests performed

All checks passed:

- `python -m py_compile server.py test_critical_security.py`
- `python -c "import server; print('import_ok', len(server.app.routes))"`
- `python -m unittest discover -v`
- AST/application route inventory compared with the exact public-route allowlist.
- Verified valid Meta HMAC acceptance at the helper level.
- Verified malformed, missing, wrong-algorithm, wrong-secret, and missing-secret signatures are rejected.
- Verified route-level invalid signature returns 403.
- Verified route-level correctly signed malformed JSON returns 400.
- Verified GET verify-token success and wrong-token 403 behavior.
- Verified `POST /api/leads/incoming` and detailed `GET /api/health` are no longer public; minimal `GET /api/live` remains public for Docker/Coolify.

Result: see the final test run recorded during review. Import emitted only the expected local warning that Supabase configuration was absent; no database operation or schema change was performed.

## Production configuration required

Before deployment, production must have:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- a strong, stable `SESSION_SECRET`
- `META_APP_SECRET` matching the Meta application that signs webhook deliveries
- `WEBHOOK_VERIFY_TOKEN` or `WHATSAPP_VERIFY_TOKEN` matching the Meta webhook subscription token
- `ADMIN_API_KEY` for integrations that previously relied on anonymous access, including any n8n/Facebook/website caller of `POST /api/leads/incoming`

The `AUTH_ENABLED` setting no longer disables authentication. Callers of previously exposed endpoints must authenticate with the admin session or existing API-key mechanism. This is intentionally security-breaking and should be coordinated before deployment.

If the shared webhook URL is currently used by the Vobiz provider rather than Meta, deployment requires provider-specific signature compatibility review. This implementation enforces the requested Meta signature protocol and will reject non-Meta unsigned deliveries.

## Explicitly unchanged

- All SQL files and RLS policies
- Supabase schema and database access logic
- UI and login presentation
- CRM, appointment, calling, WhatsApp message, automation, and export business logic
- Dependencies and deployment files
- Git history and production state

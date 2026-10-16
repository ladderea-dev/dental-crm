# AirZen Production Configuration Audit

No secret values are printed. Presence must be verified from the deployment environment or staging environment only.

| Variable | Required | Code Location | Production Check | Staging Check | Effect When Missing | Current Behavior | Recommendation |
|---|---:|---|---|---|---|---|---|
| `SUPABASE_URL` | yes | `db.py` | present/missing only | must use isolated staging value | database unavailable | may fail at runtime | fail fast |
| `SUPABASE_SERVICE_KEY` | yes | `db.py` | present/missing only | staging service role only | backend writes fail | may fail at runtime | fail fast |
| `STAGING_SUPABASE_URL` | staging | gate script | n/a | must differ from production | staging DB tests unsafe | gate fails | keep gate failure |
| `META_APP_SECRET` | yes | `server.py` | present/missing only | present/missing only | webhook signatures rejected or unverifiable | webhook rejects if absent | fail fast when WhatsApp enabled |
| `WEBHOOK_VERIFY_TOKEN` | yes | `server.py`, `db.py` | present/missing only | present/missing only | webhook setup fails | verify endpoint rejects | fail fast when WhatsApp enabled |
| `WHATSAPP_ACCESS_TOKEN` | yes | `whatsapp.py` | present/missing only | staging token only | sends fail | failed log created | fail fast for send-enabled mode |
| `WHATSAPP_PHONE_NUMBER_ID` | yes | `whatsapp.py` | present/missing only | staging number only | sends fail | failed log created | fail fast for send-enabled mode |
| `WHATSAPP_GRAPH_VERSION` | optional | `whatsapp.py` | defaults to v20.0 | defaults to v20.0 | old API risk | default used | pin and review regularly |
| `WHATSAPP_DEFAULT_LANGUAGE` | optional | `whatsapp.py` | present or default `en` | same | template language mismatch risk | default used | validate templates against Meta |
| template purpose settings | yes for sends | `whatsapp.py`, `server.py` | present/missing only | present/missing only | notification send failure | visible failure | startup health should flag required templates |
| `ADMIN_USERNAME` | yes | `server.py` | present/missing only | present/missing only | weak/no admin login | depends on auth config | fail fast when auth enabled |
| `ADMIN_PASSWORD` | yes | `server.py` | present/missing only | present/missing only | weak/no admin login | depends on auth config | fail fast when auth enabled |
| `SESSION_SECRET` | yes | `server.py` | present/missing only | present/missing only | insecure sessions | generated/fallback risk | fail fast |
| `APP_TIMEZONE` | yes | `db.py`, `server.py` | should be `Asia/Singapore` | should be `Asia/Singapore` | wrong due windows | fixed default to Singapore | enforce in gate |
| `APPOINTMENT_TIMEZONE` | yes | `db.py` | should be `Asia/Singapore` | should be `Asia/Singapore` | wrong appointment slots | fixed default to Singapore | enforce in gate |
| `FOLLOWUP_TIMEZONE` | yes | `db.py`, `server.py` | should match ops timezone | should match staging | wrong follow-up windows | default now Singapore through app default | enforce |
| `GOOGLE_API_KEY` | yes for AI | `db.py`, `whatsapp.py` | present/missing only | present/missing only | AI replies fall back or fail | fallback response path exists | fail fast if AI enabled |
| `GEMINI_MODEL` | optional | `db.py` | configured/default | configured/default | model drift | default used | pin production model |
| `LIVEKIT_URL` | required for calls | `db.py`, `server.py` | present/missing only | present/missing only | calls cannot dispatch | actions may queue/fail | fail fast for call automations |
| `LIVEKIT_API_KEY` | required for calls | `db.py`, `server.py` | present/missing only | present/missing only | calls cannot dispatch | actions may queue/fail | fail fast for call automations |
| `LIVEKIT_API_SECRET` | required for calls | `db.py`, `server.py` | present/missing only | present/missing only | calls cannot dispatch | actions may queue/fail | fail fast for call automations |
| Google Calendar service account | optional | `db.py` | present/missing only | staging calendar only | Meet links unavailable | fallback allowed | health should not report usable unless full JSON parses |
| `GOOGLE_CALENDAR_ENABLED` | optional | `db.py` | boolean | boolean | no calendar integration | internal appointment continues | okay if explicit |
| `N8N` URL variables | optional if referenced | repo search | not actively required in code paths audited | not required | webhook integrations unavailable | no active core dependency found | document if reintroduced |
| `STAGING_SUPABASE_SERVICE_KEY` | staging | `scripts/run_airzen_production_gate.py` | n/a | required for guarded staging writes | staging DB verification blocked | gate fails closed | provide only for isolated staging |
| `AIRZEN_TEST_ENVIRONMENT` | staging guard | `scripts/run_airzen_production_gate.py` | n/a | must equal `staging` | refuses DB writes | gate fails closed | set only for staging |
| `ALLOW_STAGING_DB_WRITES` | staging guard | `scripts/run_airzen_production_gate.py` | n/a | must equal `true` | refuses DB writes | gate fails closed | set only when safe |

Misleading startup status found: `supabase_configured=True` is set after a successful settings read, not after complete schema/config validation. The production gate now performs explicit presence checks and fails staging if isolated DB-write tests were not executed.

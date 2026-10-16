# Security and phishing-risk audit

Audit date: 2026-07-11  
Scope: \`D:\Vloicecallcrm-main\Vloicecallcrm-main\` at commit \`ebd737c\`  
Method: read-only source/configuration/history review plus safe GET requests to the five approved URLs. Secret values were not printed. No migration was run.

## 1. Executive summary

The repository does not contain an obvious injected miner, hidden iframe, encoded malicious payload, or unknown remote loader. The Google warnings must not be treated as false positives, however. The application has security conditions that plausibly explain both warnings:

- The production root serves an admin-style login/dashboard at \`voice.airzensg.com\` with the stale title **“OutboundAI — Dashboard”**, no canonical or robots metadata, and no \`robots.txt\`; this is inconsistent with AirZen branding.
- Authentication is custom and **defaults off**. When \`AUTH_ENABLED\` is false or missing, all sensitive APIs become public and \`/api/auth/me\` claims the visitor is authenticated as “local”.
- The public WhatsApp webhook accepts unsigned POST bodies and starts message-processing side effects. The code itself reports signature verification as “placeholder”.
- All 20 application tables are explicitly created with RLS disabled. The supplied remediation migration is unsafe as written because its admin helper trusts user-editable \`user_metadata\`.
- The live voice site returned no CSP, HSTS, X-Frame-Options/frame-ancestors, X-Content-Type-Options, Referrer-Policy, or Permissions-Policy in the sampled response.

Overall risk: **Critical until production authentication state, Supabase grants, and webhook authenticity are verified and corrected.**

## 2. Critical confirmed vulnerabilities

### F-01 — Unsigned public WhatsApp webhook

- Severity: Critical
- Status: Confirmed
- File: \`server.py\`
- Lines: 242-251, 4013-4033; corroboration 1113-1125
- Evidence: POST \`/api/whatsapp/webhook\` is explicitly public. It parses arbitrary JSON and schedules \`handle_inbound_whatsapp_message()\` without validating Meta \`X-Hub-Signature-256\`, an HMAC, timestamp, source, or replay identifier. Security health labels signature verification “placeholder”.
- Impact: forged inbound messages/statuses, CRM mutation, automated AI replies/actions, cost generation, spam, and log pollution.
- Safe remediation: verify the raw request body with the Meta App Secret and constant-time comparison before parsing; reject missing/invalid signatures; add idempotency/replay protection, body-size limits, and rate limits.
- Production approval required: Yes (Meta/Vobiz compatibility must be tested).

### F-02 — Repository schema deliberately disables RLS on sensitive tables

- Severity: Critical
- Status: Confirmed configuration defect; direct anonymous exploitability remains unverified
- File: \`supabase_schema.sql\`
- Lines: 41-44, 67, 124, 141, 156, 166, 183, 231, 239, 271, 285, 312, 414, 440, 474, 498
- Evidence: explicit \`DISABLE ROW LEVEL SECURITY\` statements cover settings, logs, CRM contacts, messages, conversations, appointments, campaigns and automation data. Supabase separately reports the same production state. No protective GRANT/REVOKE statements exist in this schema.
- Impact: if \`anon\` or \`authenticated\` retains table privileges (common in Supabase API exposure), browser/API clients can read or mutate PII, prompts, configuration, messages, and operational data.
- Safe remediation: first inventory effective grants and client use; revoke \`anon\`; enable tested deny-by-default/admin policies table-by-table in staging; keep service-role use server-side.
- Production approval required: Yes. Do not run the existing migration unchanged.

## 3. High-risk issues

### F-03 — Authentication fails open and protects APIs only conditionally

- Severity: High (Critical if production \`AUTH_ENABLED=false\`)
- Status: Confirmed in code; production value not verified
- File: \`server.py\`
- Lines: 240-281, 324-334, 1091-1103; \`supabase_schema.sql\` 366-368
- Evidence: missing configuration falls back to \`false\`; the seed inserts \`AUTH_ENABLED=false\`; middleware performs no authentication when false; \`/api/auth/me\` returns authenticated/local. Nearly all sensitive endpoints rely solely on this middleware.
- Impact: public access to contacts, recordings, messages, exports, settings, prompts, logs, call dispatch, broadcasts, destructive admin clears, and integrations.
- Safe remediation: fail closed in production, require an explicit secure development override, enforce authentication independently of mutable database settings, and add startup refusal when production auth is not configured.
- Production approval required: Yes.

### F-04 — Public unauthenticated lead-ingestion endpoint lacks request authentication

- Severity: High
- Status: Confirmed
- File: \`server.py\`
- Lines: 242-251, 4488-4563
- Evidence: POST \`/api/leads/incoming\` is explicitly public and has no HMAC/API-key verification visible at the route boundary.
- Impact: attackers can inject leads, trigger automations/calls/messages, create costs, and poison CRM data.
- Safe remediation: require a per-source HMAC/API key, timestamp and nonce; rate-limit; validate source and payload size.
- Production approval required: Yes.

### F-05 — Existing RLS migration grants admin from user-editable metadata

- Severity: High
- Status: Confirmed
- File: \`enable_rls_and_policies.sql\`
- Lines: 9-31, especially 19, 21, 23; policies 58-108
- Evidence: \`SECURITY DEFINER\` helper accepts admin role flags from both \`app_metadata\` and \`user_metadata\`. Supabase users can normally update their own user metadata.
- Impact: an authenticated user could self-assign an admin role and obtain CRUD access to every listed table after the migration.
- Safe remediation: trust only server-controlled \`app_metadata\` or a protected membership table; set \`search_path = pg_catalog, public\` (or fully qualify all objects); keep PUBLIC revoked; test token refresh behavior.
- Production approval required: Yes.

### F-06 — No login rate limiting or lockout

- Severity: High
- Status: Confirmed
- File: \`server.py\`
- Lines: 1071-1088
- Evidence: unlimited username/password comparisons; no IP/account throttling, delay, lockout, CAPTCHA, or audit event.
- Impact: online password guessing and credential stuffing.
- Safe remediation: proxy and application rate limits, exponential backoff, generic errors, alerting, and strong unique credentials.
- Production approval required: Yes.

## 4. Medium-risk findings

### F-07 — Missing security headers on code and sampled production response

- Severity: Medium
- Status: Confirmed
- File: \`server.py\`
- Lines: 238-352, 1281-1286 (no response-header middleware)
- Live evidence: \`https://voice.airzensg.com/\` returned 200 with Server: uvicorn but none of CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, or authenticated-page Cache-Control.
- Impact: clickjacking, weaker XSS containment, MIME sniffing, referrer leakage, and caching uncertainty.
- Remediation: add CSP with nonces/hashes, \`frame-ancestors 'none'\`, HSTS at the trusted TLS edge, \`nosniff\`, strict referrer policy, permissions policy, and \`Cache-Control: no-store\` for dashboard/authenticated/API responses.
- Production approval required: Yes (CSP needs testing).

### F-08 — Uploads have no size/MIME/row limits

- Severity: Medium
- Status: Confirmed
- File: \`server.py\`
- Lines: 5356-5388
- Evidence: reads the complete upload into memory, trusts only filename suffix, and materializes every workbook row.
- Impact: memory/CPU denial of service and parser abuse.
- Remediation: enforce proxy and application byte/row limits, validate magic/type, reject macros and malformed archives, and stream CSV processing.
- Production approval required: Yes.

### F-09 — Spreadsheet formula injection in exports

- Severity: Medium
- Status: Confirmed
- File: \`server.py\`
- Lines: 587-598, 1662-1709, 5464-5502
- Evidence: user-controlled CRM/log values are written directly into CSV/XLSX cells with no neutralization of leading \`=\`, \`+\`, \`-\`, or \`@\`.
- Impact: opening an export in spreadsheet software may execute formulas or exfiltrate data.
- Remediation: prefix risky cells with an apostrophe or use a documented safe export encoding; test phone-number preservation.
- Production approval required: No for code preparation; Yes for deployment.

### F-10 — Arbitrary recording object key access and sensitive key logging

- Severity: Medium
- Status: Confirmed if authentication is enabled; High if disabled
- File: \`server.py\`
- Lines: 1839-1888
- Evidence: caller supplies the S3 object key directly; no ownership/prefix/record linkage check; full key is logged.
- Impact: authenticated IDOR across recordings, and sensitive object-name leakage to logs.
- Remediation: accept a recording ID, resolve an authorized key server-side, restrict prefixes/types, and redact logged keys.
- Production approval required: Yes.

### F-11 — Backend returns internal exception messages

- Severity: Medium
- Status: Confirmed
- File: \`server.py\`
- Lines: 337-352, 5386-5388
- Evidence: unhandled exception text and upload parser exceptions are returned to clients.
- Impact: implementation/configuration disclosure useful for targeted attacks.
- Remediation: return stable generic errors with correlation IDs; retain details only in redacted server logs.
- Production approval required: No for code preparation; Yes for deployment.

### F-12 — Unpinned dependencies and mutable Docker base

- Severity: Medium
- Status: Confirmed
- File: \`requirements.txt\` lines 1-20; \`Dockerfile\` lines 1, 9-10
- Evidence: all packages use lower bounds or no version; base image is tag-only; pip itself is upgraded during build.
- Impact: non-reproducible builds and supply-chain drift.
- Remediation: generate a reviewed hash-locked requirements file, pin base image digest, use dependency scanning, and remove runtime build drift.
- Production approval required: Yes.

## 5. Low-risk findings

### F-13 — Forwarded-proto is trusted from any client

- Severity: Low
- Status: Confirmed
- File: \`server.py\`
- Lines: 319-321, 1079-1086
- Evidence: any \`X-Forwarded-Proto: https\` makes the session cookie Secure; there is no documented trusted-proxy enforcement.
- Impact: proxy confusion and inconsistent cookie behavior; not a direct cookie disclosure.
- Remediation: strip/overwrite forwarded headers at the edge and trust only known proxies.
- Production approval required: Yes.

### F-14 — Service fingerprint exposure

- Severity: Low
- Status: Confirmed live
- Evidence: voice responses expose \`Server: uvicorn\`.
- Impact: minor reconnaissance value.
- Remediation: suppress upstream Server header at proxy.
- Production approval required: No/edge configuration review.

### F-15 — Legacy branding and artifacts

- Severity: Low security / High phishing-reputation relevance
- Status: Confirmed
- Files: \`server.py\` 1, 238; \`ui/index.html\` 6; \`start.sh\` 6; \`OutboundAI_End_to_End_Project_Documentation.pdf\`, \`.docx\`; \`phase*.patch\`
- Evidence: public title is “OutboundAI”, while production/customer brand is AirZen; old project documents and large historical patches remain.
- Impact: brand inconsistency resembles a copied login portal and creates noisy compromise indicators.
- Remediation: align product/company/legal/contact branding; keep patch/docs out of deployed images.
- Production approval required: Yes for public branding.

## 6. Possible causes of Google “Deceptive pages”

1. Confirmed: public admin/login/dashboard at the domain root, stale “OutboundAI” title, and AirZen domain mismatch.
2. Confirmed: no robots/canonical metadata; \`robots.txt\` and \`sitemap.xml\` return 404, so the login-root is crawlable.
3. Confirmed: missing anti-framing/CSP headers permits visual embedding.
4. Possible: exposed APIs or unsigned webhooks may have been abused to host/reflect deceptive CRM content; no evidence of such content was found in the repository.
5. Possible: historical or live compromise outside this repository (proxy, DNS, injected production volume, Supabase content) could not be excluded.
6. Likely false positive only for malicious-code injection: no hidden iframe, meta refresh, miner, obfuscated remote loader, or encoded executable payload was found in tracked source. This does **not** make the Google warning itself a false positive.

## 7. Possible causes of “Possible Phishing Detected on User Login”

- Confirmed: login form is at \`/\` and submits username/password by same-origin JSON to \`/api/auth/login\` (\`ui/index.html\` 567-577, 1844; \`server.py\` 1071-1088). It does not submit credentials to Google, Meta, Microsoft, WhatsApp, or another external host.
- Confirmed: page title/brand says OutboundAI rather than AirZen; legal pages say generic “Voice Call CRM” and lack a concrete owner/contact (\`server.py\` 1296, 1391, 1404, 1443-1444).
- Confirmed: no canonical, robots meta, or recognizable AirZen trust metadata on the dashboard.
- Possible: lack of noindex and public root placement caused automated classifiers to treat it as an impersonating credential page.
- Recommended noindex,nofollow: \`/\` and all dashboard/auth/setup/debug views; APIs should use \`X-Robots-Tag: noindex, nofollow, noarchive\`. Keep public legal/marketing pages indexable only after brand/contact review.

## 8. Supabase exposure and RLS findings

The backend uses a server-side service key through \`db.py\`; no Supabase key is rendered into \`ui/index.html\`. No tracked secret file name was found. The browser calls only the FastAPI API.

Affected tables: settings, error_logs, call_logs, appointment_staff, contact_memory, campaigns, agent_profiles, lead_statuses, crm_contacts, crm_tags, crm_custom_fields, broadcast_campaigns, broadcast_recipients, followup_actions, whatsapp_conversations, whatsapp_messages, automation_actions, whatsapp_logs, appointments.

Access pattern: all are accessed through backend code using the service role; no direct browser Supabase client was found. Effective production GRANTs and bucket policies could not be queried, so anonymous exploitability is not asserted as confirmed.

Safe migration plan:

1. Export effective table/sequence/function/schema grants and policies; inventory storage buckets.
2. Remove \`user_metadata\` from \`is_app_admin()\`; fully qualify objects and harden search_path.
3. Revoke \`anon\` table/sequence/function privileges before relying on RLS.
4. Test policies and service-role backend in staging, including every CSV route and background worker.
5. Enable RLS table-by-table with deny-by-default tests; start with settings, contacts, messages, recordings metadata and logs.
6. Verify authenticated non-admin denial and admin CRUD with fresh JWTs.
7. Deploy in a maintenance window with rollback SQL and monitoring.
8. Do not use FORCE RLS until backend ownership/service-role behavior is intentionally redesigned.

## 9. Suspicious files, scripts, dependencies, or domains

No confirmed malicious file or domain was found. Reviewed domains were expected service/documentation endpoints: Meta Graph, Google APIs/fonts/maps, Vobiz, Cal.com, Telegram, jsDelivr, GitHub, localhost/127.0.0.1, wa.me, and placeholders. \`cdn.jsdelivr.net\` loads Chart.js without Subresource Integrity (\`ui/index.html\` 7), which is a supply-chain/CSP risk.

Unusual/noisy artifacts: ten \`phase*.patch\` files, legacy OutboundAI PDF/DOCX, zero-byte server logs, and a temporary Office lock file \`~$AI_Calling_Testing_Checklist.xlsx\`. Patch files contain extensive historical \`innerHTML\` code but no confirmed malicious loader. Docker \`COPY . .\` includes documents/patches not excluded by \`.dockerignore\`.

## 10. Public routes and unauthenticated endpoints

The complete route inventory is in \`public_routes_audit.csv\`. Explicit public APIs are health/live, auth login/me/logout, both WhatsApp webhook methods, and incoming lead ingestion. All other API routes are only conditionally authenticated. HTML \`/\`, privacy, terms, and terms-and-conditions are always public.

There is no per-record ownership or multi-tenant authorization layer; once the single admin gate is passed, IDs/phones supplied to routes are trusted globally. This makes conversation, appointment, CRM and recording endpoints IDOR-prone in any multi-user deployment.

## 11. Exact affected file paths and line numbers

Primary locations are included in each finding. Additional notable locations:

- Login UI and submission: \`ui/index.html\` 567-577, 1205-1212, 1844
- Escaping/table rendering: \`ui/index.html\` 1199-1200, 1512, 1789-1795
- Remote CDN: \`ui/index.html\` 7
- WhatsApp media proxy: \`server.py\` 4145-4155; \`whatsapp.py\` 2383-2431
- Destructive endpoints: \`server.py\` 4847-4896
- CRM uploads/exports: \`server.py\` 5356-5388, 5464-5502
- Public recording access logic: \`server.py\` 1839-1888
- Startup information logging: \`start.sh\` 6, 29-32
- Mutable base/build: \`Dockerfile\` 1, 9-10

## 12. Safe remediation plan in priority order

1. Immediately verify production \`AUTH_ENABLED\` and effective Supabase grants without exposing values; restrict external access at the proxy while validating.
2. Add webhook signature verification/replay controls and authenticate lead ingestion.
3. Replace the unsafe RLS helper and stage a table-by-table RLS/grant migration.
4. Fail closed on authentication, add rate limits, rotate credentials only under a separately approved incident procedure, and invalidate sessions if compromise is suspected.
5. Move the admin dashboard off the public marketing root or require edge authentication; add noindex and consistent AirZen legal branding/contact information.
6. Add security headers, no-store, trusted-proxy configuration, and remove server fingerprinting.
7. Add upload/export/recording controls and generic errors.
8. Pin dependencies/base images and minimize the production build context.
9. After deployment and clean rescans, request Google review with verified facts only.

## 13. Changes safe to apply immediately

These are safe to prepare in source but were not applied in this read-only audit: robots/noindex metadata, consistent AirZen titles/legal owner/contact details, generic error responses, CSV formula neutralization, removal of obsolete build-context artifacts, security-header tests, dependency lock generation, and webhook/auth test suites.

## 14. Changes requiring approval because they may break production

Authentication fail-closed changes; webhook signature enforcement; incoming-lead authentication; RLS/GRANT changes; session/credential rotation; proxy/forwarded-header changes; CSP; URL restructuring; rate limits; storage access redesign; dependency upgrades; deployment or Search Console resubmission.

## 15. Suggested Google Search Console review statement based only on verified findings

> We completed a read-only security review of the application source and public pages. We verified that the root page was an administrative login/dashboard with legacy “OutboundAI” branding on an AirZen domain, lacked noindex/canonical metadata and key browser security headers, and that public webhook handling did not yet validate request signatures. We also identified database RLS and authentication configuration that required hardening. We found no hidden iframe, meta-refresh redirect, cryptominer, obfuscated remote loader, or credential submission to a third-party domain in the reviewed source. After the approved fixes are deployed and independently verified, we will request a new review and provide the final deployed URLs and controls.

Do not submit this statement until the listed fixes are actually deployed and verified.

## 16. Exact audit commands used

All commands were read-only except creation of this report and CSV.

\`\`\`text
Get-Content -Raw -LiteralPath <attached-request>
rg --files
Get-ChildItem -Force
git status --short --branch
git branch -vv
git log --oneline --decorate -30
git remote -v
git diff --stat
git ls-files
git shortlog -sne HEAD
Python ast.parse route/decorator inventory
rg -n -i <suspicious primitive/auth/header/upload/domain patterns>
Get-Content requirements.txt Dockerfile start.sh .dockerignore .gitignore
curl.exe --noproxy * -sS -L --max-redirs 5 --connect-timeout 10 --max-time 20 -D - -o NUL <approved URL>
Python urllib.request GET/parser for the same five approved URLs
\`\`\`

No POST, credential submission, fuzzing, database connection, migration, dependency installation, commit, push, or deployment was performed.

## 17. Redacted output summary

- Git: clean \`main\`, aligned with \`origin/main\`; 12 visible commits; contributors Karuna and ladderea-dev; remote is the expected ladderea-dev GitHub repository.
- Secret scan: no secret values printed; no tracked env/private-key/credential filename found. Historical secret-value scanning was limited to redacted pattern/location review.
- Live: voice root 200; robots 404; sitemap 404; live health 200; marketing root 200. Voice title “OutboundAI — Dashboard”; marketing title “AirZen Aircon Services”. No redirects observed.
- Live external hosts in voice HTML: jsDelivr, Google Fonts, Google Maps and wa.me; a literal placeholder also matched the parser.
- Malicious-code indicators: none confirmed.

## 18. Items that could not be verified and why

- Production \`AUTH_ENABLED\`, admin/API-key strength, session secret strength: values were intentionally not retrieved.
- Effective Supabase GRANTs, policies, storage buckets and live table exposure: no database credentials/queries were used.
- Whether Google detected a URL/content variant not present now: Search Console supplied no sample URL.
- Reverse-proxy/DNS/WAF/container-volume compromise, access logs and deployed commit identity: unavailable from repository/public GETs.
- Full Git object-history secret scanning with specialized entropy tooling and external breach intelligence: not performed to avoid exposing values and because network/tooling was limited.
- Current dependency CVEs: versions are not locked, so installed production versions are unknown.
- n8n: excluded by context as already audited; no production fuzzing or login was attempted.
- Browser behavior after login, CSRF across the deployed proxy, and authenticated route responses: no credentials were submitted.


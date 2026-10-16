# Google Safe Browsing and phishing-warning remediation report

Date: 2026-07-11  
Scope: Verified Google Safe Browsing/indexability/brand-trust remediation only.

## Outcome

The CRM login and authenticated single-page application now identify AirZen Pte. Ltd. consistently, state that the site is a secure staff CRM portal, show the production hostname `voice.airzensg.com`, and link to the main `https://airzensg.com/` website and the existing privacy page.

The CRM root, authentication/API routes, setup/debug/admin APIs, and FastAPI documentation routes now emit `X-Robots-Tag: noindex, nofollow, noarchive`. The SPA HTML also contains the matching robots meta tag. The separate AirZen marketing website was not changed.

No authentication, RLS, Supabase, webhook, database, or business logic was changed.

## Files changed

### `ui/index.html`

- Lines 4-16: added noindex meta, AirZen description, canonical URL, Open Graph identity, inline AirZen “A” favicon, and the title `AirZen Secure Staff CRM Portal`.
- Login block around lines 578-590: identifies `AirZen Pte. Ltd.`, `Secure staff CRM portal`, and `voice.airzensg.com`; links to the main AirZen site and privacy page.
- The login form explicitly declares same-origin `POST /api/auth/login`; the existing JavaScript handler still prevents default submission and sends JSON to the same endpoint.
- Authenticated desktop, top-bar, and mobile navigation branding now says AirZen and secure staff CRM portal.
- No visible or metadata references to OutboundAI, Ladder Hub, demo companies, or unrelated brands remain. Existing `outboundai-*` localStorage keys remain only as invisible compatibility identifiers so saved user theme/sidebar preferences are not broken.

### `server.py`

- Line 1 and line 238: backend/OpenAPI identity changed to AirZen.
- Lines 321-353: added a response-header helper integrated into the existing middleware without changing its authentication decisions.
- Lines 1306-1328: added `/robots.txt` and `/sitemap.xml`.
- Legal-page template and copy around lines 1337-1522 now identify AirZen Pte. Ltd. and link to the main AirZen website.

### `test_critical_security.py`

- Updated the existing admin API-key middleware test to use a real response and assert the new noindex header. Authentication behavior being tested is unchanged.

### `google_phishing_remediation_report.md`

- This report.

## Indexability policy

| Route/category | Indexability |
|---|---|
| `/` login and authenticated CRM SPA | Meta robots and HTTP `X-Robots-Tag`: noindex, nofollow, noarchive |
| `/api/auth/*` | HTTP noindex; `Cache-Control: no-store` |
| All other `/api/*` except `/api/live` | HTTP noindex |
| Setup/admin/debug APIs | Protected by authentication and HTTP noindex |
| `/docs`, `/redoc`, `/openapi.json`, OAuth redirect helper | HTTP noindex and disallowed in robots |
| `/api/live` | Public and unchanged; no new headers |
| `/privacy`, `/terms`, `/terms-and-conditions` | Public/indexable AirZen legal information; included in CRM sitemap |
| `https://airzensg.com/` | Not modified; remains controlled by the separate public marketing site |

The CRM root is not disallowed in `robots.txt` so crawlers can read and honor its `noindex`. API and documentation paths are disallowed.

## Metadata and branding

The CRM SPA now provides:

- Title: `AirZen Secure Staff CRM Portal`
- Description: secure staff CRM portal for AirZen Pte. Ltd. at the production hostname
- Canonical: `https://voice.airzensg.com/`
- Open Graph type, site name, title, description, and URL
- AirZen-branded inline SVG favicon
- Main website and privacy links
- Explicit staff-only wording without imitating Meta, Google, Microsoft, WhatsApp, a bank, or another credential provider

The legal pages have AirZen titles, descriptions, identity, and main-site contact links. They are intentionally indexable and listed in the sitemap.

## robots.txt and sitemap.xml

`/robots.txt` now:

- disallows `/api/`
- disallows FastAPI documentation/schema paths
- declares `https://voice.airzensg.com/sitemap.xml`

`/sitemap.xml` contains only the three public AirZen legal pages. The login/CRM root, APIs, setup, admin, and debug endpoints are excluded.

## Login and credential-target review

- Form target: same-origin `/api/auth/login`
- JavaScript target: same-origin `/api/auth/login`
- Username/password values are JSON-encoded and sent only to that route.
- No absolute third-party form action exists.
- External AirZen and privacy links do not receive credentials.
- Authentication/session/API-key behavior was not modified.

## Security headers

For CRM/API/documentation responses other than the explicitly unchanged `/api/live`:

- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-Robots-Tag: noindex, nofollow, noarchive` on CRM/API/docs routes
- `Cache-Control: no-store` on the login CRM HTML and authentication endpoints

Existing endpoint-specific cache headers, such as media caching, are preserved because the helper uses safe defaults and only forces no-store on login/auth responses.

## Redirect audit

- No backend redirect response or HTTP 301/302/307/308 implementation was found.
- No meta-refresh redirect was found.
- Frontend `location.href` uses are same-origin download/export actions only.
- Canonical and Open Graph URLs use the expected production CRM domain.
- The main-site link is the expected `https://airzensg.com/`.
- No localhost, IP-address, old-company, or third-party login redirect was introduced.

## Verification performed

Passed:

- `python -m py_compile server.py test_critical_security.py test_followup_time_parser.py`
- Backend import check: AirZen application imported successfully with 200 registered routes.
- `python -m unittest discover -v`: 13 tests passed.
- `python test_followup_time_parser.py`: existing smoke tests passed.
- Frontend JavaScript extraction and `node --check -`: two inline scripts, 184,176 bytes, parse exit 0.
- Direct response-header audit for root, API, auth, docs, live, and privacy routes.
- Direct robots, sitemap, and rendered-root content audit.
- AST audit found no backend redirect calls.
- Source audit found no visible/metadata legacy-brand matches.
- Credential audit found no third-party form action.
- `git diff --check`: passed.

The backend import emitted the expected local warning that Supabase environment variables were not configured; no database connection or schema operation was performed.

## Items intentionally unchanged

- `/api/live` implementation and response headers
- Authentication, sessions, login API, logout, and admin API-key logic
- Meta/WhatsApp webhook verification
- RLS, Supabase, SQL, database schema, and database data
- CRM/voice/WhatsApp/appointment/automation business logic
- Public marketing website at `airzensg.com`
- Production, deployment, Git history, commits, and remotes


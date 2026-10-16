# Supabase RLS security review

Date: 2026-07-11  
Scope: Architecture review and generation of migration/rollback SQL only. No SQL was executed.

## Recommendation

**Choose A: service-role-only backend access with direct `PUBLIC`, `anon`, and `authenticated` grants revoked.**

This is the safest design for the application that actually exists.

- The browser authenticates to FastAPI, not Supabase Auth.
- FastAPI uses a custom signed admin session based on `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `SESSION_SECRET`.
- Server-to-server integrations may authenticate to FastAPI with `ADMIN_API_KEY`.
- The browser calls same-origin FastAPI `/api/*` endpoints and contains no Supabase JavaScript client.
- `db.py` creates synchronous and asynchronous Supabase clients exclusively with `SUPABASE_SERVICE_KEY` (or its service-role alias).
- No application code signs in through Supabase Auth, validates a Supabase user JWT, or requires direct `anon`/`authenticated` table access.
- The Supabase `service_role` database role bypasses RLS; the migration also grants its table privileges explicitly.

Design B would add a second, currently unused authentication/authorization system. Even an app_metadata-only policy would provide no benefit until the application genuinely uses Supabase Auth. Design C is stronger than JWT metadata for a future multi-user application, but it also adds a membership table and server-managed lifecycle that the current single-admin FastAPI architecture does not need.

## Authentication and data-access model

### Custom FastAPI admin session

`server.py` reads `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `SESSION_SECRET`. Login compares the configured credentials and issues an HMAC-signed, expiring cookie. The global middleware accepts that session for protected APIs.

This identity does not map to a Supabase `auth.users` row. PostgreSQL therefore cannot safely infer the FastAPI admin from `auth.uid()`, `auth.jwt()`, or Supabase JWT metadata.

### Admin API key

The same FastAPI middleware accepts the configured `ADMIN_API_KEY` for integrations. That credential is evaluated by FastAPI and is not a Supabase role or JWT.

### Supabase Auth

No active Supabase Auth flow was found:

- no browser Supabase client initialization;
- no `supabase.auth.signIn*` or session use;
- no backend Supabase user JWT validation;
- no dependency on `auth.uid()`;
- no frontend anon key.

Accordingly, neither `anon` nor `authenticated` needs table privileges.

### Service-role backend access

`db.py` normalizes `SUPABASE_SERVICE_ROLE_KEY`/`SUPABASE_KEY` aliases into `SUPABASE_SERVICE_KEY`, then passes that key to both synchronous and asynchronous Supabase clients. All CRM reads/writes occur through those server-side clients.

The safe migration:

- requires the `service_role` database role to exist;
- requires it to have `BYPASSRLS`;
- enables RLS without FORCE;
- grants service_role table privileges explicitly;
- verifies SELECT/INSERT/UPDATE/DELETE capability before commit.

The service-role key must remain server-side and must never be shipped in HTML or frontend JavaScript.

## Defect in the previous migration

`enable_rls_and_policies.sql` is unsafe for this architecture because `public.is_app_admin()` accepts role/is_admin/roles values from `user_metadata`. Supabase users can generally edit their own user metadata. An authenticated user could therefore self-assert an admin value and satisfy policies that grant CRUD on every sensitive table.

Other problems:

- it grants CRUD to every `authenticated` user before relying on the unsafe helper;
- it introduces Supabase Auth authorization that the application does not use;
- it broadens sequence privileges to authenticated users;
- its function comment explicitly treats user metadata as an admin authority.

Do not run `enable_rls_and_policies.sql`.

The new migration drops all policies on the 19 in-scope tables and removes `is_app_admin()` with RESTRICT semantics. If an unexpected dependency exists, the transaction aborts rather than cascading.

## Sensitive-table coverage

The migration and rollback each cover exactly these 19 tables (the supplied scope contains 19 names):

1. `settings`
2. `error_logs`
3. `call_logs`
4. `appointment_staff`
5. `contact_memory`
6. `campaigns`
7. `agent_profiles`
8. `lead_statuses`
9. `crm_contacts`
10. `crm_tags`
11. `crm_custom_fields`
12. `broadcast_campaigns`
13. `broadcast_recipients`
14. `followup_actions`
15. `whatsapp_conversations`
16. `whatsapp_messages`
17. `automation_actions`
18. `whatsapp_logs`
19. `appointments`

## Grant design

For each sensitive table:

| Role | Table privileges after migration | RLS behavior |
|---|---|---|
| `PUBLIC` | Revoked | No inherited direct access |
| `anon` | Revoked | No direct browser access |
| `authenticated` | Revoked | No direct user access, admin or otherwise |
| `service_role` | Explicit ALL table privileges | Bypasses non-FORCE RLS |
| Database owner/postgres | Owner privileges | Intended only for controlled administration |

Owned sequences are handled narrowly: privileges are revoked from PUBLIC/anon/authenticated and granted to service_role. Unrelated public-schema tables and sequences are not changed.

Schema `USAGE` alone does not allow data access after table privileges are revoked. The migration does not alter broad schema ownership or unrelated defaults.

## RLS design

RLS is enabled and `NO FORCE` on all 19 tables. No policies are created.

For roles subject to RLS, an enabled table with no applicable policy is default-deny. The explicit grant revocations provide an additional independent denial layer. Service role continues through its `BYPASSRLS` capability and explicit SQL privileges.

`FORCE ROW LEVEL SECURITY` is deliberately not used because it is unnecessary for service_role and may change table-owner behavior. The preflight and postflight report both flags.

## Preflight coverage

`safe_rls_migration.sql` reports before changing security:

- existence of all 19 tables;
- existence of `anon`, `authenticated`, and `service_role`;
- whether service_role has BYPASSRLS;
- current RLS and FORCE-RLS state;
- table owner;
- effective SELECT/INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER capability by role;
- explicit information-schema grants;
- every current policy, including expressions and target roles;
- exact row counts;
- service-role login/bypass flags and CRUD capability.

The migration uses a repeatable-read transaction, short lock timeout, and bounded statement timeout. Missing prerequisites abort before changes.

Exact counts may be expensive for very large tables. Review the preflight output and runtime in staging before production approval.

## Post-migration verification

The migration reports and asserts:

- RLS enabled on all 19 tables;
- FORCE RLS disabled;
- no remaining policies;
- no effective CRUD privilege for anon/authenticated;
- effective CRUD capability for service_role;
- row counts unchanged.

Any failed hard assertion raises an exception and rolls back the transaction.

Recommended application verification in staging after the SQL transaction:

1. Start the FastAPI server with its normal service-role environment.
2. Verify readiness and schema-health endpoints through an authenticated session.
3. Exercise representative read/write/delete flows for CRM, WhatsApp, appointments, settings, logs, campaigns, and automation.
4. With an anon key in a disposable client, verify SELECT and INSERT fail for every listed table.
5. With a normal Supabase Auth user in a disposable client, verify the same failures.
6. Verify background workers and scheduled jobs continue to read/write.
7. Confirm no service-role key appears in browser network responses or built frontend assets.

## Rollback strategy

`safe_rls_rollback.sql` is intentionally security-preserving rather than a byte-for-byte restoration of an unknown unsafe state.

It:

- disables RLS and FORCE RLS on the 19 tables;
- keeps PUBLIC/anon/authenticated privileges revoked;
- keeps service_role privileges;
- preserves the same sequence restrictions;
- verifies that browser roles remain denied and service_role remains capable.

It does not recreate unknown preexisting policies, restore direct browser grants, or recreate the unsafe metadata helper. Exact restoration must use a reviewed preflight snapshot or database backup. Reopening anon/authenticated access is not considered a safe rollback.

In normal conditions the backend should not need this rollback because service_role bypasses non-FORCE RLS. Use it only after staging evidence shows an unexpected incompatibility.

## Deployment order and approval gates

1. Do not run the old migration.
2. Review all preflight query outputs in a staging clone.
3. Confirm the production backend key is a service-role key, without printing it.
4. Confirm no external consumer directly queries these tables with anon/authenticated credentials.
5. Run the safe migration in staging.
6. Complete the application verification list.
7. Take a production backup and record current grants/policies.
8. Schedule a maintenance/change window.
9. Run in production only with explicit approval and monitoring.
10. Retain the security-preserving rollback and backup reference.

Re-running `supabase_schema.sql` after this migration would disable RLS again because that file contains explicit `DISABLE ROW LEVEL SECURITY` statements. Deployment procedures must apply the safe migration after schema bootstrap, and the bootstrap schema should be corrected in a separately approved change before it is used again.

## Files generated

- `safe_rls_migration.sql`: guarded preflight, migration, and postflight verification.
- `safe_rls_rollback.sql`: security-preserving rollback.
- `rls_security_review.md`: this review.

No SQL was run. No production, Supabase, schema, application code, commit, push, or deployment action was performed.

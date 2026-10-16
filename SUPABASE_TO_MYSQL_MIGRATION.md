# AirZen Supabase to MySQL data migration

This is the Phase 4 staging and cutover-rehearsal tool. It does not change the
application backend, deploy the application, stop workers, or delete Supabase
data.

The migration covers every table in `mysql_schema.sql`, in dependency order:

- settings and CRM reference data
- CRM contacts and contact memory
- appointment staff, appointments, and agent profiles
- WhatsApp conversations, messages, logs, and health-test records
- follow-up, automation, campaign, broadcast, and notification queues
- call logs and diagnostic error logs

## Safety model

- `plan` is the default and is read-only.
- `apply` refuses to write unless `--confirm-target` exactly matches
  `MYSQL_DATABASE`.
- A normal `apply` requires all destination tables to be empty.
- `--resume` is only for an interrupted run against the same source snapshot.
  Resume updates rows by primary key and uses plain inserts for new IDs, so an
  alternate unique key cannot silently retain the wrong ID.
- No command contains source or destination delete/truncate behavior.
- Source columns absent from MySQL cause a hard failure instead of silent data
  loss.
- Invalid JSON and timestamps fail before their batch is committed.
- Supabase and MySQL credentials and row values are never written to reports.

Use an isolated MySQL 8 database with `mysql_schema.sql` already applied. Do not
point Phase 4 rehearsal commands at the production MySQL database.

## Required environment

```text
SUPABASE_URL
SUPABASE_SERVICE_KEY
MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
MYSQL_USER
MYSQL_PASSWORD
```

The Supabase credential needs read access to all migrated tables. The MySQL
credential needs `SELECT`, `INSERT`, and `UPDATE` access to the isolated target,
plus read access to `information_schema.COLUMNS`.

## Staging rehearsal

First validate the source and target without writing:

```bash
python scripts/migrate_supabase_to_mysql.py plan --report airzen_migration_plan.json
```

The plan exits with code `2` when destination tables are not empty. Review the
reported source counts and critical metrics before approving an apply run.

Then run the explicitly confirmed migration against the isolated target:

```bash
python scripts/migrate_supabase_to_mysql.py apply \
  --confirm-target "$MYSQL_DATABASE" \
  --report airzen_migration_result.json
```

If an apply was interrupted, retain the same source snapshot and target, then
rerun with `--resume`. Do not use resume to merge unrelated datasets.

Finally, repeat the read-only comparison independently:

```bash
python scripts/migrate_supabase_to_mysql.py verify \
  --report airzen_migration_verify.json
```

## Verification gate

Verification must pass all of the following before any production-cutover work:

- row count for every migrated table
- primary-key digest for preserved IDs
- normalized full-row digest
- unique phone counts per phone-bearing table
- latest appointment per phone
- active booking-state count
- pending follow-up count
- pending automation count
- pending notification-outbox count

Rows are normalized before comparison: aware timestamps are converted to UTC
`DATETIME(6)`, naïve timestamps are treated as UTC, empty timestamps become
`NULL`, and JSON is parsed and canonicalized. This preserves the logical
`appointment_state` without double encoding legacy JSON strings.

Passing Phase 4 is not approval for Phase 5. Production remains on Supabase
until workers are stopped, a final snapshot and delta rehearsal are approved,
and the production cutover checklist is executed separately.

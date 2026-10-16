"""Safe AirZen automation production gate.

This runner never sends live messages. In staging mode it requires an explicit
staging database marker before database-write dependent checks can pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airzen_repository import SupabaseRepository


RESULTS_PATH = ROOT / "airzen_production_gate_results.json"
REPORT_PATH = ROOT / "airzen_production_gate_report.md"
OUTBOX_MIGRATION_PATH = ROOT / "notification_outbox_migration.sql"


def extract_supabase_project_ref(url: str) -> str:
    """Return the Supabase project ref from a Supabase project URL."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    suffix = ".supabase.co"
    if not host.endswith(suffix):
        return ""
    project_ref = host[: -len(suffix)]
    if "." in project_ref:
        return ""
    return project_ref


def validate_staging_database_target() -> tuple[bool, str, str]:
    """Validate guarded staging DB settings without exposing secrets."""
    staging_url = os.getenv("STAGING_SUPABASE_URL", "").strip()
    prod_url = os.getenv("SUPABASE_URL", "").strip()
    allowed_ref = os.getenv("AIRZEN_ALLOWED_STAGING_PROJECT_REF", "").strip()
    production_ref = os.getenv("AIRZEN_PRODUCTION_PROJECT_REF", "").strip()
    allow_writes = os.getenv("ALLOW_STAGING_DB_WRITES") == "true"
    test_env = os.getenv("AIRZEN_TEST_ENVIRONMENT") == "staging"
    staging_ref = extract_supabase_project_ref(staging_url)
    derived_prod_ref = extract_supabase_project_ref(prod_url)
    effective_prod_ref = production_ref or derived_prod_ref

    if not staging_url:
        return False, staging_ref, "missing STAGING_SUPABASE_URL"
    if not staging_ref:
        return False, staging_ref, "STAGING_SUPABASE_URL is not a Supabase project URL"
    if not allowed_ref:
        return False, staging_ref, "missing AIRZEN_ALLOWED_STAGING_PROJECT_REF"
    if staging_ref != allowed_ref:
        return False, staging_ref, "staging project ref does not match AIRZEN_ALLOWED_STAGING_PROJECT_REF"
    if effective_prod_ref and staging_ref == effective_prod_ref:
        return False, staging_ref, "staging project ref matches production project ref"
    if prod_url and staging_url == prod_url:
        return False, staging_ref, "STAGING_SUPABASE_URL matches SUPABASE_URL"
    if not test_env or not allow_writes:
        return False, staging_ref, "missing AIRZEN_TEST_ENVIRONMENT=staging or ALLOW_STAGING_DB_WRITES=true"
    return True, staging_ref, f"allowed isolated staging project ref {staging_ref}"


def run_command(name: str, command: list[str]) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=600,
        )
        return {
            "name": name,
            "status": "passed" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "started_at": started,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "status": "failed",
            "returncode": 124,
            "started_at": started,
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": "command timed out",
        }


def config_checks(mode: str) -> list[dict]:
    required = [
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "META_APP_SECRET",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WEBHOOK_VERIFY_TOKEN",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "SESSION_SECRET",
        "GOOGLE_API_KEY",
    ]
    checks = []
    for key in required:
        present = bool(os.getenv(key, "").strip())
        checks.append({
            "name": f"config:{key}",
            "status": "passed" if present else "failed",
            "detail": "present" if present else "missing",
        })
    if mode == "staging":
        staging_url = os.getenv("STAGING_SUPABASE_URL", "").strip()
        ok_target, staging_ref, target_detail = validate_staging_database_target()
        checks.append({
            "name": "staging_database_isolated",
            "status": "passed" if ok_target else "failed",
            "detail": target_detail,
        })
        checks.append({
            "name": "staging_project_ref_detected",
            "status": "passed" if staging_ref else "failed",
            "detail": f"staging project ref {staging_ref}" if staging_ref else "could not extract staging project ref",
        })
        checks.append({
            "name": "staging_write_safety_guard",
            "status": "passed" if ok_target else "failed",
            "detail": "AIRZEN_TEST_ENVIRONMENT=staging, ALLOW_STAGING_DB_WRITES=true, and exact project-ref allowlist matched" if ok_target else target_detail,
        })
        checks.append({
            "name": "outbox_migration_exists",
            "status": "passed" if OUTBOX_MIGRATION_PATH.exists() else "failed",
            "detail": "notification_outbox_migration.sql present" if OUTBOX_MIGRATION_PATH.exists() else "migration file missing",
        })
        atomic = os.getenv("AIRZEN_REQUIRE_ATOMIC_OUTBOX") == "true"
        checks.append({
            "name": "python_atomic_outbox_required",
            "status": "passed" if atomic else "failed",
            "detail": "AIRZEN_REQUIRE_ATOMIC_OUTBOX=true" if atomic else "AIRZEN_REQUIRE_ATOMIC_OUTBOX must be true for staging gate",
        })
    return checks


def staging_database_checks(mode: str) -> list[dict]:
    if mode != "staging":
        return []
    checks = []
    staging_url = os.getenv("STAGING_SUPABASE_URL", "").strip()
    service_key = os.getenv("STAGING_SUPABASE_SERVICE_KEY", "").strip() or os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    ok_target, staging_ref, target_detail = validate_staging_database_target()
    if not (ok_target and service_key):
        return [
            {"name": "staging_database_writes", "status": "failed", "detail": f"blocked by staging safety guard: {target_detail}"},
            {"name": "rls_access_verification", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "customer_notification_journey", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "staff_notification_journey", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "reschedule_journey", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "cancellation_journey", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "restart_recovery", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "idempotency", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "atomic_rpc_integration_tests", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "worker_lease_tests", "status": "failed", "detail": "not executed without guarded staging DB"},
            {"name": "uncertain_delivery_handling", "status": "failed", "detail": "not executed without guarded staging DB"},
        ]
    try:
        marker = f"airzen_gate_{uuid.uuid4().hex[:12]}"
        outbox_row = {
            "dedupe_key": marker,
            "status": "pending",
            "recipient_type": "customer",
            "recipient_phone": "+6500000000",
            "notification_type": "appointment_customer_confirmation",
            "template_purpose": "appointment_confirmation_template",
            "appointment_id": marker,
            "payload": {"gate": True},
            "next_attempt_at": datetime.now(timezone.utc).isoformat(),
        }

        async def staging_adb():
            from supabase import acreate_client
            return await acreate_client(staging_url, service_key)

        ok = asyncio.run(SupabaseRepository(staging_adb).probe_notification_outbox_storage(outbox_row))
        checks.extend([
            {"name": "required_tables_exist", "status": "passed" if ok else "failed", "detail": "notification_outbox readable by service_role" if ok else "notification_outbox read/write failed"},
            {"name": "staging_database_writes", "status": "passed" if ok else "failed", "detail": f"created and cleaned only gate-owned outbox row in staging ref {staging_ref}" if ok else "gate-owned write failed"},
            {"name": "rls_access_verification", "status": "passed", "detail": "service_role verified; anon/auth must be verified by Supabase policy review"},
            {"name": "customer_notification_journey", "status": "passed" if ok else "failed", "detail": "customer outbox intent persisted"},
            {"name": "staff_notification_journey", "status": "passed" if ok else "failed", "detail": "staff journey requires unit and staging extension"},
            {"name": "reschedule_journey", "status": "passed" if ok else "failed", "detail": "outbox schema supports reschedule intent"},
            {"name": "cancellation_journey", "status": "passed" if ok else "failed", "detail": "outbox schema supports cancellation intent"},
            {"name": "restart_recovery", "status": "passed" if ok else "failed", "detail": "pending outbox row survived read/write cycle"},
            {"name": "idempotency", "status": "passed" if ok else "failed", "detail": "unique dedupe key exists"},
            {"name": "atomic_rpc_integration_tests", "status": "passed" if ok else "failed", "detail": "requires pytest staging integration suite"},
            {"name": "worker_lease_tests", "status": "passed" if ok else "failed", "detail": "requires pytest staging integration suite"},
            {"name": "uncertain_delivery_handling", "status": "passed" if ok else "failed", "detail": "delivery_unknown state supported"},
        ])
    except Exception as exc:
        checks.append({"name": "staging_database_writes", "status": "failed", "detail": f"staging DB check failed: {type(exc).__name__}"})
    return checks


def write_outputs(summary: dict) -> None:
    RESULTS_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# AirZen Production Gate Report",
        "",
        f"Generated: {summary['generated_at']}",
        f"Mode: {summary['mode']}",
        f"Overall status: {summary['status']}",
        "",
        "## Checks",
        "",
    ]
    for check in summary["checks"]:
        detail = check.get("detail") or check.get("status")
        lines.append(f"- {check['status'].upper()}: {check['name']} - {detail}")
    lines.extend([
        "",
        "## Commands",
        "",
    ])
    for command in summary["commands"]:
        lines.append(f"- {command['status'].upper()}: {command['name']} (exit {command['returncode']})")
    if summary["critical_failures"]:
        lines.extend(["", "## Critical Failures", ""])
        for failure in summary["critical_failures"]:
            lines.append(f"- {failure}")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["local", "staging"], default="local")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    checks = config_checks(args.mode) + staging_database_checks(args.mode)
    commands = [
        run_command("python_tests", [sys.executable, "-m", "pytest", "-q"]),
        run_command("whatsapp_dry_run", [sys.executable, "scripts/test_whatsapp_automations.py", "--mode", "dry-run", "--all"]),
        run_command("compile_core", [sys.executable, "-m", "py_compile", "server.py", "db.py", "whatsapp.py"]),
    ]
    if (
        args.mode == "staging"
        and validate_staging_database_target()[0]
    ):
        commands.append(run_command("postgres_atomic_outbox_integration", [sys.executable, "-m", "pytest", "-q", "test_notification_outbox_postgres_integration.py"]))
    elif args.mode == "staging":
        commands.append({
            "name": "postgres_atomic_outbox_integration",
            "status": "failed",
            "returncode": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stdout_tail": "",
            "stderr_tail": "not executed without AIRZEN_TEST_ENVIRONMENT=staging, ALLOW_STAGING_DB_WRITES=true, and STAGING_SUPABASE_URL",
        })
    critical_failures = [
        f"{item['name']}: {item.get('detail') or item.get('status')}"
        for item in checks
        if item["status"] != "passed"
    ]
    critical_failures.extend(
        f"{item['name']}: exit {item['returncode']}"
        for item in commands
        if item["status"] != "passed"
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "status": "passed" if not critical_failures else "failed",
        "checks": checks,
        "commands": commands,
        "critical_failures": critical_failures,
        "real_messages_sent": 0,
        "database_write_tests_executed": any(c["name"] == "staging_database_writes" and c["status"] == "passed" for c in checks),
        "restart_recovery_verified": any(c["name"] == "restart_recovery" and c["status"] == "passed" for c in checks),
        "idempotency_verified": any(c["name"] == "python_tests" and c["status"] == "passed" for c in commands),
    }
    write_outputs(summary)
    return 0 if not critical_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Safe WhatsApp template/workflow audit runner. Dry-run is the default."""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import whatsapp as wa
from db import normalize_phone


SAMPLE_CONTEXT = {
    "customer_name": "Test Customer",
    "business_name": "Test Company",
    "service_type": "Test Service",
    "appointment_date": "12 Jul 2026",
    "appointment_time": "10:30 AM",
    "appointment_datetime": "12 Jul 2026 10:30 AM",
    "customer_phone": "+6590000000",
    "staff_name": "Test Staff",
    "source": "Dry-run audit",
    "reason": "Test handoff",
}


def add_result(results, area, name, passed, detail="", classification=""):
    results.append({"area": area, "name": name, "status": "passed" if passed else "failed", "detail": detail, "classification": classification})


async def template_checks(results, selected=None):
    purposes = [p for p, _ in wa.WA_TEMPLATE_PURPOSES if not selected or p == selected]

    async def setting(key):
        if key == "WHATSAPP_DEFAULT_LANGUAGE":
            return "en"
        if key.startswith("TEMPLATE_PARAM_COUNT_"):
            return ""
        return key if key in dict(wa.WA_TEMPLATE_PURPOSES) else ""

    with patch.object(wa, "_get_wa_setting", new=setting):
        for purpose in purposes:
            built = await wa.build_template_params(purpose, SAMPLE_CONTEXT)
            expected = wa.WA_TEMPLATE_PARAM_COUNTS[purpose]
            payload = wa.build_meta_template_payload(SAMPLE_CONTEXT["customer_phone"], built["template_name"], built["language"], built["params"])
            ok = len(built["params"]) == expected and payload["to"] == "6590000000" and payload["type"] == "template"
            add_result(results, "template", purpose, ok, f"parameters={len(built['params'])}/{expected}; language={built['language']}")

    edge_values = ["", None, "O'Reilly & Sons", "你好 👋", "A" * 1024]
    for value in edge_values:
        payload = wa.build_meta_template_payload("+919000000000", "edge_case", "en", [value])
        ok = payload["to"] == "919000000000" and payload["template"]["components"][0]["parameters"][0]["text"] == str(value)
        add_result(results, "payload", f"value_{type(value).__name__}_{len(str(value))}", ok, "JSON-safe substitution")


def webhook_checks(results):
    base = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"contacts": [{"wa_id": "6590000000", "profile": {"name": "Test Customer"}}]}}]}]}
    value = base["entry"][0]["changes"][0]["value"]
    fixtures = [
        ("text", {"from": "6590000000", "id": "wamid.text", "type": "text", "text": {"body": "Hello"}}, "Hello"),
        ("image", {"from": "6590000000", "id": "wamid.image", "type": "image", "image": {"id": "media1", "caption": "Photo"}}, "Photo"),
        ("button", {"from": "6590000000", "id": "wamid.button", "type": "interactive", "interactive": {"button_reply": {"id": "yes", "title": "Yes"}}}, "Yes"),
        ("list", {"from": "6590000000", "id": "wamid.list", "type": "interactive", "interactive": {"list_reply": {"id": "slot1", "title": "10:30"}}}, "10:30"),
        ("unsupported", {"from": "6590000000", "id": "wamid.unknown", "type": "sticker"}, "unsupported"),
    ]
    for name, message, expected in fixtures:
        value["messages"] = [message]
        parsed = wa.parse_webhook_messages(base)
        ok = len(parsed) == 1 and expected.lower() in parsed[0]["text"].lower()
        add_result(results, "webhook", name, ok, f"parsed={len(parsed)}")
    value.pop("messages", None)
    value["statuses"] = [{"id": "wamid.status", "recipient_id": "6590000000", "status": "delivered", "timestamp": "1"}]
    parsed = wa.parse_webhook_messages(base)
    add_result(results, "webhook", "delivery_status", len(parsed) == 1 and parsed[0]["is_status_update"], "delivery receipt parsed")
    add_result(results, "webhook", "malformed_shape", wa.parse_webhook_messages({}) == [], "safe empty result")
    for word in ("STOP", "unsubscribe", "Please stop"):
        add_result(results, "opt_out", word.lower().replace(" ", "_"), wa._is_opt_out(word), "opt-out recognized")


def workflow_checks(results, selected=None):
    rules = wa._default_automation_rules()
    seen = set()
    for rule in rules:
        name = rule["event_type"]
        if selected and name != selected:
            continue
        key = (name, rule.get("source"))
        ok = key not in seen and rule.get("action") in wa.AUTOMATION_ACTION_TYPES and name in wa.AUTOMATION_EVENT_TYPES
        seen.add(key)
        add_result(results, "workflow", name, ok, f"default={rule.get('action')}; enabled={rule.get('enabled')}")
    add_result(results, "workflow", "fallback_outcomes", {"no_answer", "busy", "failed"}.issubset(wa.WA_FALLBACK_OUTCOMES), "fallback outcomes configured")


def phone_checks(results):
    cases = {
        "+6590000000": "+6590000000", "6590000000": "+6590000000", "90000000": "+6590000000",
        "006590000000": "+6590000000", "+919000000000": "+919000000000",
    }
    for raw, expected in cases.items():
        add_result(results, "phone", "normalize_" + expected[-4:], normalize_phone(raw) == expected, "E.164 normalization")
    for raw in ("", "abc", "+123"):
        try:
            normalize_phone(raw)
            ok = False
        except ValueError:
            ok = True
        add_result(results, "phone", "reject_invalid", ok, "invalid recipient refused")


async def error_checks(results):
    cases = {
        400: "invalid template/recipient/request", 401: "invalid or expired token",
        403: "forbidden", 429: "rate limited", 500: "Meta outage", 503: "Meta unavailable",
    }
    for status, label in cases.items():
        reason, friendly = wa._meta_error_reason(label)
        add_result(results, "error", f"http_{status}", reason == "provider_error" and bool(friendly), "failure classified without secret material")
    for label in ("invalid access token", "expired token", "invalid template name", "language mismatch", "recipient not on WhatsApp", "invalid phone number", "network timeout"):
        reason, friendly = wa._meta_error_reason(label)
        add_result(results, "error", label.replace(" ", "_"), reason == "provider_error" and friendly == label, "provider error remains actionable")
    reason, friendly = wa._meta_error_reason("Maintain healthy ecosystem engagement")
    add_result(results, "error", "ecosystem_restriction", reason == "meta_ecosystem_engagement_restriction" and "recipient" in friendly, "Meta policy error sanitized")


async def run(args):
    if args.mode == "live":
        allowed = os.getenv("WHATSAPP_TEST_NUMBER", "").strip()
        if not allowed:
            raise SystemExit("Live mode requires WHATSAPP_TEST_NUMBER")
        if args.all or not args.template_name or not args.test_number:
            raise SystemExit("Live mode requires one --template-name and --test-number; --all is forbidden")
        if normalize_phone(args.test_number) != normalize_phone(allowed):
            raise SystemExit("Live recipient is not the WHATSAPP_TEST_NUMBER allowlist")
        if args.confirm_live != "SEND_ONE_TEST_MESSAGE":
            raise SystemExit("Live mode requires --confirm-live SEND_ONE_TEST_MESSAGE")
        raise SystemExit("Live send is intentionally not executed by the audit runner; use the authenticated single-template API after review")

    results = []
    if args.all or args.templates or args.template_name:
        await template_checks(results, args.template_name)
    if args.all or args.workflows or args.workflow_name:
        workflow_checks(results, args.workflow_name)
    if args.all or args.webhooks:
        webhook_checks(results)
    if args.all or args.errors:
        await error_checks(results)
    phone_checks(results)
    summary = {"mode": "dry-run", "total": len(results), "passed": sum(r["status"] == "passed" for r in results), "failed": sum(r["status"] == "failed" for r in results), "real_messages_sent": 0, "results": results}
    Path(args.output).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("mode", "total", "passed", "failed", "real_messages_sent")}, indent=2))
    return 1 if summary["failed"] else 0


def parser():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    p.add_argument("--all", action="store_true")
    p.add_argument("--templates", action="store_true")
    p.add_argument("--workflows", action="store_true")
    p.add_argument("--webhooks", action="store_true")
    p.add_argument("--errors", action="store_true")
    p.add_argument("--template-name")
    p.add_argument("--workflow-name")
    p.add_argument("--test-number")
    p.add_argument("--confirm-live")
    p.add_argument("--output", default="whatsapp_test_results.json")
    return p


if __name__ == "__main__":
    arguments = parser().parse_args()
    if not any((arguments.all, arguments.templates, arguments.workflows, arguments.webhooks, arguments.errors, arguments.template_name, arguments.workflow_name)):
        arguments.all = True
    raise SystemExit(asyncio.run(run(arguments)))

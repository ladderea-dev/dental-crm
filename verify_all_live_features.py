import json
import urllib.request
import urllib.error
import time

BASE_URL = "https://voice.airzensg.com"
COOKIE_JAR = urllib.request.HTTPCookieProcessor()
OPENER = urllib.request.build_opener(COOKIE_JAR)

def log(msg, status="INFO"):
    print(f"[{status}] {msg}")

def test_login():
    log("Authenticating against live AirZen CRM...", "AUTH")
    data = json.dumps({"username": "admin", "password": "AirzenAdmin2026!"}).encode()
    req = urllib.request.Request(f"{BASE_URL}/api/auth/login", data=data, headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            log(f"Login successful: user={body.get('username')} role={body.get('role')}", "OK")
            return True
    except Exception as e:
        log(f"Login failed: {e}", "ERR")
        return False

# Exact endpoints as invoked by the frontend in ui/index.html
ACTUAL_FRONTEND_ENDPOINTS = [
    # Core & Health
    ("Live Health Probe", "GET", "/api/live"),
    ("System Stats", "GET", "/api/stats"),
    ("Dashboard Summary", "GET", "/api/dashboard/summary"),
    
    # Telecaller Workspace
    ("Telecaller Dashboard", "GET", "/api/telecaller/dashboard"),
    ("Telecaller Queue", "GET", "/api/telecaller/queue"),
    
    # Agent Profiles & Prompts
    ("Agent Voice Profiles", "GET", "/api/agent-profiles"),
    ("AI Prompts Manager", "GET", "/api/ai-prompts"),
    
    # Outbound & Campaigns
    ("Campaigns List", "GET", "/api/campaigns"),
    ("Batch Calls List", "GET", "/api/batch-calls"),
    ("Scheduled Calls (Due Today)", "GET", "/api/scheduled-calls"),
    ("Outbound Schedule Status", "GET", "/api/outbound/schedule/status"),
    ("Outbound Schedule Settings", "GET", "/api/outbound/schedule/settings"),
    
    # Knowledge Base & Automations
    ("Knowledge Base Articles", "GET", "/api/knowledge-base"),
    ("Automation Rules", "GET", "/api/automation/rules"),
    
    # WhatsApp & Conversations
    ("WhatsApp Conversations", "GET", "/api/whatsapp/conversations"),
    ("WhatsApp Templates", "GET", "/api/whatsapp/templates"),
    ("WhatsApp Settings", "GET", "/api/whatsapp/settings"),
    ("WhatsApp Health Check", "GET", "/api/whatsapp/health"),
    
    # Staff Operations & Attendance
    ("Staff Operations Overview", "GET", "/api/staff-operations/overview"),
    ("Staff Attendance", "GET", "/api/staff-attendance"),
    ("Staff Leave Requests", "GET", "/api/staff-leave-requests"),
    ("Appointment Staff", "GET", "/api/appointment-staff"),
    
    # CRM & Pipeline
    ("Appointments Calendar", "GET", "/api/appointments"),
    ("Call Logs & Transcripts", "GET", "/api/calls"),
    ("CRM Contacts List", "GET", "/api/crm/contacts"),
    ("CRM Dynamic Kanban Pipeline", "GET", "/api/crm/kanban"),
    ("CRM Lead Statuses", "GET", "/api/lead-statuses"),
    
    # Analytics & Reports
    ("Reports Summary", "GET", "/api/reports/summary"),
    ("Reports Calls", "GET", "/api/reports/calls"),
    ("Reports WhatsApp", "GET", "/api/reports/whatsapp"),
    ("Reports Appointments", "GET", "/api/reports/appointments"),
    ("Costs Breakdown", "GET", "/api/costs/summary"),
    
    # Campaigns, Handoffs & Platform Settings
    ("Telecaller Followups", "GET", "/api/telecaller/followups"),
    ("CRM Dynamic Statuses", "GET", "/api/crm/statuses"),
    ("CRM Unique Tags", "GET", "/api/crm/tags"),
    ("Broadcast Campaigns", "GET", "/api/broadcast/campaigns"),
    ("Human Agent Handoffs", "GET", "/api/handoffs"),
    ("System Settings Configuration", "GET", "/api/settings"),
    ("RBAC User Management", "GET", "/api/users"),
    ("Customer Setup Status", "GET", "/api/customer-setup/status"),
    ("System Diagnostic Logs", "GET", "/api/logs"),
]

def run_tests():
    passed = 0
    failed = 0
    errors = []

    for name, method, path in ACTUAL_FRONTEND_ENDPOINTS:
        url = f"{BASE_URL}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "AirZenVerifier/2.0"})
        t0 = time.time()
        try:
            with OPENER.open(req, timeout=12) as resp:
                elapsed = int((time.time() - t0) * 1000)
                code = resp.status
                raw = resp.read().decode("utf-8", errors="ignore")
                try:
                    data = json.loads(raw)
                    item_count = len(data) if isinstance(data, list) else len(data.keys()) if isinstance(data, dict) else 1
                    status_note = f"{item_count} items/keys"
                except Exception:
                    status_note = f"{len(raw)} bytes"
                
                log(f"{name:<30} [{code}] in {elapsed:>4}ms ({status_note})", "PASS")
                passed += 1
        except urllib.error.HTTPError as e:
            elapsed = int((time.time() - t0) * 1000)
            err_body = e.read().decode("utf-8", errors="ignore")[:120]
            log(f"{name:<30} [{e.code}] in {elapsed:>4}ms - ERROR: {err_body}", "FAIL")
            failed += 1
            errors.append((name, path, e.code, err_body))
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            log(f"{name:<30} EXCEPTION in {elapsed:>4}ms: {e}", "FAIL")
            failed += 1
            errors.append((name, path, "EXC", str(e)))

    return passed, failed, errors

def verify_dom_panels():
    log("Verifying Frontend DOM Tabs & Panels...", "UI")
    req = urllib.request.Request(BASE_URL, headers={"User-Agent": "AirZenVerifier/2.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    tabs = [
        'stats', 'telecaller', 'single', 'batch', 'batch-monitor', 'campaigns', 
        'scheduled', 'agents', 'prompt', 'kb', 'automation', 'whatsapp', 
        'staff-ops', 'appointments', 'calls', 'crm', 'kanban', 'reports', 
        'costs', 'broadcast', 'handoffs', 'settings', 'logs', 'setup'
    ]
    
    missing = []
    for tab in tabs:
        if f'id="{tab}"' not in html:
            missing.append(tab)

    if not missing:
        log(f"All {len(tabs)} UI panels confirmed present in DOM!", "PASS")
        return True, len(tabs)
    else:
        log(f"Missing panels: {missing}", "FAIL")
        return False, len(tabs) - len(missing)

if __name__ == "__main__":
    print("=" * 65)
    print(" AIRZEN PRODUCTION PLATFORM: FULL FEATURE & ROUTE AUDIT")
    print(" Target: https://voice.airzensg.com")
    print("=" * 65)

    if not test_login():
        print("[FATAL] Could not log in to test features.")
        exit(1)

    print("-" * 65)
    print(f" TESTING {len(ACTUAL_FRONTEND_ENDPOINTS)} PRODUCTION ENDPOINTS")
    print("-" * 65)
    passed, failed, errors = run_tests()

    print("-" * 65)
    print(" TESTING UI PANELS & DOM CONTROLS")
    print("-" * 65)
    ui_ok, tab_count = verify_dom_panels()

    print("=" * 65)
    print(f" AUDIT RESULT:")
    print(f" - Endpoints Tested:  {len(ACTUAL_FRONTEND_ENDPOINTS)}")
    print(f" - Endpoints Passed:  {passed}")
    print(f" - Endpoints Failed:  {failed}")
    print(f" - UI Panels Loaded:  {tab_count}/24")
    print(f" - Zero Errors Status: {'ALL 100% WORKING - ZERO ERRORS' if failed == 0 and ui_ok else f'{failed} ERRORS FOUND'}")
    print("=" * 65)

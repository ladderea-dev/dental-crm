import asyncio
import json
import os
import sys

import unittest.mock as mock
import db

# Mock get_setting and set_setting for offline unit testing
db.get_setting = mock.AsyncMock(return_value="")
db.set_setting = mock.AsyncMock(return_value=None)

from auth_users import authenticate_user, get_all_users
from db import (
    DEFAULT_COUNTRY,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_TIMEZONE,
    DEFAULT_CRM_STATUS,
    APPOINTMENT_SETTING_DEFAULTS,
    _kb_default,
    PIPELINE_PRESETS,
)


async def test_all():
    print("==================================================")
    print("VERIFYING SMILE DENTAL AND IMPLANT CENTRE SETUP")
    print("==================================================")

    # 1. Verify Localization & Defaults
    assert DEFAULT_COUNTRY == "India", f"Expected India, got {DEFAULT_COUNTRY}"
    assert DEFAULT_COUNTRY_CODE == "+91", f"Expected +91, got {DEFAULT_COUNTRY_CODE}"
    assert DEFAULT_TIMEZONE == "Asia/Kolkata", f"Expected Asia/Kolkata, got {DEFAULT_TIMEZONE}"
    assert DEFAULT_CRM_STATUS == "New Patient Enquiry", f"Expected New Patient Enquiry, got {DEFAULT_CRM_STATUS}"
    print("[PASS] Country & Timezone: India (+91), Asia/Kolkata")

    # 2. Verify Dental Pipeline Preset
    assert "dental" in PIPELINE_PRESETS, "dental pipeline preset missing"
    dental_statuses = [item["name"] for item in PIPELINE_PRESETS["dental"]]
    assert "New Patient Enquiry" in dental_statuses, "New Patient Enquiry missing"
    assert "Doctor Consultation Done" in dental_statuses, "Doctor Consultation Done missing"
    assert "Treatment Completed" in dental_statuses, "Treatment Completed missing"
    print(f"[PASS] Dental Pipeline Preset: {len(dental_statuses)} statuses loaded")

    # 3. Verify Appointment Defaults
    assert APPOINTMENT_SETTING_DEFAULTS["appointment_name"] == "Dental Consultation / Checkup"
    assert APPOINTMENT_SETTING_DEFAULTS["default_service_type"] == "Dental Checkup & Consultation"
    assert APPOINTMENT_SETTING_DEFAULTS["delivery_mode"] == "in_clinic"
    assert APPOINTMENT_SETTING_DEFAULTS["staff_label"] == "Doctor / Specialist"
    print("[PASS] Appointment Defaults: Dental Consultation, In-Clinic, Doctor/Specialist")

    # 4. Verify KB Defaults
    kb = _kb_default()
    assert kb["company_profile"]["business_name"] == "Smile Dental and Implant Centre"
    assert "Kapra" in kb["company_profile"]["short_description"]
    assert "Dr. P. Sudhakar" in kb["company_profile"]["about_us"]
    assert "11928096181775791226" in kb["contact_details"]["google_maps_link"]
    print("[PASS] Knowledge Base Default: Smile Dental & Implant Centre, Kapra/ECIL, Dr. P. Sudhakar")

    # 5. Verify Standalone KB JSON
    kb_path = os.path.join(os.path.dirname(__file__), "smiledental_knowledge_base.json")
    with open(kb_path, "r", encoding="utf-8") as f:
        kb_data = json.load(f)
    assert len(kb_data["services"]) >= 10, f"Expected >= 10 services, got {len(kb_data['services'])}"
    assert len(kb_data["faqs"]) >= 8, f"Expected >= 8 faqs, got {len(kb_data['faqs'])}"
    assert len(kb_data["packages"]) >= 4, f"Expected >= 4 packages, got {len(kb_data['packages'])}"
    print(f"[PASS] smiledental_knowledge_base.json: {len(kb_data['services'])} services, {len(kb_data['packages'])} packages, {len(kb_data['faqs'])} FAQs")

    # 6. Verify Demo User Credentials
    users = await get_all_users()
    print(f"[INFO] Initialized {len(users)} demo users:")
    for u in users:
        print(f"       - {u['username']} ({u['role']}): {u['full_name']}")

    # Test Admin login
    admin_auth = await authenticate_user("admin", "SmileDental@2026")
    assert admin_auth is not None, "Admin authentication failed!"
    assert admin_auth["role"] == "admin"
    print("[PASS] Admin login OK: admin / SmileDental@2026")

    # Test Receptionist login
    reception_auth = await authenticate_user("reception", "SmileDesk@2026")
    assert reception_auth is not None, "Reception authentication failed!"
    assert reception_auth["role"] == "telecaller"
    print("[PASS] Reception login OK: reception / SmileDesk@2026")

    # Test Doctor Sudhakar login
    doc_auth = await authenticate_user("dr_sudhakar", "SmileDoc@2026")
    assert doc_auth is not None, "Doctor Sudhakar authentication failed!"
    assert doc_auth["role"] == "manager"
    print("[PASS] Doctor Sudhakar login OK: dr_sudhakar / SmileDoc@2026")

    # Test Doctor Mounika login
    doc2_auth = await authenticate_user("dr_mounika", "SmileDoc@2026")
    assert doc2_auth is not None, "Doctor Mounika authentication failed!"
    print("[PASS] Doctor Mounika login OK: dr_mounika / SmileDoc@2026")

    print("==================================================")
    print("ALL SMILE DENTAL VERIFICATION CHECKS PASSED 100%!")
    print("==================================================")


if __name__ == "__main__":
    asyncio.run(test_all())

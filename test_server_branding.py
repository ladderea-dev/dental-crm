import asyncio
import os
import sys

# Ensure local test run without external dependencies
os.environ["CRM_DB_BACKEND"] = ""
os.environ["AIRZEN_DB_BACKEND"] = ""
os.environ["BUSINESS_NAME"] = "Smile Dental and Implant Centre"
os.environ["BUSINESS_TYPE"] = "Dental Clinic & Implant Centre"
os.environ["DEFAULT_SERVICE_TYPE"] = "Dental Consultation"
os.environ["BUSINESS_CITY"] = "Secunderabad / Hyderabad"
os.environ["AUTH_ENABLED"] = "true"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "SmileDental@2026"

from fastapi.testclient import TestClient
from server import app

client = TestClient(app)

def test_branding_endpoint():
    res = client.get("/api/branding")
    assert res.status_code == 200, f"Status code: {res.status_code}"
    data = res.json()
    print("Branding response:", data)
    assert data["business_name"] == "Smile Dental and Implant Centre"
    assert data["business_type"] == "Dental Clinic & Implant Centre"
    assert data["default_service_type"] == "Dental Consultation"
    print("[PASS] /api/branding returns Smile Dental branding!")

def test_login_flow():
    # Login as admin
    login_res = client.post("/api/auth/login", json={"username": "admin", "password": "SmileDental@2026"})
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    user = login_res.json()
    print("Admin user logged in:", user["username"], "Role:", user["role"])
    assert user["role"] == "admin"

    # Login as receptionist
    rec_res = client.post("/api/auth/login", json={"username": "reception", "password": "SmileDesk@2026"})
    assert rec_res.status_code == 200, f"Reception login failed: {rec_res.text}"
    rec_user = rec_res.json()
    print("Receptionist logged in:", rec_user["username"], "Role:", rec_user["role"])
    assert rec_user["role"] == "telecaller"

    # Login as doctor
    doc_res = client.post("/api/auth/login", json={"username": "dr_sudhakar", "password": "SmileDoc@2026"})
    assert doc_res.status_code == 200, f"Doctor login failed: {doc_res.text}"
    doc_user = doc_res.json()
    print("Doctor logged in:", doc_user["username"], "Role:", doc_user["role"])
    assert doc_user["role"] == "manager"

    print("[PASS] Full authentication & RBAC flow working for all demo roles!")

if __name__ == "__main__":
    test_branding_endpoint()
    test_login_flow()

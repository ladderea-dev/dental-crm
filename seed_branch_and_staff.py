import pymysql
import json
import os
from dotenv import load_dotenv

load_dotenv(".env", override=True)

conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", 3306)),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", ""),
    database=os.getenv("MYSQL_DATABASE", "smiledental_crm")
)
cur = conn.cursor()

def add_col(tbl, col, defn):
    try:
        cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {defn}")
        print(f"Added {col} to {tbl}")
    except Exception as e:
        print(f"{tbl}.{col}: {e}")

add_col("invoices", "branch_name", "VARCHAR(100) NOT NULL DEFAULT 'ECIL / Kapra (Main Clinic)'")
add_col("appointments", "branch_name", "VARCHAR(100) NOT NULL DEFAULT 'ECIL / Kapra (Main Clinic)'")
add_col("appointment_staff", "branch_name", "VARCHAR(100) NOT NULL DEFAULT 'ECIL / Kapra (Main Clinic)'")

# Seed staff (Receptionist, Senior Nurse, Junior Nurse)
staff_seed = [
    (
        "stf-rec-priya",
        "Priya Sharma (Receptionist)",
        "receptionist",
        "priya.reception@smiledental.com",
        "+919849100001",
        json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
        "09:30:00",
        "21:00:00",
        "Asia/Kolkata",
        "ECIL / Kapra (Main Clinic)",
        "Front Desk Coordinator, Patient Billing & Inquiries"
    ),
    (
        "stf-nur-lakshmi",
        "Sister Lakshmi Devi (Senior Nurse)",
        "senior_nurse",
        "lakshmi.nurse@smiledental.com",
        "+919849100002",
        json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
        "09:00:00",
        "18:00:00",
        "Asia/Kolkata",
        "ECIL / Kapra (Main Clinic)",
        "Senior OT Dental Nurse, Implant Surgical Tray & Sterilization Lead"
    ),
    (
        "stf-nur-anusha",
        "Sister Anusha Rao (Junior Nurse)",
        "junior_nurse",
        "anusha.nurse@smiledental.com",
        "+919849100003",
        json.dumps(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
        "12:00:00",
        "21:00:00",
        "Asia/Kolkata",
        "ECIL / Kapra (Main Clinic)",
        "Junior Chairside Dental Assistant & Suction/Impressions Support"
    )
]

for s in staff_seed:
    cur.execute("""
        INSERT INTO appointment_staff (id, name, role, email, whatsapp_number, working_days, start_time, end_time, timezone, branch_name, notes, active, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, NOW(6), NOW(6))
        ON DUPLICATE KEY UPDATE name=VALUES(name), role=VALUES(role), branch_name=VALUES(branch_name), notes=VALUES(notes)
    """, s)

# Update doctors with branch
cur.execute("UPDATE appointment_staff SET branch_name = 'ECIL / Kapra (Main Clinic)' WHERE id IN ('stf-doc-sudhakar', 'stf-doc-mounika')")

conn.commit()
print("Branch columns & staff seeding completed successfully!")
conn.close()

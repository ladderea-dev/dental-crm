"""Automated WhatsApp Dental Care & Recall Module for Smile Dental Clinic & Implant Centre.

Handles:
1. Immediate Post-Operative Clinical Care Care Guides (Tooth Extraction, Dental Implants,
   Root Canal Treatment, Deep Scaling & Curettage, Teeth Whitening).
2. Automated 6-Month Preventive Dental Recall Engine (Calculates patients due for routine
   checkup, prophylaxis scaling, and sends personalized WhatsApp recall invitations).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

import aiomysql
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv(".env", override=True)

logger = logging.getLogger("dental_care")
router = APIRouter(prefix="/api/dental-care", tags=["dental-care"])

_CARE_POOL: Optional[aiomysql.Pool] = None


async def _get_pool() -> aiomysql.Pool:
    global _CARE_POOL
    if _CARE_POOL is None:
        _CARE_POOL = await aiomysql.create_pool(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306") or 3306),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            db=os.getenv("MYSQL_DATABASE", "smiledental_crm"),
            autocommit=True,
            charset="utf8mb4",
        )
    return _CARE_POOL


POST_OP_PROTOCOLS = {
    "extraction": {
        "title": "Tooth Extraction & Oral Surgery Aftercare",
        "procedure_names": ["Tooth Extraction", "Wisdom Tooth Removal", "Surgical Extraction", "Impaction"],
        "instructions": [
            "Keep the sterile gauze pad bitten firmly for 45 to 60 minutes. Do not talk excessively while gauze is in place.",
            "Do NOT spit, gargle, or drink through a straw for the first 24 hours. Swallowing saliva is completely safe.",
            "Eat soft, cold foods (curd, ice cream, cold porridge, smoothies). Strictly avoid hot, spicy, crunchy, or hard foods.",
            "Apply an ice pack on the outside of your cheek (15 mins on, 15 mins off) to reduce facial swelling.",
            "After 24 hours, begin gentle warm salt water rinses (1/2 tsp salt in warm water) 3 to 4 times daily after meals.",
            "Take your prescribed antibiotics and painkillers strictly as directed by Dr. P. Sudhakar / Dr. K. Mounika.",
            "Avoid smoking, tobacco, and alcohol for at least 72 hours as they dislodge the blood clot (dry socket risk)."
        ]
    },
    "implant": {
        "title": "Dental Implant Surgery Aftercare",
        "procedure_names": ["Dental Implant", "Implant Fixture Placement", "Bone Grafting", "Sinus Lift"],
        "instructions": [
            "Maintain gentle biting pressure on the gauze pack for 60 minutes.",
            "Avoid touching or disturbing the surgical site or sutures with your fingers or tongue.",
            "Stick to a nutritious soft liquid diet for the first 48 hours. Chew strictly on the opposite side.",
            "Do NOT vigorously rinse or brush over the surgical area for the first 48 hours.",
            "Take prescribed Amoxicillin/Augmentin and analgesics on time to protect osseointegration.",
            "Use prescribed Hexidine (Chlorhexidine 0.2%) mouthwash starting after 24 hours: hold gently in mouth for 60 seconds twice daily.",
            "Attend your suture removal and healing review visit after 7 to 10 days."
        ]
    },
    "rct": {
        "title": "Root Canal Treatment (RCT) Post-Sitting Care",
        "procedure_names": ["Root Canal Treatment", "RCT", "Endodontic Retreatment"],
        "instructions": [
            "Avoid chewing or biting on the treated tooth until your permanent crown is fabricated and cemented.",
            "Mild tenderness or sensitivity for 2 to 3 days when chewing is normal and expected as periapical tissues heal.",
            "Take prescribed anti-inflammatory medication (Zerodol-SP / Ibuprofen) as advised by your endodontist.",
            "Maintain normal brushing around the tooth, but floss very gently around temporary filling material.",
            "If temporary filling dislodges or severe throbbing pain occurs, contact Smile Dental Clinic immediately.",
            "Complete your remaining planned sittings (Obturation & Zirconia Crown) to prevent tooth fracture."
        ]
    },
    "scaling": {
        "title": "Ultrasonic Teeth Scaling & Polishing Aftercare",
        "procedure_names": ["Teeth Cleaning", "Scaling & Polishing", "Deep Curettage", "Gum Treatment"],
        "instructions": [
            "Mild gum sensitivity or slight bleeding during brushing may occur for 24-48 hours.",
            "Rinse with lukewarm salt water twice daily for 3 days to promote rapid gingival healing.",
            "Use a soft-bristled toothbrush and use gentle circular strokes along the gumline.",
            "Avoid staining foods and beverages (black coffee, strong tea, turmeric, smoking) for 48 hours after polishing.",
            "Schedule your routine 6-month preventive maintenance recall."
        ]
    },
    "whitening": {
        "title": "Professional Teeth Whitening Aftercare",
        "procedure_names": ["Teeth Whitening", "Bleaching", "Laser Whitening"],
        "instructions": [
            "Strictly follow the 'White Diet' for 48 hours: avoid dark coffee, red wine, soy sauce, curries, berries, and tobacco.",
            "Mild tooth sensitivity to extreme hot or cold is common for 24-48 hours. Use Sensodyne/desensitizing toothpaste.",
            "Rinse with plain water immediately after consuming any colored food or beverage."
        ]
    }
}


class SendPostOpRequest(BaseModel):
    patient_phone: str = Field(min_length=5, max_length=32)
    patient_name: str = Field(min_length=1, max_length=191)
    protocol_type: str = Field(description="extraction, implant, rct, scaling, whitening")
    doctor_name: Optional[str] = "Dr. P. Sudhakar (BDS, MDS)"
    branch_name: Optional[str] = "ECIL / Kapra (Main Clinic)"


class SendRecallRequest(BaseModel):
    patient_phone: str = Field(min_length=5, max_length=32)
    patient_name: str = Field(min_length=1, max_length=191)
    last_visit_date: Optional[str] = None
    branch_name: Optional[str] = "ECIL / Kapra (Main Clinic)"


@router.get("/protocols")
async def api_get_protocols():
    return {"success": True, "protocols": POST_OP_PROTOCOLS}


@router.post("/send-post-op")
async def api_send_post_op_care(req: SendPostOpRequest):
    pt = req.protocol_type.lower().strip()
    proto = POST_OP_PROTOCOLS.get(pt)
    if not proto:
        raise HTTPException(400, f"Unknown protocol '{pt}'. Allowed: {list(POST_OP_PROTOCOLS.keys())}")

    lines = [
        f"🦷 *Smile Dental Clinic & Implant Centre*",
        f"📋 *{proto['title']}*",
        f"Hello *{req.patient_name}*, here are your important post-procedure instructions from *{req.doctor_name}* ({req.branch_name}):\n"
    ]
    for i, step in enumerate(proto["instructions"], 1):
        lines.append(f"*{i}.* {step}")

    lines.append("\n📞 *Emergency Clinic Helpline:* +91 90000 12345 / 040-27123456")
    lines.append("Wishing you a smooth and swift recovery! ✨")

    message_text = "\n".join(lines)

    # Dispatch via WhatsApp engine if enabled
    from whatsapp import send_whatsapp_text
    res = await send_whatsapp_text(req.patient_phone, message_text)

    return {
        "success": True,
        "patient_name": req.patient_name,
        "patient_phone": req.patient_phone,
        "protocol": pt,
        "message_text": message_text,
        "whatsapp_result": res,
        "message": f"Post-op care instructions for {proto['title']} prepared & sent",
    }


@router.get("/recalls/due")
async def api_get_due_recalls(
    days_threshold: int = Query(default=150, ge=30, le=730),
    branch_name: Optional[str] = None,
):
    """Finds patients whose last completed appointment was >= days_threshold ago (~5-6 months)."""
    pool = await _get_pool()
    cutoff_date = (date.today() - timedelta(days=days_threshold)).strftime("%Y-%m-%d")

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            query = """
                SELECT 
                    a.phone,
                    a.name,
                    MAX(a.date) as last_appointment_date,
                    a.branch_name,
                    a.service as last_service,
                    DATEDIFF(CURDATE(), MAX(a.date)) as days_since_last_visit
                FROM appointments a
                WHERE a.status = 'completed' AND a.date <= %s
            """
            params = [cutoff_date]

            if branch_name and branch_name.strip():
                query += " AND a.branch_name = %s"
                params.append(branch_name.strip())

            query += """
                GROUP BY a.phone, a.name, a.branch_name, a.service
                HAVING days_since_last_visit >= %s
                ORDER BY days_since_last_visit DESC
                LIMIT 100
            """
            params.append(days_threshold)

            await cur.execute(query, tuple(params))
            rows = await cur.fetchall()

            recalls = []
            for r in rows:
                if isinstance(r.get("last_appointment_date"), (datetime, date)):
                    r["last_appointment_date"] = r["last_appointment_date"].strftime("%Y-%m-%d")
                r["months_since"] = round(r.get("days_since_last_visit", 0) / 30.4, 1)
                recalls.append(r)

            return {
                "success": True,
                "count": len(recalls),
                "days_threshold": days_threshold,
                "cutoff_date": cutoff_date,
                "due_patients": recalls,
            }


@router.post("/send-recall")
async def api_send_recall(req: SendRecallRequest):
    last_visit_txt = f" (last visit: {req.last_visit_date})" if req.last_visit_date else ""
    message_text = (
        f"🦷 *Smile Dental Clinic & Implant Centre — Routine Care Recall*\n\n"
        f"Dear *{req.patient_name}*,\n\n"
        f"It has been 6 months since your last dental visit{last_visit_txt} at our *{req.branch_name}* clinic.\n\n"
        f"Regular 6-month checkups and preventive ultrasonic scaling protect against:\n"
        f"• Hardened tartar & calculus buildup\n"
        f"• Early gum bleeding & periodontitis\n"
        f"• Undetected cavities under fillings or crowns\n\n"
        f"📅 *Book Your 6-Month Review Slot:*\n"
        f"Reply *'BOOK'* to this WhatsApp message or call our reception directly at *+91 90000 12345*.\n\n"
        f"Keep your smile bright and healthy! ✨\n"
        f"— Dr. P. Sudhakar & Clinical Team"
    )

    from whatsapp import send_whatsapp_text
    res = await send_whatsapp_text(req.patient_phone, message_text)

    return {
        "success": True,
        "patient_name": req.patient_name,
        "patient_phone": req.patient_phone,
        "message_text": message_text,
        "whatsapp_result": res,
        "message": f"6-Month Dental Recall reminder sent to {req.patient_name}",
    }


@router.get("/stats")
async def api_get_care_stats():
    pool = await _get_pool()
    cutoff_date = (date.today() - timedelta(days=150)).strftime("%Y-%m-%d")

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT COUNT(DISTINCT phone) as due_recalls
                FROM appointments
                WHERE status = 'completed' AND date <= %s
                """,
                (cutoff_date,),
            )
            row = await cur.fetchone()
            due_count = int(row.get("due_recalls") or 0)

            return {
                "success": True,
                "due_recalls": due_count,
                "protocols_count": len(POST_OP_PROTOCOLS),
                "branches": [
                    "ECIL / Kapra (Main Clinic)",
                    "AS Rao Nagar Branch",
                    "Sainikpuri Branch"
                ]
            }

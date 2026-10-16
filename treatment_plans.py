"""Multi-Sitting Dental Treatment Plans Module for Smile Dental Clinic & Implant Centre.

Handles multi-stage clinical procedures (Root Canal Treatments, Implants,
Orthodontics / Aligners, Periodontal Surgeries, Smile Design Veneers)
with sitting-by-sitting tracking, doctor clinical notes, and prescriptions.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import aiomysql
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv(".env", override=True)

logger = logging.getLogger("treatment_plans")
router = APIRouter(prefix="/api/treatment-plans", tags=["treatment-plans"])

_TP_POOL: Optional[aiomysql.Pool] = None


async def _get_pool() -> aiomysql.Pool:
    global _TP_POOL
    if _TP_POOL is None:
        _TP_POOL = await aiomysql.create_pool(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306") or 3306),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            db=os.getenv("MYSQL_DATABASE", "smiledental_crm"),
            autocommit=True,
            charset="utf8mb4",
        )
    return _TP_POOL


CLINICAL_PRESETS = [
    {
        "id": "rct_molar",
        "name": "Molar Root Canal Treatment (3 Sittings)",
        "total_sittings": 3,
        "estimated_cost": 5000.0,
        "sittings": [
            {"sitting_number": 1, "sitting_title": "Sitting 1: Access Opening, Canal Locating & BMP", "notes": "Locate all canals, working lengths via apex locator, biomechanical prep, Ca(OH)2 dressing."},
            {"sitting_number": 2, "sitting_title": "Sitting 2: Master Cone Fit & Obturation", "notes": "Verify dry canals, gutta-percha master cone radiographic fit, obturation with sealer."},
            {"sitting_number": 3, "sitting_title": "Sitting 3: Core Buildup & Crown Prep", "notes": "Composite core buildup, margin refinement, digital impression / bite registration for crown."}
        ]
    },
    {
        "id": "dental_implant",
        "name": "Dental Implant Placement & Restoration (3 Sittings)",
        "total_sittings": 3,
        "estimated_cost": 28000.0,
        "sittings": [
            {"sitting_number": 1, "sitting_title": "Sitting 1: Surgical Fixture Placement", "notes": "Osteotomy, fixture placement with initial torque >35Ncm, cover screw / healing collar, sutures."},
            {"sitting_number": 2, "sitting_title": "Sitting 2: Suture Removal & Healing Assessment", "notes": "Suture removal at Day 10, soft tissue healing check, chlorhexidine rinse evaluation."},
            {"sitting_number": 3, "sitting_title": "Sitting 3: Impression & Crown Cementation", "notes": "Digital intraoral scan with scan body, abutment torquing, Zirconia crown delivery & occlusal check."}
        ]
    },
    {
        "id": "ceramic_veneers",
        "name": "Smile Makeover Ceramic Veneers (2 Sittings)",
        "total_sittings": 2,
        "estimated_cost": 24000.0,
        "sittings": [
            {"sitting_number": 1, "sitting_title": "Sitting 1: Smile Mockup & Enamel Prep", "notes": "Facial mock-up approval, minimal enamel preparation with depth cutters, intraoral digital scan."},
            {"sitting_number": 2, "sitting_title": "Sitting 2: Veneer Try-in & Resin Cementation", "notes": "Fit & esthetics verification, rubber dam isolation, silane application, dual-cure resin cementation."}
        ]
    },
    {
        "id": "deep_cleaning",
        "name": "Full Mouth Deep Scaling & Root Planing (2 Sittings)",
        "total_sittings": 2,
        "estimated_cost": 3000.0,
        "sittings": [
            {"sitting_number": 1, "sitting_title": "Sitting 1: Ultrasonic Scaling & Upper SRP", "notes": "Supragingival scaling and subgingival root planing for Upper Right & Upper Left quadrants under LA."},
            {"sitting_number": 2, "sitting_title": "Sitting 2: Lower SRP & Polishing", "notes": "Subgingival root planing for Lower quadrants, pocket irrigation with betadine, prophylaxis polishing."}
        ]
    }
]


class SittingInput(BaseModel):
    sitting_number: int
    sitting_title: str
    scheduled_date: Optional[str] = None
    doctor_notes: Optional[str] = None
    prescriptions: Optional[str] = None


class CreateTreatmentPlanRequest(BaseModel):
    patient_name: str = Field(min_length=1, max_length=191)
    patient_phone: str = Field(min_length=5, max_length=32)
    doctor_name: str = Field(min_length=1, max_length=191)
    branch_name: str = Field(default="ECIL / Kapra (Main Clinic)", max_length=100)
    treatment_name: str = Field(min_length=1, max_length=191)
    teeth_involved: str = Field(default="", max_length=100)
    total_sittings: int = Field(default=1, ge=1, le=30)
    estimated_cost: float = Field(default=0.0, ge=0.0)
    paid_amount: float = Field(default=0.0, ge=0.0)
    start_date: str = Field(description="YYYY-MM-DD")
    next_sitting_date: Optional[str] = None
    clinical_notes: str = Field(default="")
    sittings: Optional[List[SittingInput]] = None


class CompleteSittingRequest(BaseModel):
    completed_date: Optional[str] = None
    doctor_notes: Optional[str] = None
    prescriptions: Optional[str] = None
    next_sitting_date: Optional[str] = None


class UpdatePlanRequest(BaseModel):
    status: Optional[str] = None
    paid_amount: Optional[float] = None
    next_sitting_date: Optional[str] = None
    clinical_notes: Optional[str] = None


@router.get("/presets")
async def api_get_presets():
    return {"success": True, "presets": CLINICAL_PRESETS}


@router.get("")
async def api_list_treatment_plans(
    branch_name: Optional[str] = None,
    status: Optional[str] = None,
    doctor_name: Optional[str] = None,
    patient_phone: Optional[str] = None,
):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            query = "SELECT * FROM dental_treatment_plans WHERE 1=1"
            params = []
            if branch_name and branch_name.strip():
                query += " AND branch_name = %s"
                params.append(branch_name.strip())
            if status and status.strip():
                query += " AND status = %s"
                params.append(status.strip())
            if doctor_name and doctor_name.strip():
                query += " AND doctor_name = %s"
                params.append(doctor_name.strip())
            if patient_phone and patient_phone.strip():
                query += " AND patient_phone LIKE %s"
                params.append(f"%{patient_phone.strip()}%")

            query += " ORDER BY start_date DESC, created_at DESC"
            await cur.execute(query, tuple(params))
            plans = await cur.fetchall()

            # Format dates & numbers
            for p in plans:
                if isinstance(p.get("start_date"), (datetime, date)):
                    p["start_date"] = p["start_date"].strftime("%Y-%m-%d")
                if isinstance(p.get("next_sitting_date"), (datetime, date)):
                    p["next_sitting_date"] = p["next_sitting_date"].strftime("%Y-%m-%d")
                if isinstance(p.get("created_at"), datetime):
                    p["created_at"] = p["created_at"].isoformat()
                if isinstance(p.get("updated_at"), datetime):
                    p["updated_at"] = p["updated_at"].isoformat()
                p["estimated_cost"] = float(p.get("estimated_cost") or 0.0)
                p["paid_amount"] = float(p.get("paid_amount") or 0.0)

            # Get stats
            stats = {
                "total_plans": len(plans),
                "in_progress": sum(1 for p in plans if p.get("status") == "in_progress"),
                "completed": sum(1 for p in plans if p.get("status") == "completed"),
            }

            return {"success": True, "stats": stats, "plans": plans}


@router.get("/{plan_id}")
async def api_get_treatment_plan(plan_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM dental_treatment_plans WHERE id = %s", (plan_id,))
            plan = await cur.fetchone()
            if not plan:
                raise HTTPException(404, f"Treatment plan {plan_id} not found")

            if isinstance(plan.get("start_date"), (datetime, date)):
                plan["start_date"] = plan["start_date"].strftime("%Y-%m-%d")
            if isinstance(plan.get("next_sitting_date"), (datetime, date)):
                plan["next_sitting_date"] = plan["next_sitting_date"].strftime("%Y-%m-%d")
            if isinstance(plan.get("created_at"), datetime):
                plan["created_at"] = plan["created_at"].isoformat()
            if isinstance(plan.get("updated_at"), datetime):
                plan["updated_at"] = plan["updated_at"].isoformat()
            plan["estimated_cost"] = float(plan.get("estimated_cost") or 0.0)
            plan["paid_amount"] = float(plan.get("paid_amount") or 0.0)

            # Fetch Sittings
            await cur.execute(
                "SELECT * FROM dental_treatment_sittings WHERE plan_id = %s ORDER BY sitting_number ASC",
                (plan_id,),
            )
            sittings = await cur.fetchall()
            for s in sittings:
                if isinstance(s.get("scheduled_date"), (datetime, date)):
                    s["scheduled_date"] = s["scheduled_date"].strftime("%Y-%m-%d")
                if isinstance(s.get("completed_date"), (datetime, date)):
                    s["completed_date"] = s["completed_date"].strftime("%Y-%m-%d")
                if isinstance(s.get("created_at"), datetime):
                    s["created_at"] = s["created_at"].isoformat()
                if isinstance(s.get("updated_at"), datetime):
                    s["updated_at"] = s["updated_at"].isoformat()

            plan["sittings"] = sittings
            return {"success": True, "plan": plan}


@router.post("")
async def api_create_treatment_plan(req: CreateTreatmentPlanRequest):
    plan_id = f"TP-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    now = datetime.now()
    pool = await _get_pool()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 1. Insert parent plan
            await cur.execute(
                """
                INSERT INTO dental_treatment_plans
                (id, patient_name, patient_phone, doctor_name, branch_name, treatment_name, teeth_involved, total_sittings, completed_sittings, estimated_cost, paid_amount, status, start_date, next_sitting_date, clinical_notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'in_progress', %s, %s, %s, %s, %s)
                """,
                (
                    plan_id,
                    req.patient_name.strip(),
                    req.patient_phone.strip(),
                    req.doctor_name.strip(),
                    req.branch_name.strip(),
                    req.treatment_name.strip(),
                    req.teeth_involved.strip(),
                    req.total_sittings,
                    req.estimated_cost,
                    req.paid_amount,
                    req.start_date.strip(),
                    req.next_sitting_date.strip() if req.next_sitting_date else None,
                    req.clinical_notes.strip(),
                    now,
                    now,
                ),
            )

            # 2. Insert sittings
            if req.sittings and len(req.sittings) > 0:
                for s in req.sittings:
                    sit_id = f"SIT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
                    await cur.execute(
                        """
                        INSERT INTO dental_treatment_sittings
                        (id, plan_id, sitting_number, sitting_title, scheduled_date, completed_date, doctor_notes, prescriptions, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, 'scheduled', %s, %s)
                        """,
                        (
                            sit_id,
                            plan_id,
                            s.sitting_number,
                            s.sitting_title.strip(),
                            s.scheduled_date.strip() if s.scheduled_date else None,
                            s.doctor_notes.strip() if s.doctor_notes else None,
                            s.prescriptions.strip() if s.prescriptions else None,
                            now,
                            now,
                        ),
                    )
            else:
                # Default sittings generated automatically
                for num in range(1, req.total_sittings + 1):
                    sit_id = f"SIT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
                    sit_title = f"Sitting {num}: Procedure Stage {num}"
                    sched_date = req.start_date if num == 1 else req.next_sitting_date
                    await cur.execute(
                        """
                        INSERT INTO dental_treatment_sittings
                        (id, plan_id, sitting_number, sitting_title, scheduled_date, completed_date, doctor_notes, prescriptions, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, 'scheduled', %s, %s)
                        """,
                        (sit_id, plan_id, num, sit_title, sched_date, now, now),
                    )

            return {
                "success": True,
                "plan_id": plan_id,
                "message": f"Treatment plan created with {req.total_sittings} sittings",
            }


@router.patch("/{plan_id}")
async def api_update_treatment_plan(plan_id: str, req: UpdatePlanRequest):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            updates = []
            params = []
            if req.status is not None:
                updates.append("status = %s")
                params.append(req.status.strip())
            if req.paid_amount is not None:
                updates.append("paid_amount = %s")
                params.append(req.paid_amount)
            if req.next_sitting_date is not None:
                updates.append("next_sitting_date = %s")
                params.append(req.next_sitting_date.strip() if req.next_sitting_date else None)
            if req.clinical_notes is not None:
                updates.append("clinical_notes = %s")
                params.append(req.clinical_notes.strip())

            if not updates:
                return {"success": True, "message": "No fields to update"}

            updates.append("updated_at = %s")
            params.append(datetime.now())
            params.append(plan_id)

            sql = f"UPDATE dental_treatment_plans SET {', '.join(updates)} WHERE id = %s"
            await cur.execute(sql, tuple(params))
            return {"success": True, "plan_id": plan_id, "message": "Plan updated"}


@router.post("/{plan_id}/sittings/{sitting_id}/complete")
async def api_complete_sitting(plan_id: str, sitting_id: str, req: CompleteSittingRequest):
    pool = await _get_pool()
    now = datetime.now()
    completed_date = req.completed_date or now.strftime("%Y-%m-%d")

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # 1. Update the sitting
            updates = ["status = 'completed'", "completed_date = %s", "updated_at = %s"]
            params = [completed_date, now]
            if req.doctor_notes:
                updates.append("doctor_notes = %s")
                params.append(req.doctor_notes.strip())
            if req.prescriptions:
                updates.append("prescriptions = %s")
                params.append(req.prescriptions.strip())

            params.append(sitting_id)
            params.append(plan_id)
            sql = f"UPDATE dental_treatment_sittings SET {', '.join(updates)} WHERE id = %s AND plan_id = %s"
            await cur.execute(sql, tuple(params))

            # 2. Count completed sittings
            await cur.execute(
                "SELECT COUNT(*) as completed_count FROM dental_treatment_sittings WHERE plan_id = %s AND status = 'completed'",
                (plan_id,),
            )
            count_row = await cur.fetchone()
            comp_count = int(count_row.get("completed_count") or 0)

            # 3. Get total sittings from plan
            await cur.execute("SELECT total_sittings FROM dental_treatment_plans WHERE id = %s", (plan_id,))
            plan_row = await cur.fetchone()
            total_sittings = int(plan_row.get("total_sittings") or 1) if plan_row else 1

            new_status = "completed" if comp_count >= total_sittings else "in_progress"

            # 4. Update parent plan
            plan_updates = ["completed_sittings = %s", "status = %s", "updated_at = %s"]
            plan_params = [comp_count, new_status, now]
            if req.next_sitting_date:
                plan_updates.append("next_sitting_date = %s")
                plan_params.append(req.next_sitting_date.strip())
            elif new_status == "completed":
                plan_updates.append("next_sitting_date = NULL")

            plan_params.append(plan_id)
            await cur.execute(
                f"UPDATE dental_treatment_plans SET {', '.join(plan_updates)} WHERE id = %s",
                tuple(plan_params),
            )

            return {
                "success": True,
                "sitting_id": sitting_id,
                "plan_id": plan_id,
                "completed_sittings": comp_count,
                "total_sittings": total_sittings,
                "plan_status": new_status,
                "message": f"Sitting marked completed ({comp_count}/{total_sittings})",
            }

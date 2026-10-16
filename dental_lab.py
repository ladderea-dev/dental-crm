"""Dental Laboratory Tracking Module for Smile Dental Clinic & Implant Centre.

Tracks prosthetic orders sent to external dental labs (e.g. Zirconia crowns,
bridges, dentures, ceramic veneers, clear aligners) across clinic branches.
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

logger = logging.getLogger("dental_lab")
router = APIRouter(prefix="/api/dental-lab", tags=["dental-lab"])

_LAB_POOL: Optional[aiomysql.Pool] = None


async def _get_pool() -> aiomysql.Pool:
    global _LAB_POOL
    if _LAB_POOL is None:
        _LAB_POOL = await aiomysql.create_pool(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306") or 3306),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            db=os.getenv("MYSQL_DATABASE", "smiledental_crm"),
            autocommit=True,
            charset="utf8mb4",
        )
    return _LAB_POOL


class CreateLabOrderRequest(BaseModel):
    patient_name: str = Field(min_length=1, max_length=191)
    patient_phone: str = Field(default="", max_length=32)
    doctor_name: str = Field(min_length=1, max_length=191)
    branch_name: str = Field(default="ECIL / Kapra (Main Clinic)", max_length=100)
    lab_name: str = Field(min_length=1, max_length=191)
    item_type: str = Field(min_length=1, max_length=100)
    tooth_numbers: str = Field(default="", max_length=100)
    shade: str = Field(default="A2", max_length=32)
    sent_date: str = Field(description="YYYY-MM-DD")
    expected_date: str = Field(description="YYYY-MM-DD")
    lab_cost: float = Field(default=0.0, ge=0.0)
    patient_charge: float = Field(default=0.0, ge=0.0)
    notes: str = Field(default="")


class UpdateLabOrderRequest(BaseModel):
    status: Optional[str] = None
    received_date: Optional[str] = None
    lab_cost: Optional[float] = None
    patient_charge: Optional[float] = None
    notes: Optional[str] = None


@router.get("/orders")
async def api_list_lab_orders(
    branch_name: Optional[str] = None,
    status: Optional[str] = None,
    doctor_name: Optional[str] = None,
):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            query = "SELECT * FROM dental_lab_orders WHERE 1=1"
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

            query += " ORDER BY expected_date ASC, created_at DESC"
            await cur.execute(query, tuple(params))
            rows = await cur.fetchall()

            for r in rows:
                if isinstance(r.get("sent_date"), (datetime, date)):
                    r["sent_date"] = r["sent_date"].strftime("%Y-%m-%d")
                if isinstance(r.get("expected_date"), (datetime, date)):
                    r["expected_date"] = r["expected_date"].strftime("%Y-%m-%d")
                if isinstance(r.get("received_date"), (datetime, date)):
                    r["received_date"] = r["received_date"].strftime("%Y-%m-%d")
                if isinstance(r.get("created_at"), datetime):
                    r["created_at"] = r["created_at"].isoformat()
                if isinstance(r.get("updated_at"), datetime):
                    r["updated_at"] = r["updated_at"].isoformat()
                if "lab_cost" in r:
                    r["lab_cost"] = float(r["lab_cost"])
                if "patient_charge" in r:
                    r["patient_charge"] = float(r["patient_charge"])

            return {"success": True, "count": len(rows), "orders": rows}


@router.post("/orders")
async def api_create_lab_order(req: CreateLabOrderRequest):
    order_id = f"LAB-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    now = datetime.now()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dental_lab_orders
                (id, patient_name, patient_phone, doctor_name, branch_name, lab_name, item_type, tooth_numbers, shade, sent_date, expected_date, received_date, status, lab_cost, patient_charge, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 'sent_to_lab', %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    req.patient_name.strip(),
                    req.patient_phone.strip(),
                    req.doctor_name.strip(),
                    req.branch_name.strip(),
                    req.lab_name.strip(),
                    req.item_type.strip(),
                    req.tooth_numbers.strip(),
                    req.shade.strip(),
                    req.sent_date.strip(),
                    req.expected_date.strip(),
                    req.lab_cost,
                    req.patient_charge,
                    req.notes.strip(),
                    now,
                    now,
                ),
            )
            return {"success": True, "order_id": order_id, "message": "Dental lab order created"}


@router.patch("/orders/{order_id}")
async def api_update_lab_order(order_id: str, req: UpdateLabOrderRequest):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            updates = []
            params = []
            if req.status is not None:
                updates.append("status = %s")
                params.append(req.status.strip())
            if req.received_date is not None:
                updates.append("received_date = %s")
                params.append(req.received_date.strip() if req.received_date else None)
            if req.lab_cost is not None:
                updates.append("lab_cost = %s")
                params.append(req.lab_cost)
            if req.patient_charge is not None:
                updates.append("patient_charge = %s")
                params.append(req.patient_charge)
            if req.notes is not None:
                updates.append("notes = %s")
                params.append(req.notes.strip())

            if not updates:
                return {"success": True, "message": "No fields to update"}

            updates.append("updated_at = %s")
            params.append(datetime.now())
            params.append(order_id)

            sql = f"UPDATE dental_lab_orders SET {', '.join(updates)} WHERE id = %s"
            await cur.execute(sql, tuple(params))
            return {"success": True, "order_id": order_id, "message": "Lab order updated"}


@router.get("/stats")
async def api_get_lab_stats(branch_name: Optional[str] = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            query = "SELECT status, COUNT(*) as cnt, SUM(lab_cost) as total_lab_cost, SUM(patient_charge) as total_patient_charge FROM dental_lab_orders"
            params = []
            if branch_name and branch_name.strip():
                query += " WHERE branch_name = %s"
                params.append(branch_name.strip())
            query += " GROUP BY status"

            await cur.execute(query, tuple(params) if params else None)
            rows = await cur.fetchall()

            status_counts = {
                "sent_to_lab": 0,
                "in_fabrication": 0,
                "received_at_clinic": 0,
                "trial_fitted": 0,
                "cemented_done": 0,
                "remake_required": 0,
            }
            total_orders = 0
            total_lab_cost = 0.0
            total_patient_charge = 0.0

            for r in rows:
                st = r.get("status") or "sent_to_lab"
                cnt = int(r.get("cnt") or 0)
                status_counts[st] = cnt
                total_orders += cnt
                total_lab_cost += float(r.get("total_lab_cost") or 0)
                total_patient_charge += float(r.get("total_patient_charge") or 0)

            return {
                "success": True,
                "branch_name": branch_name or "All Branches",
                "total_orders": total_orders,
                "status_counts": status_counts,
                "total_lab_cost": total_lab_cost,
                "total_patient_charge": total_patient_charge,
                "gross_margin": total_patient_charge - total_lab_cost,
            }

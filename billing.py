"""Billing, Invoicing, Revenue Tracking & Sales Reporting for Smile Dental and Implant Centre.

Provides:
- Invoices generation with itemized dental treatments & tooth numbers
- Partial & full payment recording (UPI, Cash, Card, EMI)
- Doctor-wise revenue attribution (Dr. P. Sudhakar vs Dr. K. Mounika)
- Service/treatment category analytics & sales reports
- Instant WhatsApp invoice / receipt delivery
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv(".env", override=True)

logger = logging.getLogger("billing")

_BILLING_POOL = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_pool():
    global _BILLING_POOL
    if _BILLING_POOL is None:
        import aiomysql
        _BILLING_POOL = await aiomysql.create_pool(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306") or 3306),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            db=os.getenv("MYSQL_DATABASE", "smiledental_crm"),
            autocommit=True,
            charset="utf8mb4",
        )
    return _BILLING_POOL


async def generate_invoice_number() -> str:
    """Generate sequential invoice number: SD-YYYY-XXXX."""
    year = datetime.now().year
    prefix = f"SD-{year}-"
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT invoice_number FROM invoices WHERE invoice_number LIKE %s ORDER BY created_at DESC LIMIT 1",
                (f"{prefix}%",)
            )
            row = await cur.fetchone()
            if row and row[0]:
                try:
                    last_num = int(row[0].replace(prefix, ""))
                    return f"{prefix}{last_num + 1:04d}"
                except ValueError:
                    pass
            return f"{prefix}0001"


async def create_invoice(payload: dict) -> dict:
    """Create a new patient invoice with treatment line items."""
    patient_name = (payload.get("patient_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    if not patient_name or not phone:
        raise ValueError("Patient name and phone number are required.")

    doctor_name = (payload.get("doctor_name") or "Dr. P. Sudhakar").strip()
    doctor_id = (payload.get("doctor_id") or "").strip()
    branch_name = (payload.get("branch_name") or "ECIL / Kapra (Main Clinic)").strip()
    treatment_date_str = payload.get("treatment_date") or date.today().isoformat()
    notes = (payload.get("notes") or "").strip()
    payment_method = (payload.get("payment_method") or "UPI").strip()

    items = payload.get("items") or []
    if not items:
        # Default single consultation item if none provided
        items = [{"treatment_name": "Dental Checkup & Consultation", "tooth_number": "", "quantity": 1, "unit_price": 500.0}]

    subtotal = Decimal("0.00")
    clean_items = []
    for item in items:
        t_name = (item.get("treatment_name") or "Dental Treatment").strip()
        tooth = str(item.get("tooth_number") or "").strip()
        qty = max(1, int(item.get("quantity") or 1))
        unit_p = Decimal(str(item.get("unit_price") or 0.00))
        tot_p = unit_p * qty
        subtotal += tot_p
        clean_items.append({
            "id": str(uuid.uuid4()),
            "treatment_name": t_name,
            "tooth_number": tooth,
            "quantity": qty,
            "unit_price": float(unit_p),
            "total_price": float(tot_p),
        })

    discount = Decimal(str(payload.get("discount") or "0.00"))
    tax = Decimal(str(payload.get("tax") or "0.00"))
    total_amount = max(Decimal("0.00"), subtotal - discount + tax)
    paid_amount = Decimal(str(payload.get("paid_amount") or total_amount))
    balance_amount = max(Decimal("0.00"), total_amount - paid_amount)

    if paid_amount >= total_amount:
        status = "paid"
    elif paid_amount > 0:
        status = "partial"
    else:
        status = "pending"

    inv_id = str(uuid.uuid4())
    inv_number = payload.get("invoice_number") or await generate_invoice_number()

    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 1. Insert invoice header
            await cur.execute(
                """
                INSERT INTO invoices (
                    id, invoice_number, patient_name, phone, doctor_id, doctor_name, branch_name,
                    treatment_date, subtotal, discount, tax, total_amount, paid_amount,
                    balance_amount, payment_status, payment_method, notes,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    NOW(6), NOW(6)
                )
                """,
                (
                    inv_id, inv_number, patient_name, phone, doctor_id, doctor_name, branch_name,
                    treatment_date_str, float(subtotal), float(discount), float(tax),
                    float(total_amount), float(paid_amount), float(balance_amount),
                    status, payment_method, notes
                )
            )

            # 2. Insert invoice line items
            for item in clean_items:
                await cur.execute(
                    """
                    INSERT INTO invoice_items (
                        id, invoice_id, treatment_name, tooth_number, quantity, unit_price, total_price, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, NOW(6)
                    )
                    """,
                    (
                        item["id"], inv_id, item["treatment_name"], item["tooth_number"],
                        item["quantity"], item["unit_price"], item["total_price"]
                    )
                )

            # 3. Insert initial payment record if paid_amount > 0
            if paid_amount > 0:
                payment_id = str(uuid.uuid4())
                await cur.execute(
                    """
                    INSERT INTO invoice_payments (
                        id, invoice_id, amount, payment_method, payment_date, notes, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, NOW(6)
                    )
                    """,
                    (payment_id, inv_id, float(paid_amount), payment_method, treatment_date_str, "Initial Payment at Consultation")
                )

    return await get_invoice(inv_id)


async def get_invoice(invoice_id: str) -> Optional[dict]:
    """Retrieve full invoice with line items and payment history."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM invoices WHERE id = %s LIMIT 1", (invoice_id,))
            inv_row = await cur.fetchone()
            if not inv_row:
                return None

            col_names = [desc[0] for desc in cur.description]
            inv = dict(zip(col_names, inv_row))
            # Normalize date and decimals
            if isinstance(inv.get("treatment_date"), (date, datetime)):
                inv["treatment_date"] = str(inv["treatment_date"])
            for f in ("subtotal", "discount", "tax", "total_amount", "paid_amount", "balance_amount"):
                inv[f] = float(inv.get(f) or 0.0)

            # Fetch items
            await cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY created_at ASC", (invoice_id,))
            item_rows = await cur.fetchall()
            item_cols = [desc[0] for desc in cur.description]
            inv["items"] = []
            for r in item_rows:
                it = dict(zip(item_cols, r))
                it["unit_price"] = float(it.get("unit_price") or 0.0)
                it["total_price"] = float(it.get("total_price") or 0.0)
                inv["items"].append(it)

            # Fetch payments
            await cur.execute("SELECT * FROM invoice_payments WHERE invoice_id = %s ORDER BY created_at ASC", (invoice_id,))
            pmt_rows = await cur.fetchall()
            pmt_cols = [desc[0] for desc in cur.description]
            inv["payments"] = []
            for r in pmt_rows:
                pmt = dict(zip(pmt_cols, r))
                pmt["amount"] = float(pmt.get("amount") or 0.0)
                if isinstance(pmt.get("payment_date"), (date, datetime)):
                    pmt["payment_date"] = str(pmt["payment_date"])
                inv["payments"].append(pmt)

            return inv


async def list_invoices(
    q: Optional[str] = None,
    status: Optional[str] = None,
    doctor_name: Optional[str] = None,
    branch_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List invoices with filters, search, and pagination."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        where_clauses = ["1=1"]
        params = []

        if q and q.strip():
            term = f"%{q.strip()}%"
            where_clauses.append("(patient_name LIKE %s OR phone LIKE %s OR invoice_number LIKE %s)")
            params.extend([term, term, term])

        if status and status.strip():
            where_clauses.append("payment_status = %s")
            params.append(status.strip().lower())

        if doctor_name and doctor_name.strip():
            where_clauses.append("doctor_name LIKE %s")
            params.append(f"%{doctor_name.strip()}%")

        if branch_name and branch_name.strip():
            where_clauses.append("branch_name LIKE %s")
            params.append(f"%{branch_name.strip()}%")

        if date_from and date_from.strip():
            where_clauses.append("treatment_date >= %s")
            params.append(date_from.strip())

        if date_to and date_to.strip():
            where_clauses.append("treatment_date <= %s")
            params.append(date_to.strip())

        where_str = " AND ".join(where_clauses)

        async with conn.cursor() as cur:
            # Count total
            await cur.execute(f"SELECT COUNT(*) FROM invoices WHERE {where_str}", tuple(params))
            total_count = (await cur.fetchone())[0]

            # Fetch rows
            query = f"""
                SELECT id, invoice_number, patient_name, phone, doctor_name, branch_name, treatment_date,
                       subtotal, discount, total_amount, paid_amount, balance_amount,
                       payment_status, payment_method, created_at
                FROM invoices
                WHERE {where_str}
                ORDER BY treatment_date DESC, created_at DESC
                LIMIT %s OFFSET %s
            """
            exec_params = list(params) + [limit, offset]
            await cur.execute(query, tuple(exec_params))
            rows = await cur.fetchall()
            cols = [desc[0] for desc in cur.description]

            invoices = []
            for r in rows:
                item = dict(zip(cols, r))
                if isinstance(item.get("treatment_date"), (date, datetime)):
                    item["treatment_date"] = str(item["treatment_date"])
                for f in ("subtotal", "discount", "total_amount", "paid_amount", "balance_amount"):
                    item[f] = float(item.get(f) or 0.0)
                invoices.append(item)

            return {
                "invoices": invoices,
                "total": total_count,
                "limit": limit,
                "offset": offset,
            }


async def add_invoice_payment(invoice_id: str, amount: float, payment_method: str = "UPI", notes: str = "") -> dict:
    """Record an installment or balance payment on an invoice."""
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT total_amount, paid_amount FROM invoices WHERE id = %s LIMIT 1", (invoice_id,))
            row = await cur.fetchone()
            if not row:
                raise ValueError("Invoice not found.")

            total_amount = float(row[0] or 0.0)
            cur_paid = float(row[1] or 0.0)
            new_paid = cur_paid + float(amount)
            new_balance = max(0.0, total_amount - new_paid)

            new_status = "paid" if new_balance <= 0.001 else "partial"

            # Insert payment record
            payment_id = str(uuid.uuid4())
            today_str = date.today().isoformat()
            await cur.execute(
                """
                INSERT INTO invoice_payments (id, invoice_id, amount, payment_method, payment_date, notes, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(6))
                """,
                (payment_id, invoice_id, float(amount), payment_method, today_str, notes)
            )

            # Update invoice header
            await cur.execute(
                """
                UPDATE invoices
                SET paid_amount = %s, balance_amount = %s, payment_status = %s, updated_at = NOW(6)
                WHERE id = %s
                """,
                (new_paid, new_balance, new_status, invoice_id)
            )

    return await get_invoice(invoice_id)


async def get_revenue_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch_name: Optional[str] = None
) -> dict:
    """Aggregate financial KPIs, doctor revenue attribution, and category breakdown."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        where_clauses = ["1=1"]
        params = []
        if date_from and date_from.strip():
            where_clauses.append("treatment_date >= %s")
            params.append(date_from.strip())
        if date_to and date_to.strip():
            where_clauses.append("treatment_date <= %s")
            params.append(date_to.strip())
        if branch_name and branch_name.strip():
            where_clauses.append("branch_name LIKE %s")
            params.append(f"%{branch_name.strip()}%")

        where_str = " AND ".join(where_clauses)

        async with conn.cursor() as cur:
            # 1. Total KPI aggregates
            await cur.execute(
                f"""
                SELECT
                    COUNT(*) as invoice_count,
                    COALESCE(SUM(total_amount), 0) as total_billed,
                    COALESCE(SUM(paid_amount), 0) as total_collected,
                    COALESCE(SUM(balance_amount), 0) as total_outstanding,
                    COALESCE(SUM(discount), 0) as total_discount
                FROM invoices
                WHERE {where_str}
                """,
                tuple(params)
            )
            kpi_row = await cur.fetchone()
            inv_count = int(kpi_row[0] or 0)
            tot_billed = float(kpi_row[1] or 0.0)
            tot_collected = float(kpi_row[2] or 0.0)
            tot_outstanding = float(kpi_row[3] or 0.0)
            tot_discount = float(kpi_row[4] or 0.0)
            avg_bill = (tot_billed / inv_count) if inv_count > 0 else 0.0

            # 2. Doctor-wise breakdown
            await cur.execute(
                f"""
                SELECT
                    doctor_name,
                    COUNT(*) as count,
                    COALESCE(SUM(total_amount), 0) as billed,
                    COALESCE(SUM(paid_amount), 0) as collected
                FROM invoices
                WHERE {where_str}
                GROUP BY doctor_name
                ORDER BY billed DESC
                """,
                tuple(params)
            )
            doc_rows = await cur.fetchall()
            doctors = []
            for r in doc_rows:
                doctors.append({
                    "doctor_name": r[0] or "General",
                    "count": int(r[1] or 0),
                    "billed": float(r[2] or 0.0),
                    "collected": float(r[3] or 0.0),
                })

            # 3. Branch-wise Revenue breakdown
            await cur.execute(
                f"""
                SELECT
                    COALESCE(branch_name, 'ECIL / Kapra (Main Clinic)') as branch,
                    COUNT(*) as count,
                    COALESCE(SUM(total_amount), 0) as billed,
                    COALESCE(SUM(paid_amount), 0) as collected,
                    COALESCE(SUM(balance_amount), 0) as balance
                FROM invoices
                WHERE {where_str}
                GROUP BY branch
                ORDER BY billed DESC
                """,
                tuple(params)
            )
            branch_rows = await cur.fetchall()
            branches = [
                {
                    "branch_name": r[0] or "ECIL / Kapra (Main Clinic)",
                    "count": int(r[1] or 0),
                    "billed": float(r[2] or 0.0),
                    "collected": float(r[3] or 0.0),
                    "balance": float(r[4] or 0.0),
                }
                for r in branch_rows
            ]

            # 4. Payment Method breakdown
            await cur.execute(
                f"""
                SELECT
                    payment_method,
                    COUNT(*) as count,
                    COALESCE(SUM(paid_amount), 0) as collected
                FROM invoices
                WHERE {where_str}
                GROUP BY payment_method
                ORDER BY collected DESC
                """,
                tuple(params)
            )
            method_rows = await cur.fetchall()
            methods = []
            for r in method_rows:
                methods.append({
                    "method": r[0] or "Other",
                    "count": int(r[1] or 0),
                    "collected": float(r[2] or 0.0),
                })

            # 5. Status counts
            await cur.execute(
                f"""
                SELECT
                    payment_status,
                    COUNT(*) as count,
                    COALESCE(SUM(total_amount), 0) as billed,
                    COALESCE(SUM(balance_amount), 0) as balance
                FROM invoices
                WHERE {where_str}
                GROUP BY payment_status
                """,
                tuple(params)
            )
            status_rows = await cur.fetchall()
            statuses = {r[0]: {"count": int(r[1]), "billed": float(r[2]), "balance": float(r[3])} for r in status_rows}

            # 6. Top Treatments / Service breakdown
            item_where_str = where_str.replace("treatment_date", "inv.treatment_date").replace("branch_name", "inv.branch_name")
            await cur.execute(
                f"""
                SELECT
                    it.treatment_name,
                    COUNT(*) as times_performed,
                    COALESCE(SUM(it.total_price), 0) as revenue
                FROM invoice_items it
                JOIN invoices inv ON it.invoice_id = inv.id
                WHERE {item_where_str}
                GROUP BY it.treatment_name
                ORDER BY revenue DESC
                LIMIT 8
                """,
                tuple(params)
            )
            treatment_rows = await cur.fetchall()
            treatments = [
                {"treatment_name": r[0], "count": int(r[1]), "revenue": float(r[2])}
                for r in treatment_rows
            ]

            return {
                "currency": "₹",
                "invoice_count": inv_count,
                "total_billed": tot_billed,
                "total_collected": tot_collected,
                "total_outstanding": tot_outstanding,
                "total_discount": tot_discount,
                "average_bill_value": round(avg_bill, 2),
                "doctor_revenue": doctors,
                "branch_revenue": branches,
                "payment_methods": methods,
                "payment_statuses": statuses,
                "top_treatments": treatments,
            }


def generate_whatsapp_receipt_text(invoice: dict) -> str:
    """Format an itemized, friendly WhatsApp invoice receipt for the patient."""
    inv_num = invoice.get("invoice_number", "SD-BILL")
    p_name = invoice.get("patient_name", "Valued Patient")
    doc_name = invoice.get("doctor_name", "Specialist Doctor")
    t_date = invoice.get("treatment_date", str(date.today()))
    tot = invoice.get("total_amount", 0.0)
    paid = invoice.get("paid_amount", 0.0)
    bal = invoice.get("balance_amount", 0.0)
    method = invoice.get("payment_method", "UPI")
    status = (invoice.get("payment_status") or "paid").upper()

    items = invoice.get("items") or []
    item_lines = []
    for it in items:
        tooth_str = f" (Tooth #{it['tooth_number']})" if it.get("tooth_number") else ""
        item_lines.append(f"• {it['treatment_name']}{tooth_str} - ₹{it['total_price']:,.2f}")

    items_text = "\n".join(item_lines) if item_lines else "• Dental Consultation & Treatment"

    msg = f"""🦷 *SMILE DENTAL AND IMPLANT CENTRE*
Kapra Circle, Saket Road, ECIL, Secunderabad
📞 +91 7396529265 | +91 9490618635

🧾 *TREATMENT RECEIPT / BILL*
━━━━━━━━━━━━━━━━━━━━
*Bill No:* {inv_num}
*Date:* {t_date}
*Patient Name:* {p_name}
*Attending Doctor:* {doc_name}

*Treatments Performed:*
{items_text}

━━━━━━━━━━━━━━━━━━━━
*Grand Total:* ₹{tot:,.2f}
*Amount Paid:* ₹{paid:,.2f} ({method})
*Balance Due:* ₹{bal:,.2f}
*Payment Status:* {status}
━━━━━━━━━━━━━━━━━━━━

Thank you for choosing Smile Dental & Implant Centre! Keep smiling! 😊
📍 Clinic Directions: https://maps.google.com/?cid=11928096181775791226
"""
    return msg


async def export_sales_csv(date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
    """Generate CSV string of sales report."""
    res = await list_invoices(date_from=date_from, date_to=date_to, limit=1000, offset=0)
    invoices = res.get("invoices") or []

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "Invoice Number", "Date", "Patient Name", "Phone", "Doctor",
        "Subtotal (INR)", "Discount (INR)", "Total Amount (INR)",
        "Paid Amount (INR)", "Balance Due (INR)", "Status", "Payment Method"
    ])

    for inv in invoices:
        writer.writerow([
            inv.get("invoice_number"),
            inv.get("treatment_date"),
            inv.get("patient_name"),
            inv.get("phone"),
            inv.get("doctor_name"),
            f"{inv.get('subtotal', 0):.2f}",
            f"{inv.get('discount', 0):.2f}",
            f"{inv.get('total_amount', 0):.2f}",
            f"{inv.get('paid_amount', 0):.2f}",
            f"{inv.get('balance_amount', 0):.2f}",
            inv.get("payment_status"),
            inv.get("payment_method"),
        ])

    return out.getvalue()

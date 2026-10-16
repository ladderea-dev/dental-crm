"""Telecaller Workspace & Human Dialing System for AirZen CRM.

Provides complete telecaller operations:
- Lead queue management (assigned/unassigned leads)
- Next-lead auto-fetching and locking
- LiveKit room bridge generation for in-browser calling
- Disposition submission & CRM contact updating
- Personal callback scheduling & performance tracking
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from db import (
    get_crm_contact_by_phone,
    get_crm_contacts,
    get_repository,
    get_setting,
    log_call,
    log_error,
    normalize_phone,
    update_lead_journey,
)
from followup import parse_followup_time

logger = logging.getLogger("telecaller")

VALID_DISPOSITIONS = {
    "interested": "Interested / Hot Lead",
    "callback_requested": "Callback Requested",
    "demo_booked": "Demo / Appointment Booked",
    "not_interested": "Not Interested",
    "wrong_number": "Wrong Number",
    "no_answer": "No Answer",
    "busy": "Line Busy",
    "converted": "Converted / Won",
    "lost": "Lost / Disqualified",
    "call_later": "Follow-up Later",
}


class DispositionRequest(BaseModel):
    phone: str
    outcome: str
    notes: Optional[str] = ""
    callback_at: Optional[str] = None  # ISO timestamp or relative phrase like "tomorrow 2pm"
    service_type: Optional[str] = None
    budget: Optional[str] = None
    requirement: Optional[str] = None


class TelecallerCallRequest(BaseModel):
    phone: str
    lead_name: Optional[str] = None


class ReassignRequest(BaseModel):
    phones: Optional[List[str]] = None
    phone_numbers: Optional[List[str]] = None
    target_telecaller: Optional[str] = ""
    notes: Optional[str] = None
    strategy: Optional[str] = "least_loaded"

    def get_phones(self) -> List[str]:
        return self.phones or self.phone_numbers or []


async def get_telecaller_stats(telecaller_username: str) -> Dict[str, Any]:
    """Calculate daily and total stats for a human telecaller."""
    repository = get_repository()
    calls = []
    try:
        calls = await repository.list_call_logs(limit=2000)
    except Exception as e:
        logger.warning("Could not list call logs in get_telecaller_stats: %s", e)
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    user_calls = [
        c for c in calls
        if str(c.get("telecaller_username") or "").lower() == str(telecaller_username).lower()
    ]
    
    today_calls = [c for c in user_calls if str(c.get("timestamp") or "")[:10] == today_str]
    today_interested = [c for c in today_calls if c.get("outcome") in ("interested", "demo_booked", "converted")]
    
    # Get assigned leads count
    contacts = await get_crm_contacts()
    assigned = [
        c for c in contacts
        if str(c.get("assigned_to") or c.get("assigned_telecaller") or "").lower() == str(telecaller_username).lower()
    ]
    
    callbacks_due = [
        c for c in assigned
        if c.get("next_followup_at") and str(c.get("next_followup_at"))[:10] <= today_str and not c.get("stop_automation")
    ]

    return {
        "telecaller": telecaller_username,
        "today_calls_count": len(today_calls),
        "today_interested_count": len(today_interested),
        "total_calls_count": len(user_calls),
        "assigned_leads_count": len(assigned),
        "callbacks_due_count": len(callbacks_due),
        "conversion_rate_percent": round((len(today_interested) / len(today_calls) * 100), 1) if today_calls else 0.0,
    }


async def is_lead_locked(lead: Dict[str, Any], current_user: str) -> bool:
    """Check if lead is actively locked by another telecaller."""
    locked_by = str(lead.get("locked_by") or "").strip().lower()
    locked_until_str = str(lead.get("locked_until") or "").strip()
    if not locked_by or not locked_until_str:
        return False
    if locked_by == current_user.strip().lower():
        return False
    try:
        locked_until = datetime.fromisoformat(locked_until_str.replace("Z", "+00:00"))
        return locked_until > datetime.now(timezone.utc)
    except Exception:
        return False


async def get_telecaller_queue(telecaller_username: str, limit: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve queue of leads prioritized for this telecaller, skipping leads locked by other telecallers."""
    contacts = await get_crm_contacts()
    queue = []
    u_lower = telecaller_username.strip().lower()
    now_utc = datetime.now(timezone.utc)
    tag_clean = tag.strip().lower() if (tag and tag.strip()) else None
    
    for c in contacts:
        if c.get("stop_automation"):
            continue
        status = (c.get("crm_status") or "").lower()
        
        # Don't queue completed won/lost leads
        if status in ("converted", "closed won", "lost", "closed lost", "not interested", "wrong number"):
            continue

        if tag_clean:
            raw_t = c.get("tags_json")
            try:
                lead_tags = json.loads(raw_t) if isinstance(raw_t, str) else (raw_t or [])
                if not any(tag_clean == str(t).strip().lower() for t in lead_tags):
                    continue
            except Exception:
                continue

        # Skip leads locked by another active telecaller
        locked_by = str(c.get("locked_by") or "").strip().lower()
        locked_until_str = str(c.get("locked_until") or "").strip()
        if locked_by and locked_until_str and locked_by != u_lower:
            try:
                locked_until = datetime.fromisoformat(locked_until_str.replace("Z", "+00:00"))
                if locked_until > now_utc:
                    continue
            except Exception:
                pass

        # Match leads assigned to this user OR unassigned open leads
        if assigned := (c.get("assigned_to") or c.get("assigned_telecaller") or "").strip().lower():
            if assigned == u_lower or u_lower == "admin":
                queue.append(c)
        else:
            queue.append(c)
            
    # Sort queue: Callbacks due first, then New Leads, then by creation date
    def _priority_key(item):
        next_followup = item.get("next_followup_at") or ""
        status = (item.get("crm_status") or "").lower()
        is_callback = 0 if (next_followup and status == "callback_requested") else 1
        is_new = 0 if "new" in status else 1
        return (is_callback, is_new, next_followup or "9999")
        
    queue.sort(key=_priority_key)
    return queue[:limit]


async def fetch_next_lead_for_telecaller(telecaller_username: str) -> Optional[Dict[str, Any]]:
    """Fetch next available lead and atomically lock and assign it to the telecaller."""
    queue = await get_telecaller_queue(telecaller_username, limit=10)
    if not queue:
        return None
        
    selected = None
    now_utc = datetime.now(timezone.utc)
    u_clean = telecaller_username.strip()
    u_lower = u_clean.lower()

    for lead in queue:
        locked_by = str(lead.get("locked_by") or "").strip().lower()
        locked_until_str = str(lead.get("locked_until") or "").strip()
        if locked_by and locked_until_str and locked_by != u_lower:
            try:
                locked_until = datetime.fromisoformat(locked_until_str.replace("Z", "+00:00"))
                if locked_until > now_utc:
                    continue
            except Exception:
                pass
        selected = lead
        break

    if not selected:
        return None

    phone = selected.get("phone_number")
    lock_expiry = (now_utc + timedelta(seconds=300)).isoformat()
    
    await update_lead_journey(phone, {
        "assigned_to": u_clean,
        "assigned_telecaller": u_clean,
        "locked_by": u_clean,
        "locked_until": lock_expiry,
    })
    selected["assigned_to"] = u_clean
    selected["locked_by"] = u_clean
    selected["locked_until"] = lock_expiry
    return selected


async def get_telecaller_followups(
    telecaller_username: str,
    scope: str = "my",
    timezone_str: Optional[str] = None,
    tag: Optional[str] = None
) -> Dict[str, Any]:
    """Retrieve categorized follow-up leads (Overdue, Due Today, Upcoming, Uncontacted)."""
    tz_name = timezone_str or (await get_setting("OUTBOUND_TIMEZONE", "Asia/Singapore")) or "Asia/Singapore"
    try:
        from zoneinfo import ZoneInfo
        now_tz = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now_tz = datetime.now()
    
    today_str = now_tz.strftime("%Y-%m-%d")
    contacts = await get_crm_contacts()
    
    overdue, due_today, upcoming, uncontacted = [], [], [], []
    u_lower = telecaller_username.strip().lower()
    tag_clean = tag.strip().lower() if (tag and tag.strip()) else None

    for c in contacts:
        if c.get("stop_automation"):
            continue
        status = (c.get("crm_status") or "").lower()
        if status in ("converted", "closed won", "lost", "closed lost", "not interested", "wrong number"):
            continue

        if tag_clean:
            raw_t = c.get("tags_json")
            try:
                lead_tags = json.loads(raw_t) if isinstance(raw_t, str) else (raw_t or [])
                if not any(tag_clean == str(t).strip().lower() for t in lead_tags):
                    continue
            except Exception:
                continue
        
        assigned = str(c.get("assigned_to") or c.get("assigned_telecaller") or "").strip().lower()
        if scope == "my" and assigned and assigned != u_lower:
            continue
        if scope == "my" and not assigned and u_lower not in ("admin", "md"):
            continue

        fup = str(c.get("next_followup_at") or "").strip()
        fup_day = fup[:10]
        attempts = int(c.get("call_attempts") or c.get("attempts") or 0)
        
        if attempts == 0 and not fup_day:
            uncontacted.append(c)
        elif fup_day:
            if fup_day < today_str:
                overdue.append(c)
            elif fup_day == today_str:
                due_today.append(c)
            elif fup_day > today_str:
                upcoming.append(c)
        else:
            uncontacted.append(c)

    overdue.sort(key=lambda x: str(x.get("next_followup_at") or ""))
    due_today.sort(key=lambda x: str(x.get("next_followup_at") or ""))
    upcoming.sort(key=lambda x: str(x.get("next_followup_at") or ""))
    uncontacted.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    buckets_map = {
        "overdue": overdue[:100],
        "due_today": due_today[:100],
        "upcoming": upcoming[:100],
        "uncontacted": uncontacted[:100],
    }
    return {
        "telecaller": telecaller_username,
        "scope": scope,
        "today": today_str,
        "timezone": tz_name,
        "counts": {
            "overdue": len(overdue),
            "due_today": len(due_today),
            "upcoming": len(upcoming),
            "uncontacted": len(uncontacted),
            "total_open": len(overdue) + len(due_today) + len(upcoming) + len(uncontacted),
        },
        "buckets": buckets_map,
        "overdue": overdue[:100],
        "due_today": due_today[:100],
        "upcoming": upcoming[:100],
        "uncontacted": uncontacted[:100],
    }


_ROUND_ROBIN_INDEX = 0


async def assign_lead_auto(phone: str, strategy: str = "round_robin") -> Optional[str]:
    """Auto-assign a lead to an active telecaller using Round-Robin or Least-Loaded strategy."""
    global _ROUND_ROBIN_INDEX
    from auth_users import get_all_users
    users = await get_all_users()
    active_telecallers = [
        u["username"] for u in users
        if u.get("active", True) and u.get("role") in ("telecaller", "admin")
    ]
    if not active_telecallers:
        return None

    assigned_user = None
    if strategy == "least_loaded":
        contacts = await get_crm_contacts()
        counts = {u.lower(): 0 for u in active_telecallers}
        for c in contacts:
            a = str(c.get("assigned_to") or c.get("assigned_telecaller") or "").strip().lower()
            if a in counts:
                status = (c.get("crm_status") or "").lower()
                if status not in ("converted", "closed won", "lost", "closed lost", "not interested"):
                    counts[a] += 1
        best_u = min(active_telecallers, key=lambda u: counts.get(u.lower(), 0))
        assigned_user = best_u
    else:
        assigned_user = active_telecallers[_ROUND_ROBIN_INDEX % len(active_telecallers)]
        _ROUND_ROBIN_INDEX += 1

    if assigned_user:
        await update_lead_journey(phone, {
            "assigned_to": assigned_user,
            "assigned_telecaller": assigned_user,
        })
    return assigned_user


async def reassign_leads(phones: List[str], target_telecaller: str) -> Dict[str, Any]:
    """Bulk reassign leads to a specific telecaller."""
    clean_target = target_telecaller.strip()
    success_count = 0
    for p in phones:
        phone = normalize_phone(p)
        if phone:
            try:
                await update_lead_journey(phone, {
                    "assigned_to": clean_target,
                    "assigned_telecaller": clean_target,
                })
                success_count += 1
            except Exception as e:
                logger.warning("Could not reassign lead %s: %s", phone, e)
    return {
        "success": True,
        "target_telecaller": clean_target,
        "reassigned_count": success_count,
    }


async def get_telecaller_performance_report(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    telecaller_username: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate comprehensive performance report and analytics for all or single telecaller."""
    repository = get_repository()
    calls = []
    try:
        calls = await repository.list_call_logs(limit=5000)
    except Exception as e:
        logger.warning("Could not list call logs in get_telecaller_performance_report: %s", e)
    from auth_users import get_all_users
    users = await get_all_users()
    
    if date_from or date_to:
        filtered = []
        for c in calls:
            ts = str(c.get("timestamp") or "")[:10]
            if date_from and ts < date_from:
                continue
            if date_to and ts > date_to:
                continue
            filtered.append(c)
        calls = filtered

    telecallers = [u["username"] for u in users if u.get("role") in ("telecaller", "admin")]
    if telecaller_username:
        telecallers = [t for t in telecallers if t.lower() == telecaller_username.lower()]

    contacts = await get_crm_contacts()
    today_str = datetime.now().strftime("%Y-%m-%d")

    report_rows = []
    for tc in telecallers:
        tc_calls = [c for c in calls if str(c.get("telecaller_username") or "").lower() == tc.lower()]
        total_dials = len(tc_calls)
        connected_calls = len([c for c in tc_calls if int(c.get("duration_seconds") or 0) >= 15 or c.get("outcome") in ("interested", "demo_booked", "converted", "callback_requested")])
        total_talk_sec = sum(int(c.get("duration_seconds") or 0) for c in tc_calls)
        avg_talk_sec = round(total_talk_sec / total_dials, 1) if total_dials else 0.0
        
        demos_booked = len([c for c in tc_calls if c.get("outcome") == "demo_booked"])
        converted = len([c for c in tc_calls if c.get("outcome") == "converted"])
        interested = len([c for c in tc_calls if c.get("outcome") == "interested"])
        callbacks = len([c for c in tc_calls if c.get("outcome") == "callback_requested"])

        disp_map = {}
        for c in tc_calls:
            o = str(c.get("outcome") or "other").lower()
            disp_map[o] = disp_map.get(o, 0) + 1

        tc_assigned = [c for c in contacts if str(c.get("assigned_to") or c.get("assigned_telecaller") or "").lower() == tc.lower()]
        tc_overdue = len([c for c in tc_assigned if c.get("next_followup_at") and str(c.get("next_followup_at"))[:10] < today_str and not c.get("stop_automation")])
        tc_due_today = len([c for c in tc_assigned if c.get("next_followup_at") and str(c.get("next_followup_at"))[:10] == today_str and not c.get("stop_automation")])

        conv_rate = round((converted + demos_booked) / total_dials * 100, 1) if total_dials else 0.0

        report_rows.append({
            "telecaller": tc,
            "total_dials": total_dials,
            "connected_calls": connected_calls,
            "total_talk_time_seconds": total_talk_sec,
            "avg_talk_time_seconds": avg_talk_sec,
            "interested_count": interested,
            "demos_booked_count": demos_booked,
            "converted_count": converted,
            "callbacks_scheduled": callbacks,
            "conversion_rate_percent": conv_rate,
            "assigned_leads_count": len(tc_assigned),
            "overdue_followups_count": tc_overdue,
            "today_followups_count": tc_due_today,
            "dispositions": disp_map,
        })

    report_rows.sort(key=lambda r: r["total_dials"], reverse=True)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "telecaller_count": len(report_rows),
        "summary": {
            "total_dials": sum(r["total_dials"] for r in report_rows),
            "total_connected": sum(r["connected_calls"] for r in report_rows),
            "total_demos_booked": sum(r["demos_booked_count"] for r in report_rows),
            "total_converted": sum(r["converted_count"] for r in report_rows),
            "total_talk_time_seconds": sum(r["total_talk_time_seconds"] for r in report_rows),
        },
        "rows": report_rows,
    }


async def process_telecaller_disposition(
    req: DispositionRequest,
    telecaller_username: str,
    telecaller_id: Optional[str] = None
) -> Dict[str, Any]:
    """Save telecaller call disposition, update CRM contact and create callback action."""
    phone = normalize_phone(req.phone)
    outcome = req.outcome.strip().lower()
    
    contact = await get_crm_contact_by_phone(phone) or {}
    lead_name = contact.get("lead_name") or "Contact"
    
    # Calculate duration (if tracked) or default 60s
    duration = 60
    
    # 1. Log call to call_logs
    await log_call(
        phone_number=phone,
        lead_name=lead_name,
        outcome=outcome,
        reason=f"Telecaller {telecaller_username} disposition: {req.notes or outcome}",
        duration_seconds=duration,
        notes=req.notes,
    )
    
    # Update call log with telecaller info
    repository = get_repository()
    recent_calls = await repository.list_call_logs(phone=phone, limit=1)
    if recent_calls:
        latest_call_id = recent_calls[0]["id"]
        await repository.update_call_log(latest_call_id, {
            "caller_type": "telecaller",
            "telecaller_username": telecaller_username,
            "telecaller_id": telecaller_id,
            "notes": req.notes,
        })

    # 2. Map disposition to CRM stage and status
    crm_updates = {
        "last_call_outcome": outcome,
        "last_call_at": datetime.now(timezone.utc).isoformat(),
        "assigned_to": telecaller_username,
        "locked_by": None,
        "locked_until": None,
    }
    
    if req.notes:
        existing_notes = contact.get("crm_notes") or ""
        timestamp = datetime.now().strftime("%d-%m %H:%M")
        crm_updates["crm_notes"] = f"[{timestamp} - {telecaller_username}]: {req.notes}\n{existing_notes}".strip()

    if outcome == "interested":
        crm_updates["crm_status"] = "Hot Lead"
        crm_updates["journey_stage"] = "interested"
    elif outcome == "demo_booked":
        crm_updates["crm_status"] = "demo_booked"
        crm_updates["journey_stage"] = "demo_booked"
    elif outcome == "converted":
        crm_updates["crm_status"] = "Closed Won"
        crm_updates["journey_stage"] = "converted"
    elif outcome in ("not_interested", "lost"):
        crm_updates["crm_status"] = "Not Interested" if outcome == "not_interested" else "Closed Lost"
        crm_updates["journey_stage"] = "not_interested" if outcome == "not_interested" else "lost"
        crm_updates["stop_automation"] = True
        crm_updates["stop_automation_reason"] = f"Telecaller disposition: {outcome}"
    elif outcome == "wrong_number":
        crm_updates["crm_status"] = "Wrong Number"
        crm_updates["stop_automation"] = True
        crm_updates["stop_automation_reason"] = "Telecaller reported wrong number"
    elif outcome == "callback_requested":
        crm_updates["crm_status"] = "callback_requested"
        crm_updates["journey_stage"] = "callback_requested"
        
        # Parse callback time
        if req.callback_at:
            try:
                callback_dt = parse_followup_time(req.callback_at)
                crm_updates["next_followup_at"] = callback_dt.isoformat()
                crm_updates["preferred_callback_at"] = callback_dt.isoformat()
            except Exception as e:
                logger.warning("Failed to parse callback time: %s", e)

    if req.requirement:
        crm_updates["requirement"] = req.requirement
    if req.budget:
        crm_updates["budget"] = req.budget
    if req.service_type:
        crm_updates["service_type"] = req.service_type

    await update_lead_journey(phone, crm_updates)
    await log_error("telecaller", "disposition_submitted", f"phone={phone}; user={telecaller_username}; outcome={outcome}", "info")
    
    return {
        "success": True,
        "phone": phone,
        "outcome": outcome,
        "crm_updates": crm_updates,
    }

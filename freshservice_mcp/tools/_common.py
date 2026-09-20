"""Shared helpers for tool implementations."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Optional

# FreshService fixed numeric values (from the API reference).
STATUSES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PRIORITIES = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


def status_name(value) -> str:
    try:
        return STATUSES.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def priority_name(value) -> str:
    try:
        return PRIORITIES.get(int(value), str(value))
    except (TypeError, ValueError):
        return str(value)


def summarize_ticket(t: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact, human-readable summary of a ticket dict."""
    requester = t.get("requester") or {}
    responder = t.get("responder") or {}
    group = t.get("group") or {}
    return {
        "id": t.get("id"),
        "display_id": t.get("display_id"),
        "subject": t.get("subject"),
        "status": status_name(t.get("status")),
        "status_id": t.get("status"),
        "priority": priority_name(t.get("priority")),
        "priority_id": t.get("priority"),
        "type": t.get("type"),
        "source": t.get("source"),
        "requester": (requester.get("name") if isinstance(requester, dict) else None),
        "requester_email": (requester.get("email") if isinstance(requester, dict) else None),
        "responder": (responder.get("name") if isinstance(responder, dict) else None),
        "group": (group.get("name") if isinstance(group, dict) else None),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "due_by": t.get("due_by"),
        "tags": t.get("tags"),
        "cc_emails": t.get("cc_emails"),
        "description_text": t.get("description_text"),
    }


def summarize_conversation(c: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact summary of a conversation (reply or note)."""
    source = c.get("source") or {}
    from_email = c.get("from_email")
    return {
        "id": c.get("id"),
        "ticket_id": c.get("ticket_id"),
        "private": bool(c.get("private")),
        "incoming": bool(c.get("incoming")),
        "body_text": c.get("body_text"),
        "from_email": from_email,
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "source": source.get("name") if isinstance(source, dict) else None,
    }


def today_start_iso() -> str:
    """Return ISO-8601 timestamp for the start of today (local) for filter
    queries."""
    d = date.today()
    dt = datetime.combine(d, time.min)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_email(emails) -> Optional[str]:
    """Return a cleaned email string from various shapes, or None."""
    if emails is None:
        return None
    if isinstance(emails, str):
        emails = [emails]
    cleaned = [e.strip() for e in emails if e and e.strip()]
    return ",".join(cleaned) if cleaned else None
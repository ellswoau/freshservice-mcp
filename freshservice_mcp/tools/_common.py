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
    """Return today's date as YYYY-MM-DD for filter queries (FreshService's
    created_at comparison uses date-only values)."""
    return date.today().isoformat()


def normalize_email(emails) -> Optional[str]:
    """Return a cleaned email string from various shapes, or None."""
    if emails is None:
        return None
    if isinstance(emails, str):
        emails = [emails]
    cleaned = [e.strip() for e in emails if e and e.strip()]
    return ",".join(cleaned) if cleaned else None


def current_agent_id(client) -> Optional[int]:
    """Return the id of the agent the API key authenticates as (via
    /agents/me), used for time entries, resolution responder, etc."""
    data = client.get_json("/agents/me")
    agent = data.get("agent") if isinstance(data, dict) else None
    return (agent or {}).get("id")


def summarize_requester(r: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact summary of a FreshService requester/contact."""
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "email": r.get("email") or r.get("primary_email"),
        "active": r.get("active"),
        "job_title": r.get("job_title"),
        "department_id": r.get("department_id"),
        "department_name": r.get("department_name") or (r.get("department") or {}).get("name") if isinstance(r.get("department"), dict) else r.get("department"),
    }


def find_requesters(client, email: Optional[str] = None, name: Optional[str] = None,
                    *, max_pages: int = 6, per_page: int = 100) -> list:
    """Locate FreshService requesters (contacts) by matching email or name.

    The `/api/v2/requesters` list endpoint does not expose a reliable email or
    name query filter, so we scan a bounded number of pages and match
    client-side. ``max_pages`` caps how many pages we pull to stay well within
    the API rate limit. Returns matching requester dicts (summarised).
    """
    needle_email = (email or "").strip().lower()
    needle_name = (name or "").strip().lower()
    matches = []
    seen = set()
    for page in range(1, max_pages + 1):
        batch = client.get_list(
            "/requesters",
            per_page=per_page,
            page=page,
            envelope_key="requesters",
        )
        if not batch:
            break
        for r in batch:
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            r_email = (r.get("email") or r.get("primary_email") or "").lower()
            r_name = (r.get("name") or "").lower()
            if needle_email and needle_email in r_email:
                matches.append(summarize_requester(r))
            elif needle_name and needle_name in r_name:
                matches.append(summarize_requester(r))
        if len(batch) < per_page:
            break
    return matches
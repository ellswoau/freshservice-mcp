"""Conversation operations: reply to the requester, add private internal notes,
list conversation history, CC a manager, and notify/escalate to IT team members.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import get_client
from ._common import summarize_conversation, normalize_email


def _require_ticket_id(ticket_id) -> int:
    # Accept bare ids ('47199') or display-form ids ('INC-47199', 'SR-39').
    m = re.search(r"\d+", str(ticket_id).strip())
    if not m:
        raise ValueError("ticket_id must be a positive integer, e.g. 47199 or INC-47199.")
    tid = int(m.group(0))
    if tid <= 0:
        raise ValueError("ticket_id must be a positive integer.")
    return tid


def register(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    @mcp.tool()
    def reply_to_requestor(ticket_id: int, body: str,
                           to_emails: Optional[List[str]] = None) -> dict:
        """Send an outgoing reply to the ticket requester (and any extra
        recipients via to_emails, e.g. to keep someone else in the loop). The
        reply becomes a public, requester-visible conversation entry.

        Uses POST /api/v2/tickets/{id}/reply."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not body or not body.strip():
            raise ValueError("body must not be empty.")
        payload: dict = {"body": body}
        if to_emails:
            payload["to_emails"] = list(to_emails)
        result = client.post_json(f"/tickets/{tid}/reply", payload)
        return {
            "ticket_id": tid,
            "sent": True,
            "to_emails": to_emails or [],
            "conversation_id": (result or {}).get("conversation", {}).get("id")
            or (result or {}).get("note", {}).get("id"),
        }

    @mcp.tool()
    def add_private_note(ticket_id: int, body: str) -> dict:
        """Add a private internal note to a ticket (not visible to the
        requester). Use for internal observations, troubleshooting notes, or to
        pass context to colleagues.

        Uses POST /api/v2/tickets/{id}/notes with private=true (multipart)."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not body or not body.strip():
            raise ValueError("body must not be empty.")
        result = client.post_form(f"/tickets/{tid}/notes", {"body": body, "private": "true"})
        return {
            "ticket_id": tid,
            "added": True,
            "conversation_id": (result or {}).get("note", {}).get("id")
            or (result or {}).get("conversation", {}).get("id"),
        }

    @mcp.tool()
    def list_ticket_conversations(ticket_id: int) -> dict:
        """List the full conversation history on a ticket (replies, notes, emails)
        newest first, marking each as public or private."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        data = client.get_json(f"/tickets/{tid}/conversations")
        convs = data.get("conversations") if isinstance(data, dict) else data
        if not isinstance(convs, list):
            convs = []
        return {
            "ticket_id": tid,
            "count": len(convs),
            "conversations": [summarize_conversation(c) for c in convs],
        }

    @mcp.tool()
    def cc_email_on_ticket(ticket_id: int, emails: List[str]) -> dict:
        """CC additional email addresses on a ticket (e.g. the requester's
        manager) so they receive updates via email. Emails are added to both the
        forward-Cc and reply-Cc lists."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        # Read the current ticket to append to the existing cc list.
        ticket = client.get_one(f"/tickets/{tid}", key="ticket")
        existing_cc = (ticket.get("cc_emails") or [])
        new_cc = []
        for e in emails:
            if e not in existing_cc:
                existing_cc.append(e)
                new_cc.append(e)
        updated = client.put_json(f"/tickets/{tid}", {"cc_emails": existing_cc})
        return {
            "ticket_id": tid,
            "cc_added": emails,
            "all_cc_emails": existing_cc,
            "ticket": updated.get("ticket") or {},
        }

    @mcp.tool()
    def notify_emails(ticket_id: int, emails: List[str], body: Optional[str] = None) -> dict:
        """Notify additional people (IT team members / manager / escalation
        contacts) by adding their emails to the ticket's CC list so they get
        email updates, optionally with a private note for the team.

        (FreshService has no dedicated 'notify once' endpoint; adding to
        cc_emails is the supported way to loop people into a ticket.)"""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not emails:
            raise ValueError("Provide at least one email to notify.")
        ticket = client.get_one(f"/tickets/{tid}", key="ticket")
        existing_cc = (ticket.get("cc_emails") or [])
        new_cc = list(existing_cc)
        for e in emails:
            if e not in new_cc:
                new_cc.append(e)
        updated = client.put_json(f"/tickets/{tid}", {"cc_emails": new_cc})
        noted = False
        if body and body.strip():
            try:
                client.post_form(f"/tickets/{tid}/notes", {"body": body, "private": "true"})
                noted = True
            except Exception:
                noted = False
        return {
            "ticket_id": tid,
            "notified_emails": [e for e in emails if e not in existing_cc],
            "all_cc_emails": new_cc,
            "note_added": noted,
            "ticket": updated.get("ticket") or {},
        }
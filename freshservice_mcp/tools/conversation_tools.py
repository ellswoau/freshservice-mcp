"""Conversation operations: reply to the requester, add private internal notes,
list conversation history, CC a manager, and notify/escalate to IT team members.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import get_client
from ._common import summarize_conversation, normalize_email


def _require_ticket_id(ticket_id) -> int:
    tid = int(str(ticket_id).strip())
    if tid <= 0:
        raise ValueError("ticket_id must be a positive integer.")
    return tid


def register(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    @mcp.tool()
    def reply_to_requestor(ticket_id: int, body: str,
                           to_emails: Optional[List[str]] = None) -> dict:
        """Send an outgoing reply to the ticket requester (and any extra
        recipients via to_emails, e.g. to keep someone else in the loop). The
        reply becomes a public, requester-visible conversation entry."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not body or not body.strip():
            raise ValueError("body must not be empty.")
        payload: dict = {"body": body}
        if to_emails:
            payload["to_emails"] = list(to_emails)
        result = client.post_json(f"/tickets/{tid}/conversations/reply", payload)
        return {
            "ticket_id": tid,
            "sent": True,
            "to_emails": to_emails or [],
            "conversation_id": (result or {}).get("conversation", {}).get("id"),
        }

    @mcp.tool()
    def add_private_note(ticket_id: int, body: str) -> dict:
        """Add a private internal note to a ticket (not visible to the
        requester). Use for internal observations, troubleshooting notes, or to
        pass context to colleagues."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not body or not body.strip():
            raise ValueError("body must not be empty.")
        result = client.post_json(f"/tickets/{tid}/conversations/note", {"body": body})
        return {
            "ticket_id": tid,
            "added": True,
            "conversation_id": (result or {}).get("conversation", {}).get("id"),
        }

    @mcp.tool()
    def list_ticket_conversations(ticket_id: int) -> dict:
        """List the full conversation history on a ticket (replies, notes, emails)
        newest first, marking each as public or private."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        convs = client.get_json(f"/tickets/{tid}/conversations")
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
        # Read the current ticket to append to existing cc lists.
        ticket = client.get_one(f"/tickets/{tid}", key="ticket")
        existing_cc = ticket.get("cc_emails") or []
        existing_reply_cc = ticket.get("reply_cc_emails") or []
        new_cc = list(existing_cc) + [e for e in emails if e not in existing_cc]
        new_reply_cc = list(existing_reply_cc) + [e for e in emails if e not in existing_reply_cc]
        updated = client.put_json(
            f"/tickets/{tid}",
            {"cc_emails": new_cc, "reply_cc_emails": new_reply_cc},
        )
        return {
            "ticket_id": tid,
            "cc_added": emails,
            "all_cc_emails": new_cc,
            "ticket": updated.get("ticket") or {},
        }

    @mcp.tool()
    def notify_emails(ticket_id: int, emails: List[str], body: Optional[str] = None) -> dict:
        """Notify additional people (IT team members / manager / escalation
        contacts) about a ticket by email. Unlike CC (which stays on the ticket)
        this fires a notification email to the given addresses now. Optionally
        include a body message and, optionally, add a private note via
        add_private_note."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not emails:
            raise ValueError("Provide at least one email to notify.")
        payload: dict = {"emails": list(emails), "cc": True}
        result = client.post_json(f"/tickets/{tid}/notify", payload)
        noted = False
        if body and body.strip():
            client.post_json(f"/tickets/{tid}/conversations/note", {"body": body})
            noted = True
        return {
            "ticket_id": tid,
            "notified_emails": list(emails),
            "note_added": noted,
            "result": result,
        }
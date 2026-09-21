"""Conversation operations: reply to the requester, add private internal notes,
list conversation history, CC a manager, notify/escalate to IT team members, and
read requester attachments (screenshots pasted inline in the HTML body).
"""
from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, List, Optional, Union

from mcp.types import ImageContent, TextContent

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import get_client
from ._common import extract_inline_attachments, summarize_conversation, normalize_email

# Accept bare ids (47208) or display-form ids (INC-47208): the helper parses a
# number out of either, so the schema must not force a strict int.
TicketId = Union[int, str]


# Guardrails so a single call cannot pull unbounded bytes / images into context.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_CALL = 8
# Default size gate: email-signature logos are ~190x53, real screenshots are
# larger. Used only by the bulk image tool; an explicit id bypasses it.
DEFAULT_MIN_WIDTH = 250
DEFAULT_MIN_HEIGHT = 120


def _require_ticket_id(ticket_id) -> int:
    # Accept bare ids ('47199') or display-form ids ('INC-47199', 'SR-39').
    m = re.search(r"\d+", str(ticket_id).strip())
    if not m:
        raise ValueError("ticket_id must be a positive integer, e.g. 47199 or INC-47199.")
    tid = int(m.group(0))
    if tid <= 0:
        raise ValueError("ticket_id must be a positive integer.")
    return tid


def _probe_dims(raw: bytes) -> tuple:
    """Return (width, height) for an image, or (None, None) if unreadable."""
    try:
        from PIL import Image as PILImage  # type: ignore
    except Exception:
        return None, None
    try:
        import io
        with PILImage.open(io.BytesIO(raw)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None, None


def _download(client, tid: int, a: dict) -> tuple:
    """Fetch one attachment's bytes.

    Prefers the (pre-signed) URL found in the conversation body; falls back to
    the ``/attachments/{id}`` endpoint by id. Returns (bytes, content_type,
    filename).
    """
    url = a.get("url")
    if url:
        return client.download_binary(url)
    aid = a.get("attachment_id")
    if aid is None:
        raise ValueError("attachment has neither a url nor an id")
    return client.download_binary(f"/attachments/{int(aid)}")


def _collect_attachments(client, tid: int, conversation_id: Optional[int] = None) -> List[dict]:
    """Gather attachments across a ticket's conversations.

    Combines inline images parsed from each conversation's HTML body with any
    entries in the conversation ``attachments`` array.
    """
    data = client.get_json(f"/tickets/{tid}/conversations")
    convs = data.get("conversations") if isinstance(data, dict) else data
    if not isinstance(convs, list):
        convs = []
    out: List[dict] = []
    for c in convs:
        if conversation_id is not None and int(c.get("id")) != int(conversation_id):
            continue
        meta = {
            "conversation_id": c.get("id"),
            "incoming": bool(c.get("incoming")),
            "from_email": c.get("from_email"),
            "created_at": c.get("created_at"),
        }
        for a in extract_inline_attachments(c.get("body")):
            out.append({**meta, **a})
        for a in (c.get("attachments") or []):
            if isinstance(a, dict):
                out.append({
                    **meta,
                    "attachment_id": a.get("id"),
                    "url": a.get("attachment_url") or a.get("url"),
                    "alt": a.get("name"),
                    "width": None,
                    "height": None,
                })
    return out



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
    def list_ticket_attachments(ticket_id: TicketId,
                                conversation_id: Optional[int] = None,
                                min_width: int = 0,
                                min_height: int = 0,
                                probe_sizes: bool = True):
        """List the attachments on a ticket's conversations, **including images
        the requester pasted inline in the message body**.

        Important: FreshService renders pasted screenshots as inline ``<img>``
        tags inside the conversation HTML body and leaves the API's
        ``attachments`` array empty -- so a plain text view of the conversation
        shows a 'blank' message. This tool surfaces them.

        Returns metadata per attachment: ``attachment_id``, ``conversation_id``,
        ``from_email``, ``created_at``, ``content_type``, ``size``, ``width``,
        ``height`` and the (pre-signed) ``url``. Set ``probe_sizes=False`` to
        skip downloading each image just to measure it. Use ``min_width`` /
        ``min_height`` to filter out small signature logos (~190x53).

        To actually *see* a screenshot, pass its id to ``view_attachments``."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        items = _collect_attachments(client, tid, conversation_id)
        result = []
        for a in items:
            entry = {
                "attachment_id": a.get("attachment_id"),
                "conversation_id": a.get("conversation_id"),
                "from_email": a.get("from_email"),
                "incoming": a.get("incoming"),
                "created_at": a.get("created_at"),
                "name": a.get("alt"),
                "width": a.get("width"),
                "height": a.get("height"),
                "content_type": None,
                "size": None,
                "probed": False,
            }
            if probe_sizes:
                try:
                    raw, ctype, _name = _download(client, tid, a)
                    entry["content_type"] = ctype
                    entry["size"] = len(raw)
                    w, h = _probe_dims(raw)
                    if w is not None:
                        entry["width"], entry["height"] = w, h
                    entry["probed"] = True
                except Exception as exc:  # noqa: BLE001 - report, don't fail the listing
                    entry["error"] = str(exc)[:200]
            if min_width and (entry["width"] or 0) < min_width:
                continue
            if min_height and (entry["height"] or 0) < min_height:
                continue
            result.append(entry)
        return {
            "ticket_id": tid,
            "count": len(result),
            "attachments": result,
            "hint": (
                "Call view_attachments(ticket_id, attachment_ids=[...]) to load "
                "a screenshot into context."
            ),
        }

    @mcp.tool()
    def view_attachments(ticket_id: TicketId,
                         attachment_ids: Optional[List[int]] = None,
                         conversation_id: Optional[int] = None,
                         max_images: int = 4,
                         min_width: int = DEFAULT_MIN_WIDTH,
                         min_height: int = DEFAULT_MIN_HEIGHT):
        """Download image attachments from a ticket and return them *as images*
        so a vision-capable model can read a screenshot directly.

        Use this whenever a requester's message looks empty or says 'see the
        error below' but the text has no content: the content is usually an
        inline screenshot. Discover ids with ``list_ticket_attachments`` first,
        or leave ``attachment_ids`` unset to auto-load the likely screenshots on
        the ticket (small signature logos are skipped via ``min_width`` /
        ``min_height``).

        ``max_images`` caps how many are returned; each image is capped at 8 MB.
        """
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        items = _collect_attachments(client, tid, conversation_id)

        wanted = set(int(i) for i in (attachment_ids or []) if i is not None)
        selected = []
        for a in items:
            if wanted and a.get("attachment_id") not in wanted:
                continue
            selected.append(a)

        out: List[object] = []
        loaded = 0
        for a in selected:
            if loaded >= max(1, min(int(max_images), MAX_IMAGES_PER_CALL)):
                break
            try:
                raw, ctype, name = _download(client, tid, a)
            except Exception as exc:  # noqa: BLE001
                out.append(TextContent(type="text", text=(
                    f"attachment {a.get('attachment_id')} could not be "
                    f"downloaded: {exc}")))
                continue
            # When ids are explicitly requested the caller has decided it
            # wants the image, so skip the logo-size gate.
            if not wanted:
                w, h = _probe_dims(raw)
                if (min_width and w is not None and w < min_width) or \
                        (min_height and h is not None and h < min_height):
                    continue
            if not ctype.startswith("image/"):
                out.append(TextContent(type="text", text=(
                    f"attachment {a.get('attachment_id')} is {ctype} "
                    f"({len(raw)} bytes), not an image -- not rendered.")))
                continue
            if len(raw) > MAX_IMAGE_BYTES:
                out.append(TextContent(type="text", text=(
                    f"attachment {a.get('attachment_id')} is {len(raw)} bytes "
                    f"(> {MAX_IMAGE_BYTES}); too large to inline.")))
                continue
            w, h = _probe_dims(raw)
            label = (
                f"Attachment {a.get('attachment_id')} from "
                f"{a.get('from_email') or 'unknown'} at {a.get('created_at')} "
                f"({ctype}, {w}x{h}px):"
            )
            out.append(TextContent(type="text", text=label))
            out.append(ImageContent(
                type="image",
                data=base64.b64encode(raw).decode("ascii"),
                mime_type=ctype,
            ))
            loaded += 1
        if not out:
            return [TextContent(type="text", text=(
                f"No matching image attachments found on ticket {tid} "
                f"(considered {len(items)} attachment(s))."))]
        return out

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
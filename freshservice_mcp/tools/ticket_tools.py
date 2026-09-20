"""Ticket operations for L1/L2 helpdesk work.

View, filter, and manage FreshService tickets: list recent, filter by query,
view a single ticket, create/update, change status/priority, categorize
(ticket type), assign, look up tickets by a requester, and log time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import FreshServiceClient, get_client
from ._common import summarize_ticket, today_start_iso


def _require_ticket_id(ticket_id) -> int:
    tid = int(str(ticket_id).strip())
    if tid <= 0:
        raise ValueError("ticket_id must be a positive integer.")
    return tid


def register(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    @mcp.tool()
    def list_tickets(per_page: int = 50, page: int = 1,
                     order_by: str = "created_at", order_type: str = "desc") -> dict:
        """Return a paginated list of non-deleted tickets from the active ticket
        view (most recent first by default). Set order_by to e.g. 'created_at',
        'updated_at', 'due_by', 'priority' or 'status'; order_type to 'asc' or
        'desc'. Per page is capped at 100."""
        client = get_client(config)
        tickets = client.get_list(
            "/tickets",
            params={
                "order_by": order_by,
                "order_type": order_type,
            },
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "page": page,
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def list_recent_tickets(limit: int = 100) -> dict:
        """Return the most recent tickets (newest first), e.g. the latest 100.
        This is the quick equivalent of a 'latest <limit> tickets' inbox."""
        client = get_client(config)
        limit = max(1, min(int(limit), 300))
        tickets = client.get_list(
            "/tickets",
            params={"order_by": "created_at", "order_type": "desc"},
            per_page=limit,
            envelope_key="tickets",
        )
        return {
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def filter_tickets(query: str, per_page: int = 50, page: int = 1) -> dict:
        """List tickets using FreshService's filter query language.

        Examples:
          status:2                       -> all Open tickets
          status:2 OR priority:3         -> Open OR High priority
          status:4 AND group_id:11       -> Resolved in group 11
          priority:4 OR priority:3       -> Urgent or High
          created_at:>'2024-01-01' AND created_at:<'2024-02-01'
          requester_id:1000000675        -> tickets raised by a contact id
          agent_id:null                  -> unassigned tickets
          deleted:true                   -> include deleted tickets

        Use `view_tickets_by_user` for a friendlier by-requester lookup.
        """
        client = get_client(config)
        tickets = client.get_list(
            "/tickets/filter",
            params={"query": query},
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "query": query,
            "page": page,
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def list_tickets_by_status(status: str, per_page: int = 100) -> dict:
        """Return tickets filtered by status name, e.g. 'open', 'pending',
        'resolved', 'closed'. Requested by status name for readability."""
        status_map = {"open": "2", "pending": "3", "resolved": "4", "closed": "5"}
        key = (status or "").strip().lower()
        if key not in status_map:
            raise ValueError(
                "status must be one of: open, pending, resolved, closed."
            )
        client = get_client(config)
        tickets = client.get_list(
            "/tickets/filter",
            params={"query": f"status:{status_map[key]}"},
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "status": key,
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def list_tickets_opened_today(per_page: int = 200) -> dict:
        """Return all tickets opened today (created_at after local midnight)."""
        client = get_client(config)
        query = f"created_at:>'{today_start_iso()}'"
        tickets = client.get_list(
            "/tickets/filter",
            params={"query": query},
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "opened_after": today_start_iso(),
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def view_ticket(ticket_id: int, include_conversations: bool = False) -> dict:
        """View a single ticket by id. Set include_conversations=True to also
        return the latest replies/notes. Returns full ticket attributes,
        requester/responder/group details and timestamps."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        params = {"include": "requester,responder,stats,company"} if include_conversations else None
        ticket = client.get_one(f"/tickets/{tid}", params=params, key="ticket")
        result = summarize_ticket(ticket)
        if include_conversations:
            try:
                convs = client.get_json(f"/tickets/{tid}/conversations")
            except Exception:
                convs = None
            result["conversations"] = convs if isinstance(convs, list) else []
        return result

    @mcp.tool()
    def view_tickets_by_user(email: Optional[str] = None,
                             requester_id: Optional[int] = None,
                             per_page: int = 200) -> dict:
        """List all tickets raised by a specific requester (~user). Provide
        either the requester's email or their FreshService contact id. Returns
        each ticket's dates, subject, status, priority and timestamps."""
        client = get_client(config)
        if requester_id is None:
            if not email or not email.strip():
                raise ValueError("Provide either 'email' or 'requester_id'.")
            # Resolve contact id from email.
            contacts = client.get_list(
                "/contacts",
                params={"query": f"email:{email.strip()}"},
                per_page=10,
                envelope_key="contacts",
            )
            if not contacts:
                return {"error": f"No contact found for email {email}.", "tickets": []}
            requester_id = contacts[0].get("id")
        else:
            requester_id = int(requester_id)
            if email:
                contacts = client.get_list(
                    "/contacts",
                    params={"query": f"id:{requester_id}"},
                    per_page=10,
                    envelope_key="contacts",
                )
                email = (contacts[0].get("email") if contacts else email)

        tickets = client.get_list(
            "/tickets/filter",
            params={"query": f"requester_id:{requester_id}"},
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "requester_id": requester_id,
            "requester_email": email,
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def create_ticket(subject: str, description: str, email: Optional[str] = None,
                      requester_id: Optional[int] = None, priority: Optional[int] = None,
                      status: Optional[int] = None, ticket_type: Optional[str] = None,
                      group_id: Optional[int] = None, cc_emails: Optional[List[str]] = None) -> dict:
        """Create a new ticket. Provide requester email (contact auto-resolved/
        created) or requester_id. priority and status are FreshService numeric
        ids: status Open=2/Pending=3/Resolved=4/Closed=5; priority Low=1/
        Medium=2/High=3/Urgent=4. ticket_type e.g. Incident / Request / Change."""
        client = get_client(config)
        body: dict = {"subject": subject, "description": description,
                      "priority": priority, "status": status,
                      "type": ticket_type, "group_id": group_id}
        if requester_id is not None:
            body["requester_id"] = int(requester_id)
        elif email and email.strip():
            body["email"] = email.strip()
        elif email is None and requester_id is None:
            raise ValueError("Provide a requester email or requester_id.")
        if cc_emails:
            body["cc_emails"] = list(cc_emails)
        created = client.post_json("/tickets", {k: v for k, v in body.items() if v is not None})
        return {"created": True, "ticket": summarize_ticket(created.get("ticket") or {})}

    @mcp.tool()
    def update_ticket(ticket_id: int, updates: dict) -> dict:
        """Update any ticket attributes generically via an ``updates`` dict
        (e.g. {"subject": ..., "description": ..., "cc_emails": [...],
        "tags": [...], "due_by": ...}). Pass only the fields you want to
        change, or use the dedicated status/priority/categorize/assign tools
        for those. Returns the updated ticket."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not updates or not isinstance(updates, dict):
            raise ValueError("Provide an updates dict with at least one field.")
        updated = client.put_json(f"/tickets/{tid}", updates)
        return {"updated": True, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def set_ticket_status(ticket_id: int, status: str) -> dict:
        """Change a ticket's status. Accepts a name ('open', 'pending',
        'resolved', 'closed') or a numeric id (2/3/4/5). Returns the updated
        ticket."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        status_map = {"open": 2, "pending": 3, "resolved": 4, "closed": 5}
        key = str(status).strip().lower()
        if key.isdigit():
            status_id = int(key)
        else:
            if key not in status_map:
                raise ValueError("status must be open, pending, resolved, or closed.")
            status_id = status_map[key]
        updated = client.put_json(f"/tickets/{tid}", {"status": status_id})
        return {"ticket_id": tid, "status": status, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def set_ticket_priority(ticket_id: int, priority: str) -> dict:
        """Change a ticket's priority. Accepts a name ('low', 'medium', 'high',
        'urgent') or a numeric id (1/2/3/4). Returns the updated ticket."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        priority_map = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
        key = str(priority).strip().lower()
        if key.isdigit():
            priority_id = int(key)
        else:
            if key not in priority_map:
                raise ValueError("priority must be low, medium, high, or urgent.")
            priority_id = priority_map[key]
        updated = client.put_json(f"/tickets/{tid}", {"priority": priority_id})
        return {"ticket_id": tid, "priority": priority, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def categorize_ticket(ticket_id: int, ticket_type: str,
                          group_id: Optional[int] = None,
                          priority: Optional[str] = None) -> dict:
        """Categorize/classify a ticket by setting its type (e.g. 'Incident',
        'Request', 'Feature Request', 'Change'), optionally moving it to a
        group and/or setting its priority. group_id comes from list_groups."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        body: dict = {"type": ticket_type}
        if group_id is not None:
            body["group_id"] = int(group_id)
        if priority is not None:
            pmap = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
            key = priority.strip().lower()
            body["priority"] = pmap[key] if key in pmap else int(priority)
        updated = client.put_json(f"/tickets/{tid}", body)
        return {"ticket_id": tid, "categorized": True, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def assign_ticket(ticket_id: int, responder_id: Optional[int] = None,
                      group_id: Optional[int] = None) -> dict:
        """Assign a ticket to an agent (responder_id from) and/or an agent group
        (group_id from list_groups). Pass at least one. Use for routing work to
        a specific technician or team."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if responder_id is None and group_id is None:
            raise ValueError("Provide responder_id and/or group_id.")
        body: dict = {}
        if responder_id is not None:
            body["responder_id"] = int(responder_id)
        if group_id is not None:
            body["group_id"] = int(group_id)
        updated = client.put_json(f"/tickets/{tid}", body)
        return {"ticket_id": tid, "assigned": True, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def add_time_entry(ticket_id: int, time_spent: str, note: Optional[str] = None,
                       billable: bool = True) -> dict:
        """Log billable/non-billable time worked on a ticket. time_spent is a
        duration string like '1h 30m', '45m', or '2h'. Useful for tracking L1/L2
        effort."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        body = {"time_spent": time_spent, "billable": billable}
        if note:
            body["note"] = note
        result = client.post_json(f"/tickets/{tid}/time_entries", body)
        return {"ticket_id": tid, "logged": True, "result": result}
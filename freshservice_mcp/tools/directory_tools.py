"""Directory lookups to support assignment, escalation and user resolution.

Search requester/contacts, list agents (technicians) and list agent groups so a
helper can route or escalate a ticket to the right person or team.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import get_client


def _contact_summary(c) -> dict:
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "active": c.get("active"),
        "job_title": c.get("job_title"),
        "department": (c.get("department") or {}).get("name") if isinstance(c.get("department"), dict) else c.get("department"),
    }


def _agent_summary(a) -> dict:
    contact = a.get("contact") or {}
    roles = a.get("roles") or []
    return {
        "id": a.get("id"),
        "name": a.get("name") or contact.get("name"),
        "email": a.get("email") or contact.get("email"),
        "group_ids": a.get("group_ids"),
        "role_ids": [r.get("id") for r in roles if isinstance(r, dict)],
        "role_names": [r.get("name") for r in roles if isinstance(r, dict)],
    }


def register(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    @mcp.tool()
    def search_contact(query: str = "", email: Optional[str] = None,
                       per_page: int = 20) -> dict:
        """Search requester/contacts by name (query) or email. Use this to
        resolve a requester's contact id before view_tickets_by_user or to
        confirm who a ticket belongs to."""
        client = get_client(config)
        if email:
            fq = f"email:{email.strip()}"
        elif query and query.strip():
            fq = f"name:{query.strip()}"
        else:
            fq = ""
        contacts = client.get_list(
            "/contacts",
            params={"query": fq} if fq else None,
            per_page=per_page,
            envelope_key="contacts",
        )
        return {"returned": len(contacts), "contacts": [_contact_summary(c) for c in contacts]}

    @mcp.tool()
    def list_agents(active: bool = True, per_page: int = 100) -> dict:
        """List FreshService agents (technicians). Useful for finding who to
        assign/escalate a ticket to."""
        client = get_client(config)
        agents = client.get_list(
            "/agents",
            params={"state": "active"} if active else None,
            per_page=per_page,
            envelope_key="agents",
        )
        return {"returned": len(agents), "agents": [_agent_summary(a) for a in agents]}

    @mcp.tool()
    def list_groups(per_page: int = 100) -> dict:
        """List agent groups (teams/queues). Useful for routing tickets via
        assign_ticket or categorize_ticket by fetching group ids."""
        client = get_client(config)
        groups = client.get_list(
            "/groups",
            per_page=per_page,
            envelope_key="groups",
        )
        return {
            "returned": len(groups),
            "groups": [{"id": g.get("id"), "name": g.get("name"), "description": g.get("description")} for g in groups],
        }
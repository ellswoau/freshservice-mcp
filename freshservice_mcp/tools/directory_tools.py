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
from ._common import find_requesters, summarize_requester


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
        """Search requesters/contacts by name (query) or email. Use this to
        resolve a requester's id before view_tickets_by_user or to confirm who
        a ticket belongs to. Matches against the /requesters directory."""
        client = get_client(config)
        if email and email.strip():
            matches = find_requesters(client, email=email)
        elif query and query.strip():
            matches = find_requesters(client, name=query.strip())
        else:
            all_reqs = client.get_list(
                "/requesters", per_page=per_page, envelope_key="requesters"
            )
            matches = [summarize_requester(r) for r in all_reqs]
        return {"returned": len(matches), "requesters": matches[:per_page]}

    @mcp.tool()
    def list_agents(active: Optional[bool] = None, per_page: int = 100) -> dict:
        """List FreshService agents (technicians). Useful for finding who to
        assign/escalate a ticket to. If ``active`` is True/False the result is
        filtered to that state after listing (the /agents API has no 'active'
        state filter)."""
        client = get_client(config)
        agents = client.get_list(
            "/agents",
            per_page=per_page,
            envelope_key="agents",
        )
        if active is not None:
            agents = [a for a in agents if bool(a.get("active")) == active]
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
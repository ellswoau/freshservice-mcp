"""Change module operations for IT change management.

Read FreshService **changes** (the Change module), which is a separate API
surface from tickets: ``GET /changes`` (paginated list) and
``GET /changes/{id}`` (single change). Changes carry change-specific fields
(change type, risk, approval status, planned window) that tickets do not.

Enum ids below are the account's ``/change_form_fields`` values (verified
against the live tenant); the per-call return also echoes the valid sets so a
caller can filter client-side. Note this API has **no** ``/changes/filter``
endpoint -- narrowing is done in the client, not the server.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import get_client


# Change-module enums (from GET /change_form_fields on this account).
CHANGE_STATUSES = {
    1: "Open", 2: "Planning", 3: "Awaiting Approval",
    4: "Pending Release", 5: "Pending Review", 6: "Closed",
}
CHANGE_TYPES = {1: "Minor", 2: "Standard", 3: "Major", 4: "Emergency"}
CHANGE_RISKS = {1: "Low", 2: "Medium", 3: "High", 4: "Very High"}
CHANGE_IMPACTS = {1: "Low", 2: "Medium", 3: "High"}
CHANGE_PRIORITIES = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}

# Change ids are integers; accept a bare id (276) for symmetry with tickets.
ChangeId = Union[int, str]


def _enum_name(mapping: Dict[int, str], value) -> Optional[str]:
    try:
        return mapping.get(int(value))
    except (TypeError, ValueError):
        return None


def _require_change_id(change_id) -> int:
    m = re.search(r"\d+", str(change_id).strip())
    if not m:
        raise ValueError("change_id must be a positive integer, e.g. 276.")
    cid = int(m.group(0))
    if cid <= 0:
        raise ValueError("change_id must be a positive integer.")
    return cid


def _attach_summary(attachments) -> Dict[str, Any]:
    items = attachments if isinstance(attachments, list) else []
    return {
        "attachment_count": len(items),
        "attachments": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "content_type": a.get("content_type"),
                "size": a.get("size"),
                "url": a.get("attachment_url") or a.get("url"),
            }
            for a in items if isinstance(a, dict)
        ],
    }


def summarize_change(c: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact, human-readable summary of a change dict."""
    assets = c.get("assets") if isinstance(c.get("assets"), list) else []
    services = c.get("impacted_services")
    if not isinstance(services, list):
        services = []
    return {
        "id": c.get("id"),
        "subject": c.get("subject"),
        "status": _enum_name(CHANGE_STATUSES, c.get("status")),
        "status_id": c.get("status"),
        "change_type": _enum_name(CHANGE_TYPES, c.get("change_type")),
        "change_type_id": c.get("change_type"),
        "risk": _enum_name(CHANGE_RISKS, c.get("risk")),
        "risk_id": c.get("risk"),
        "impact": _enum_name(CHANGE_IMPACTS, c.get("impact")),
        "impact_id": c.get("impact"),
        "priority": _enum_name(CHANGE_PRIORITIES, c.get("priority")),
        "priority_id": c.get("priority"),
        "approval_status": c.get("approval_status"),
        "category": c.get("category"),
        "sub_category": c.get("sub_category"),
        "item_category": c.get("item_category"),
        "department_id": c.get("department_id"),
        "workspace_id": c.get("workspace_id"),
        "group_id": c.get("group_id"),
        "agent_id": c.get("agent_id"),
        "requester_id": c.get("requester_id"),
        "planned_start_date": c.get("planned_start_date"),
        "planned_end_date": c.get("planned_end_date"),
        "maintenance_window": c.get("maintenance_window"),
        "blackout_window": c.get("blackout_window"),
        "impacted_services": services,
        "asset_count": len(assets),
        "tasks_dependency_type": c.get("tasks_dependency_type"),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "description_text": c.get("description_text"),
        **_attach_summary(c.get("attachments")),
    }


def compact_change(c: Dict[str, Any], desc_chars: int = 150) -> Dict[str, Any]:
    """The few fields a "what changed" read needs (keeps a week of changes ~3 KB)."""
    desc = (c.get("description_text") or "").strip().replace("\r", " ").replace("\n", " ")
    desc = re.sub(r"\s+", " ", desc)
    if len(desc) > desc_chars:
        desc = desc[:desc_chars].rstrip() + "..."
    return {
        "id": c.get("id"),
        "subject": (c.get("subject") or "").strip(),
        "status": _enum_name(CHANGE_STATUSES, c.get("status")),
        "change_type": _enum_name(CHANGE_TYPES, c.get("change_type")),
        "risk": _enum_name(CHANGE_RISKS, c.get("risk")),
        "planned_start_date": c.get("planned_start_date"),
        "planned_end_date": c.get("planned_end_date"),
        "updated_at": c.get("updated_at"),
        "description": desc,
    }


def _matches(c: Dict[str, Any], query: Optional[str]) -> bool:
    if not query:
        return True
    hay = " ".join(str(c.get(k) or "") for k in ("subject", "description_text")).lower()
    return all(term in hay for term in query.lower().split())


def register(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    @mcp.tool()
    def list_changes(per_page: int = 50, page: int = 1,
                     updated_since: Optional[str] = None,
                     order_by: str = "created_at",
                     order_type: str = "desc",
                     compact: bool = False,
                     query: Optional[str] = None) -> dict:
        """Return a paginated list of changes from the Change module (most
        recent first by default).

        This API has **no server-side filter endpoint** (``/changes/filter``
        does not exist), so use ``updated_since`` (ISO-8601, e.g.
        '2026-09-01T00:00:00Z') plus paging and filter the returned fields
        client-side. ``change_type``, ``status``, ``risk``, ``impact`` and
        ``priority`` come back as ids; the ``valid_values`` block maps them to
        names. Set order_by to 'created_at' or 'updated_at'; order_type to 'asc'
        or 'desc'. Per page is capped at 100.

        For a "what changed recently" read pass ``compact=True`` (id, subject,
        status, type, risk, planned window, updated_at and the first 150
        characters of the description -- about 3 KB for a week instead of ~40 KB)
        and optionally ``query``: space-separated words that must ALL appear in the
        subject or description (case-insensitive), e.g. ``"south bend"`` or
        ``"horizon"``. The filter is applied to the fetched page, so pair it with
        ``updated_since`` and ``per_page=100``."""
        client = get_client(config)
        params: Dict[str, Any] = {"order_by": order_by, "order_type": order_type}
        if updated_since:
            params["updated_since"] = updated_since
        changes = client.get_list(
            "/changes",
            params=params,
            per_page=per_page,
            page=page,
            envelope_key="changes",
        )
        matched = [c for c in changes if _matches(c, query)]
        if compact:
            return {
                "page": page,
                "fetched": len(changes),
                "returned": len(matched),
                "updated_since": updated_since,
                "query": query,
                "changes": [compact_change(c) for c in matched],
            }
        return {
            "page": page,
            "fetched": len(changes),
            "returned": len(matched),
            "updated_since": updated_since,
            "query": query,
            "changes": [summarize_change(c) for c in matched],
            "valid_values": {
                "status": CHANGE_STATUSES,
                "change_type": CHANGE_TYPES,
                "risk": CHANGE_RISKS,
                "impact": CHANGE_IMPACTS,
                "priority": CHANGE_PRIORITIES,
            },
        }

    @mcp.tool()
    def view_change(change_id: ChangeId, include_stats: bool = False) -> dict:
        """View a single change by id from the Change module.

        Returns the change's fields (type, status, risk, impact, approval
        status, planned window, impacted services) and its description. Set
        ``include_stats`` to also pull the change's ``stats`` include. Note the
        Change API accepts only ``stats`` as an ``include`` (no conversations),
        so unlike tickets a change has no attached conversation thread."""
        client = get_client(config)
        cid = _require_change_id(change_id)
        params = {"include": "stats"} if include_stats else None
        change = client.get_one(f"/changes/{cid}", params=params, key="change")
        result = summarize_change(change)
        if include_stats:
            result["stats"] = change.get("stats")
        return result

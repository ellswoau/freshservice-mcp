"""Ticket operations for L1/L2 helpdesk work.

View, filter, and manage FreshService tickets: list recent, filter by query,
view a single ticket, create/update, change status/priority, categorize
(ticket type), assign, look up tickets by a requester, and log time.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from ..client import FreshServiceClient, get_client
from ._common import (
    TicketId,
    current_agent_id,
    find_requesters,
    status_name,
    summarize_conversation,
    summarize_ticket,
    today_start_iso,
)


# --------------------------------------------------------------- closure helpers
# FreshService refuses to resolve/close a ticket until every field flagged
# ``required_for_closure`` in /ticket_form_fields is populated. These helpers
# fetch that schema, resolve human-friendly names to the ids/values the API
# wants, validate before sending, and report exactly what is still missing --
# so a ticket can be classified and resolved on the first try.
CLOSURE_REQUIRED_FALLBACK = (
    "workspace_id", "subject", "status", "urgency", "priority",
    "category", "group", "agent", "msf_store", "resolution",
)

# Form-field name -> ticket attribute it populates when reading the ticket.
_FIELD_TO_ATTR = {
    "workspace_id": "workspace_id",
    "subject": "subject",
    "status": "status",
    "urgency": "urgency",
    "priority": "priority",
    "category": "category",
    "group": "group_id",
    "agent": "responder_id",
    "department": "department_id",
    "msf_store": "msf_store",
    "resolution": "resolution",
}
_CUSTOM_FIELD_NAMES = {"msf_store", "resolution"}


def form_fields(client) -> List[dict]:
    """Return the account's ticket form fields (with choices) from
    ``GET /ticket_form_fields``."""
    data = client.get_json("/ticket_form_fields")
    fields = data.get("ticket_fields") if isinstance(data, dict) else None
    return fields if isinstance(fields, list) else []


def _field_by_name(fields: List[dict], name: str) -> Optional[dict]:
    for f in fields:
        if f.get("name") == name:
            return f
    return None


def _choices(field) -> List[dict]:
    return [c for c in (field or {}).get("choices") or [] if isinstance(c, dict)]


def _valid_values(field) -> List[str]:
    return [str(c.get("value")) for c in _choices(field)]


def _find_option(options, value):
    target = str(value).strip().lower()
    for o in options or []:
        if str(o.get("value")).strip().lower() == target:
            return o
    return None


def _choice_id(field, value, label: str) -> int:
    """Resolve a choice name (or a numeric id) to the FreshService id."""
    opt = _find_option(_choices(field), value)
    if opt is not None:
        return opt.get("id")
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    raise ValueError(
        f"{label} {value!r} is not a valid value. Valid values: "
        + ", ".join(_valid_values(field))
    )


def _validate_category(fields, category, sub_category=None, item_category=None) -> None:
    """Validate a category / sub-category / item-category path against the
    account's category tree, raising a helpful error listing valid options."""
    cat = _field_by_name(fields, "category")
    parent = _find_option(_choices(cat), category)
    if parent is None:
        raise ValueError(
            f"category {category!r} is not valid. Valid values: "
            + ", ".join(_valid_values(cat))
        )
    sub = None
    if sub_category:
        sub = _find_option(parent.get("nested_options"), sub_category)
        if sub is None:
            valid = ", ".join(str(o.get("value")) for o in parent.get("nested_options") or []) or "(none)"
            raise ValueError(
                f"sub_category {sub_category!r} is not valid for category "
                f"{category!r}. Valid sub-categories: {valid}"
            )
    if item_category:
        options = (sub or {}).get("nested_options") or []
        if _find_option(options, item_category) is None:
            valid = ", ".join(str(o.get("value")) for o in options) or "(none)"
            raise ValueError(
                f"item_category {item_category!r} is not valid for "
                f"{category!r}/{sub_category!r}. Valid items: {valid}"
            )


def _resolve_agent_id(client, fields, agent) -> int:
    """Resolve an agent from a numeric id, a form-field display name, or an
    email / partial name via the /agents directory."""
    raw = str(agent).strip()
    if raw.isdigit():
        return int(raw)
    opt = _find_option(_choices(_field_by_name(fields, "agent")), raw)
    if opt is not None:
        return opt.get("id")
    low = raw.lower()
    try:
        agents = client.get_list("/agents", per_page=100, envelope_key="agents")
    except Exception:  # noqa: BLE001 - fall through to a clear error
        agents = []
    for a in agents:
        email = str(a.get("email") or "").lower()
        name = str(a.get("name") or (a.get("contact") or {}).get("name") or "").lower()
        if low == email or (name and low in name):
            return a.get("id")
    raise ValueError(
        f"agent {agent!r} could not be resolved. Pass an agent id (see "
        "list_agents) or an exact display name."
    )


def _classification_body(client, config, fields, *, category=None, sub_category=None,
                         item_category=None, department=None, store=None, group=None,
                         agent=None, impact=None, urgency=None, priority=None,
                         workspace=None, extra_custom_fields=None) -> dict:
    """Build the PUT body for the closure/classification fields, validating each
    value and falling back to the configured defaults for group/agent/workspace."""
    body: dict = {}
    custom: dict = {}

    if category is not None:
        _validate_category(fields, category, sub_category, item_category)
        body["category"] = category
        if sub_category is not None:
            body["sub_category"] = sub_category
        if item_category is not None:
            body["item_category"] = item_category

    if department is not None:
        raw = str(department).strip()
        body["department_id"] = (int(raw) if raw.isdigit()
                                 else _choice_id(_field_by_name(fields, "department"), department, "department"))

    if store is not None:
        values = [store] if isinstance(store, str) else list(store)
        store_field = _field_by_name(fields, "msf_store")
        for v in values:
            if not str(v).strip().isdigit() and _find_option(_choices(store_field), v) is None:
                raise ValueError(
                    f"store {v!r} is not valid. Valid values: "
                    + ", ".join(_valid_values(store_field))
                )
        custom["msf_store"] = values

    if group is not None:
        body["group_id"] = _choice_id(_field_by_name(fields, "group"), group, "group")
    elif config.default_group_id:
        body["group_id"] = config.default_group_id
    elif config.default_group_name:
        body["group_id"] = _choice_id(_field_by_name(fields, "group"), config.default_group_name, "group")

    if agent is not None:
        body["responder_id"] = _resolve_agent_id(client, fields, agent)
    elif config.default_agent_id:
        body["responder_id"] = config.default_agent_id
    elif config.default_agent_email:
        body["responder_id"] = _resolve_agent_id(client, fields, config.default_agent_email)

    if impact is not None:
        body["impact"] = _choice_id(_field_by_name(fields, "impact"), impact, "impact")
    if urgency is not None:
        body["urgency"] = _choice_id(_field_by_name(fields, "urgency"), urgency, "urgency")
    if priority is not None:
        body["priority"] = _choice_id(_field_by_name(fields, "priority"), priority, "priority")

    if workspace is not None:
        body["workspace_id"] = _choice_id(_field_by_name(fields, "workspace_id"), workspace, "workspace")
    elif config.default_workspace_id:
        body["workspace_id"] = config.default_workspace_id

    if extra_custom_fields:
        custom.update(extra_custom_fields)
    if custom:
        body["custom_fields"] = custom
    return body


def _missing_closure_fields(ticket: dict, fields: List[dict]) -> List[str]:
    """Return the names of closure-required form fields that are still empty on
    the given (prospective) ticket."""
    required = [f.get("name") for f in fields if f.get("required_for_closure")]
    if not required:
        required = list(CLOSURE_REQUIRED_FALLBACK)
    missing = []
    custom = ticket.get("custom_fields") if isinstance(ticket.get("custom_fields"), dict) else {}
    for name in required:
        val = custom.get(name) if name in _CUSTOM_FIELD_NAMES else ticket.get(_FIELD_TO_ATTR.get(name, name))
        if val in (None, "", [], {}):
            missing.append(name)
    return missing


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
            page=page,
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
        # FreshService requires filter queries to be wrapped in double quotes.
        tickets = client.get_list(
            "/tickets/filter",
            params={"query": f'"{query}"'},
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
            params={"query": f'"status:{status_map[key]}"'},
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
            params={"query": f'"{query}"'},
            per_page=per_page,
            envelope_key="tickets",
        )
        return {
            "opened_after": today_start_iso(),
            "returned": len(tickets),
            "tickets": [summarize_ticket(t) for t in tickets],
        }

    @mcp.tool()
    def view_ticket(ticket_id: TicketId, include_conversations: bool = False) -> dict:
        """View a single ticket by id. Set include_conversations=True to also
        return the ticket's replies & private notes (inline via the
        ``conversations`` include, valid on this account). Returns full ticket
        attributes, requester/responder/group details and timestamps."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        # Only valid `include` values for this account are used (e.g.
        # ``requester,stats,conversations``); 'responder'/'company' are rejected.
        base = "requester,stats"
        if include_conversations:
            base = f"{base},conversations"
        ticket = client.get_one(f"/tickets/{tid}", params={"include": base}, key="ticket")
        result = summarize_ticket(ticket)
        if include_conversations:
            convs = ticket.get("conversations")
            # Some accounts return them inline; otherwise fall back to the list
            # endpoint (read-only, always available).
            if not isinstance(convs, list):
                try:
                    data = client.get_json(f"/tickets/{tid}/conversations")
                    convs = data.get("conversations") if isinstance(data, dict) else data
                except Exception:
                    convs = []
            result["conversations"] = (
                [summarize_conversation(c) for c in convs] if isinstance(convs, list) else []
            )
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
            # Resolve the requester id from email via the /requesters directory.
            reqs = find_requesters(client, email=email.strip())
            if not reqs:
                return {"error": f"No requester found for email {email}.", "tickets": []}
            requester_id = reqs[0]["id"]
            if email is None:
                email = reqs[0].get("email")
        else:
            requester_id = int(requester_id)
        if email is None:
            email = None

        tickets = client.get_list(
            "/tickets/filter",
            params={"query": f'"requester_id:{requester_id}"'},
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
        Medium=2/High=3/Urgent=4. ticket_type e.g. Incident / Request / Change.
        status defaults to Open (2) and priority to Low (1) if omitted."""
        # ``ticket_type`` values are account-defined; common ones are
        # Incident / Service Request / Major Incident (the API enforces them).
        client = get_client(config)
        # FreshService enforces status & priority on create; default to Open/Low
        # when the caller omits them.
        body: dict = {"subject": subject, "description": description,
                      "priority": priority if priority is not None else 1,
                      "status": status if status is not None else 2,
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
    def update_ticket(ticket_id: TicketId, updates: dict) -> dict:
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
    def set_ticket_status(ticket_id: TicketId, status: str, resolution: Optional[str] = None) -> dict:
        """Change a ticket's status. Accepts a name ('open', 'pending',
        'resolved', 'closed') or a numeric id (2/3/4/5). On accounts that accept
        a free-text resolution you may pass it via `resolution`; other accounts
        use custom workflows, in which case only the status id is sent and the
        API surfaces any additional requirements. Returns the updated ticket."""
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
        body: dict = {"status": status_id}
        if resolution and resolution.strip():
            # The account stores the resolution text in the custom field
            # ``resolution`` (a top-level ``resolution`` key is rejected with
            # "Unexpected/invalid field"). Setting status=Resolved also
            # requires every ``required_for_closure`` field to be populated --
            # prefer ``resolve_ticket`` for a one-shot resolve.
            body["custom_fields"] = {"resolution": resolution.strip()}
        updated = client.put_json(f"/tickets/{tid}", body)
        return {"ticket_id": tid, "status": status, "ticket": summarize_ticket(updated.get("ticket") or {})}

    @mcp.tool()
    def set_ticket_priority(ticket_id: TicketId, priority: str) -> dict:
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
    def categorize_ticket(ticket_id: TicketId, ticket_type: str,
                          group_id: Optional[int] = None,
                          priority: Optional[str] = None) -> dict:
        """Categorize/classify a ticket by setting its type (account-defined;
        commonly 'Incident', 'Service Request' or 'Major Incident'), optionally
        moving it to a group and/or setting its priority. group_id comes from
        list_groups."""
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
    def assign_ticket(ticket_id: TicketId, responder_id: Optional[int] = None,
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
    def add_time_entry(ticket_id: TicketId, time_spent: str, note: Optional[str] = None,
                       billable: bool = True, agent_id: Optional[int] = None) -> dict:
        """Log billable/non-billable time worked on a ticket. time_spent must be
        in 'hh:mm' form (e.g. '00:15', '01:30'). The authenticated agent is used
        as the time-entry owner unless agent_id is given."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        ts = (time_spent or "").strip()
        if not ts:
            raise ValueError("time_spent is required in 'hh:mm' format (e.g. '00:15').")
        # Convenience: accept '45m' / '1h30m' style and normalise to hh:mm.
        if ":" not in ts:
            m = re.match(r"^(?:(\d+)h)?\s*(?:(\d+)m)?$", ts, re.IGNORECASE)
            if m:
                h = int(m.group(1) or 0)
                mm = int(m.group(2) or 0)
                ts = f"{h:02d}:{mm:02d}"
            else:
                raise ValueError("time_spent must be 'hh:mm' (e.g. '00:15') or '45m'/'1h30m'.")
        if agent_id is None:
            agent_id = current_agent_id(client)
        body = {"time_spent": ts, "billable": billable, "agent_id": agent_id}
        if note:
            body["note"] = note
        result = client.post_json(f"/tickets/{tid}/time_entries", body)
        return {"ticket_id": tid, "logged": True, "time_spent": ts, "result": result}

    # ------------------------------------------------ classification / closure
    @mcp.tool()
    def list_ticket_fields(required_only: bool = False) -> dict:
        """List the account's ticket form fields and their allowed values.

        Use this to discover the valid `category` (and its nested
        sub-categories / item categories), `group`, `agent`, `msf_store` (Store)
        and `department` values, and which fields FreshService requires before a
        ticket can be resolved (`required_for_closure`). Set
        ``required_only=True`` to return just the closure-required fields. Doing
        this classification FIRST is what lets `resolve_ticket` succeed on the
        first try."""
        client = get_client(config)
        fields = form_fields(client)
        out = []
        for f in fields:
            if required_only and not f.get("required_for_closure"):
                continue
            entry = {
                "name": f.get("name"),
                "label": f.get("label"),
                "field_type": f.get("field_type"),
                "required_for_closure": bool(f.get("required_for_closure")),
            }
            ch = _choices(f)
            if ch:
                entry["choices"] = [
                    {**{"id": c.get("id"), "value": c.get("value")},
                     **({"sub_categories": [o.get("value") for o in c.get("nested_options") or []]}
                        if c.get("nested_options") else {})}
                    for c in ch
                ]
            out.append(entry)
        return {
            "count": len(out),
            "required_for_closure": [f.get("name") for f in fields
                                     if f.get("required_for_closure")] or list(CLOSURE_REQUIRED_FALLBACK),
            "fields": out,
        }

    @mcp.tool()
    def list_departments(per_page: int = 100) -> dict:
        """List FreshService departments (id + name) so a ticket's Department can
        be set to the department the requester/end user works in."""
        client = get_client(config)
        deps = client.get_list("/departments", per_page=per_page, envelope_key="departments")
        return {
            "returned": len(deps),
            "departments": [{"id": d.get("id"), "name": d.get("name")} for d in deps],
        }

    @mcp.tool()
    def classify_ticket(ticket_id: TicketId,
                        category: Optional[str] = None,
                        sub_category: Optional[str] = None,
                        item_category: Optional[str] = None,
                        department: Optional[str] = None,
                        store: Optional[List[str]] = None,
                        group: Optional[str] = None,
                        agent: Optional[str] = None,
                        impact: Optional[str] = None,
                        urgency: Optional[str] = None,
                        priority: Optional[str] = None,
                        workspace: Optional[str] = None,
                        custom_fields: Optional[dict] = None) -> dict:
        """Classify a ticket by setting the fields FreshService requires before it
        can be resolved -- do this FIRST, then resolve.

        Arguments (each validated against the account's form fields):
          - category / sub_category / item_category: the issue classification
            tree, e.g. category='User Account', sub_category='Reset Password'.
          - department: the department the end user works in (name or id).
          - store: one or more Store values, e.g. ['8-Atlanta'].
          - group: agent group (name or id); defaults to the configured group.
          - agent: assignee (id, exact display name, or email); defaults to the
            configured agent.
          - impact / urgency: 'low'|'medium'|'high' (or the numeric id).
          - priority: 'low'|'medium'|'high'|'urgent' (or the numeric id).
          - workspace: workspace name or id; defaults to the configured one.

        Returns the updated ticket plus ``missing_for_closure`` (any
        closure-required field still empty). Use list_ticket_fields first if you
        are unsure of the valid values."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        fields = form_fields(client)
        body = _classification_body(
            client, config, fields,
            category=category, sub_category=sub_category, item_category=item_category,
            department=department, store=store, group=group, agent=agent,
            impact=impact, urgency=urgency, priority=priority, workspace=workspace,
            extra_custom_fields=custom_fields,
        )
        if not body:
            raise ValueError("Provide at least one field to classify (category, department, store, group, agent, impact, urgency, priority, workspace).")
        updated = client.put_json(f"/tickets/{tid}", body)
        verify = client.get_one(f"/tickets/{tid}", key="ticket") or {}
        return {
            "ticket_id": tid,
            "classified": True,
            "missing_for_closure": _missing_closure_fields(verify, fields),
            "ticket": summarize_ticket(verify),
        }

    @mcp.tool()
    def resolve_ticket(ticket_id: TicketId, resolution: str,
                       category: Optional[str] = None,
                       sub_category: Optional[str] = None,
                       item_category: Optional[str] = None,
                       department: Optional[str] = None,
                       store: Optional[List[str]] = None,
                       group: Optional[str] = None,
                       agent: Optional[str] = None,
                       impact: Optional[str] = None,
                       urgency: Optional[str] = None,
                       priority: Optional[str] = None,
                       workspace: Optional[str] = None,
                       custom_fields: Optional[dict] = None,
                       force: bool = False) -> dict:
        """Resolve a ticket in ONE call: set every closure-required field
        (classification, store, resolution note) and flip the status to Resolved.

        Pass ``resolution`` (the resolution note text) and the classification
        fields you learned (category/sub_category, department, store, group,
        agent, impact, urgency, priority) -- or rely on the configured defaults
        for group/agent/workspace. Do the classification with `classify_ticket`
        first if you prefer, since these fields must be set before a ticket can
        close.

        Safety: before writing anything this reads the ticket + form schema and,
        if a closure-required field would still be empty, raises an error naming
        exactly which fields to set (no partial change). Set ``force=True`` to
        attempt the resolve anyway. Verifies the result by re-reading the ticket
        and returns ``resolved`` plus any ``missing_for_closure``."""
        client = get_client(config)
        tid = _require_ticket_id(ticket_id)
        if not resolution or not resolution.strip():
            raise ValueError("resolution is required (the resolution note text).")
        fields = form_fields(client)
        body = _classification_body(
            client, config, fields,
            category=category, sub_category=sub_category, item_category=item_category,
            department=department, store=store, group=group, agent=agent,
            impact=impact, urgency=urgency, priority=priority, workspace=workspace,
            extra_custom_fields=custom_fields,
        )
        current = client.get_one(f"/tickets/{tid}", key="ticket") or {}
        prospective = dict(current)
        for k, v in body.items():
            if k == "custom_fields":
                merged = dict(prospective.get("custom_fields") or {})
                merged.update(v)
                prospective["custom_fields"] = merged
            else:
                prospective[k] = v
        prospective.setdefault("custom_fields", {})["resolution"] = resolution.strip()
        prospective["status"] = 4
        missing = _missing_closure_fields(prospective, fields)
        if missing and not force:
            raise ValueError(
                "Ticket cannot be resolved yet; these closure-required fields "
                "would still be empty: " + ", ".join(missing)
                + ". Set them with classify_ticket (or pass them to resolve_ticket) "
                "and retry. Call list_ticket_fields to see valid values."
            )
        body["status"] = 4
        cf = dict(body.get("custom_fields") or {})
        cf["resolution"] = resolution.strip()
        body["custom_fields"] = cf
        client.put_json(f"/tickets/{tid}", body)
        verify = client.get_one(f"/tickets/{tid}", key="ticket") or {}
        resolved = int(verify.get("status") or 0) == 4
        return {
            "ticket_id": tid,
            "resolved": resolved,
            "status": status_name(verify.get("status")),
            "missing_for_closure": _missing_closure_fields(verify, fields),
            "ticket": summarize_ticket(verify),
        }
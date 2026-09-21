# FreshService MCP Server (Python / FastMCP)

A [Model Context Protocol](https://modelcontextprotocol.io) server exposing
helpdesk / Service Desk operations for a **FreshService** account, built with
Python [FastMCP](https://github.com/jlowin/fastmcp). It targets the FreshService
(v2) REST API documented at [https://api.freshservice.com](https://api.freshservice.com).

> Runs both as a direct `python -m` process **and** as a Docker container
> (see "Running via Docker" below).

## Skills / features (L1 & L2 helpdesk)

**View & filter tickets**
- `list_tickets` / `list_recent_tickets` — paginated ticket list; "latest 100"
- `filter_tickets` — powerful query-language filtering (status, priority,
  group, requester, created_at range, unassigned `agent_id:null`, …)
- `list_tickets_by_status` — open / pending / resolved / closed
- `list_tickets_opened_today` — all tickets opened today
- `view_ticket` — full ticket attributes (+ optional conversations)
- `view_tickets_by_user` — all tickets for a requester with dates, subjects,
  statuses

**Talk to people**
- `reply_to_requestor` — public reply to the requester (conversation)
- `add_private_note` — internal note (private conversation), not visible to requester
- `cc_email_on_ticket` — CC a manager / extra recipients on the ticket
- `notify_emails` — email-notify IT team members / escalation contacts now
- `list_ticket_conversations` — full conversation history (public/private marked)

**Manage tickets**
- `create_ticket` / `update_ticket`
- `set_ticket_status` — change status (open/pending/resolved/closed)
- `set_ticket_priority` — low/medium/high/urgent
- `categorize_ticket` — set ticket type (Incident / Request / Change, …) and group
- `assign_ticket` — route to an agent and/or group
- `add_time_entry` — log billable time worked

**Directory / routing**
- `search_contact` — find a requester by name or email
- `list_agents` / `list_groups` — who's available to assign/escalate to

> **Merge tickets:** FreshService has **no** ticket-merge API. If related tickets
> need linking, use child tickets (`POST /api/v2/tickets/{parent}/create_child_ticket`) or
> reference them in a private note. A merge tool was intentionally left out per
> project scope.

**Status / priority values** (from the API reference): Status — Open=2,
Pending=3, Resolved=4, Closed=5. Priority — Low=1, Medium=2, High=3, Urgent=4.
Source — Email=1, Portal=2, Phone=3, Chat=4, Feedback widget=5, … Slack=10.

## Requirements & install

- Python 3.9+ (tested on 3.12)
- `pip install -r requirements.txt`

Credentials are never hard-coded. Provide them via a config JSON file or
environment variables (precedence: explicit args > env > file).

## Configure

```bash
cd freshservice-mcp
python -m freshservice_mcp config init --config ./freshservice.json
```

This prompts for the FreshService subdomain and API key (key entered without
echoing) and writes the file with `0600` owner-only permissions. Example result:

```json
{
  "domain": "yourcompany",
  "api_key": "…",
  "verify_ssl": true,
  "timeout": 30
}
```

> Let the account's **admin > Settings > API key** provide the key. This client
> sends it as the HTTP **Basic** auth *username* (`curl -u <api_key>:X`, i.e.
> `Authorization: Basic base64("<api_key>:")`), which is the form this tenant
> accepts (HTTP 200). Note: some docs show a literal `api_key` *username* with
> the real key as the password (`-u api_key:<key>`); that form returns 403
> `access_denied` on this account, which is the exact 403 this project hit
> before the fix.

## Run (stdio / embedded client)

```bash
export FRESHSERVICE_DOMAIN="yourcompany"
export FRESHSERVICE_API_KEY="***"
export FRESHSERVICE_VERIFY_SSL="true"   # default true
export FRESHSERVICE_TIMEOUT="30"
# optional, to point at a config file:
export FRESHSERVICE_CONFIG_FILE="./freshservice.json"

# test connectivity first:
python -m freshservice_mcp ping --config ./freshservice.json

# stdio transport (used by Claude Desktop / MCP clients)
python -m freshservice_mcp --config ./freshservice.json
```

A `.env` file is also honored if `python-dotenv` is installed and loaded (see
`.env.example`). See `freshservice_mcp/config.py` for all `FRESHSERVICE_*` vars.

### MCP client config (stdio)

```json
{
  "mcpServers": {
    "freshservice": {
      "command": "python",
      "args": ["-m", "freshservice_mcp"],
      "env": {
        "FRESHSERVICE_CONFIG_FILE": "/abs/path/to/freshservice.json"
      }
    }
  }
}
```

## Run as a Docker container

The project ships a `Dockerfile`, `docker-compose.yml` and `.dockerignore`. It
runs as a non-root user over stdio by default and can also run as a long-lived
HTTP/SSE daemon.

```bash
docker build -t freshservice-mcp .
```

Create your config file with `config init`, then run the container
interactively so stdio stays attached (pass credentials via env or a mounted
config file):

```bash
docker run -i --rm \
  -e FRESHSERVICE_CONFIG_FILE=/config/freshservice.json \
  -v "$(pwd)/freshservice.json:/config/freshservice.json:ro" \
  freshservice-mcp
```

Point the MCP client at the container, e.g. Claude Desktop:

```json
{
  "mcpServers": {
    "freshservice": {
      "command": "docker",
      "args": ["run", "-i", "--rm",
               "-e", "FRESHSERVICE_CONFIG_FILE=/config/freshservice.json",
               "-v", "/abs/path/freshservice.json:/config/freshservice.json:ro",
               "freshservice-mcp"]
    }
  }
}
```

Or pass secrets via `-e` instead of mounting a file:

```bash
docker run -i --rm \
  -e FRESHSERVICE_DOMAIN=yourcompany \
  -e FRESHSERVICE_API_KEY='***' \
  freshservice-mcp
```

**Network daemon mode** (run as long-lived HTTP/SSE service, gated behind an API key):

```bash
docker run -d --name freshservice-mcp -p 8000:8000 \
  -e FRESHSERVICE_CONFIG_FILE=/config/freshservice.json \
  -e FRESHSERVICE_MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -v "$(pwd)/freshservice.json:/config/freshservice.json:ro" \
  freshservice-mcp --transport http --host 0.0.0.0 --port 8000
```

or with Compose (bundled):

```bash
docker compose up -d --build
```

Then connect an MCP client that supports HTTP/SSE to `http://<host>:8000/mcp`.
Available transports: `stdio` (default), `sse`, `streamable-http`, `http`.

```bash
curl -i http://localhost:8000/health   # HTTP 200 + JSON status (no auth)
curl -i http://localhost:8000/mcp      # expect 401 without the API key
```

### Gating the network transport with an API key

Set `FRESHSERVICE_MCP_AUTH_TOKEN` (or pass `--token`). Every endpoint except
`/health` and `/healthz` then requires:

```bash
curl -H "Authorization: Bearer $FRESHSERVICE_MCP_AUTH_TOKEN" http://host:8000/mcp
```

- When `FRESHSERVICE_MCP_AUTH_TOKEN` is unset/empty, auth is disabled (open),
  preserving default behaviour. The bundled `docker-compose.yml` requires the
  variable so daemon deployments are secured by default.
- If the key is compromised, rotate it and redeploy — the running process does
  not cache it across restarts.
- `/health` stays public so load balancers / uptime monitors can probe liveness
  without a secret.
- `docker compose` file-permission note: the container runs as an unprivileged
  user, so a mounted config file must be world/group-readable. Prefer env vars,
  or `chown` the file to the container UID, if you hit `Permission denied`.

## Project layout

```
freshservice-mcp/
├── README.md
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── freshservice_mcp/
    ├── __init__.py
    ├── __main__.py        # python -m entrypoint
    ├── server.py          # FastMCP app + CLI (config init / ping / run)
    ├── config.py          # secure config & credential resolution
    ├── client.py          # FreshService v2 REST client (auth, pagination, errors)
    └── tools/
        ├── __init__.py    # registers all tool modules
        ├── _common.py     # status/priority maps + summaries
        ├── ticket_tools.py
        ├── conversation_tools.py
        └── directory_tools.py
```

## Notes on the FreshService API handled here

Operational details verified against the live Weller Truck Parts tenant:

- Base URL is `https://<domain>.freshservice.com/api/v2`.
- Auth is a single API key sent as HTTP Basic auth **with the key as the
  username** (`curl -u <api_key>:X`); the password field is ignored/empty. The
  key is never logged. HTTP 429 (rate limit) responses trigger a short
  backoff retry honoring `Retry-After`.
- List endpoints use `page` / `per_page` and return an envelope
  (`{"tickets": [...]}`, `{"conversations": [...]}`, `{"requesters": [...]}`,
  `{"agents": [...]}`), unwrapped automatically. `get_list` returns a **single
  page** by default; it does not auto-cascade pages (which would blow straight
  through the API rate limit).
- **Filter queries must be wrapped in double quotes** (`query="status:2"`),
  and `created_at` comparisons use date-only values (`YYYY-MM-DD`). This is
  exactly how `filter_tickets`, `list_tickets_by_status`,
  `list_tickets_opened_today` and `view_tickets_by_user` are implemented.
- Requester/contact directory is **`/api/v2/requesters`**, not `/contacts`
  (which returns 404 on this tenant). Email/name lookup scans a bounded number
  of requester pages client-side because the requester filter query does not
  support email.
- **Conversations** — list history with `GET /api/v2/tickets/{id}/conversations`,
  **reply to the requester** with `POST /api/v2/tickets/{id}/reply` (JSON body), and
  **add a private note** with `POST /api/v2/tickets/{id}/notes` (multipart
  `body` + `private=true`). These are separate endpoints, not
  `/conversations/reply` / `/conversations/note` (which return 404).
- `view_ticket(include_conversations=True)` uses `?include=requester,stats,conversations`
  (only valid `include` values on this account; `responder`/`company` are rejected),
  falling back to the conversations list endpoint if not returned inline.
- **Ticket `type` is account-defined**; common values are `Incident`,
  `Service Request` or `Major Incident` (the API enforces valid values).
  Custom status/priority ids beyond the standard 2/3/4/5 are possible on this tenant;
  `create_ticket` defaults to `status=Open (2)` and `priority=Low (1)` since the
  API requires them on create.
- `add_time_entry` requires `time_spent` in `hh:mm` (e.g. `00:15`; `45m` /
  `1h30m` are accepted and normalised) and the authenticated agent id is used
  automatically.
- `cc_email_on_ticket` / `notify_emails` add to `cc_emails` (there is no
  dedicated notify endpoint); `reply_cc_emails` is NOT writable via the ticket
  update.
- Mutating calls return the updated resource, which is summarised for the
  assistant instead of dumping raw JSON.
- All requests honour `verify_ssl` (on by default; disable only for self-signed
  proxies).
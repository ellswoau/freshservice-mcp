"""Thin REST client for the FreshService (Service Desk) v2 API.

Authentication uses a FreshService API key sent as an ``Authorization: Bearer
<api_key>`` header (FreshService accepts the API key as a bearer token). The
key itself is never logged.

API conventions (FreshService v2):
  * Base path is ``/api/v2`` against ``https://<domain>.freshservice.com``.
  * List endpoints accept ``page`` / ``per_page`` query params.
  * Filtering uses a powerful query string language against
    ``/api/v2/tickets/filter?query="..."``.
  * Mutating calls return 200 with the updated resource envelope.

See https://api.freshservice.com for the full reference.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

import requests

from .config import FreshServiceConfig


class FreshServiceError(Exception):
    """Raised for FreshService API errors, carrying status + parsed detail."""

    def __init__(self, status: Optional[int], message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - helper
        return f"FreshServiceError(status={self.status}, message={self.message!r})"


class FreshServiceClient:
    """Stateful client bound to one FreshService account config."""

    API_PREFIX = "/api/v2"
    DEFAULT_PAGE_SIZE = 50
    MAX_PAGE_SIZE = 100

    def __init__(self, config: FreshServiceConfig):
        self.config = config
        self.base_url = config.resolved_base_url()
        self.verify_ssl = config.verify_ssl
        self.timeout = config.timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------ request core
    @staticmethod
    def _error_detail(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return resp.text[:500]

    def _raise_for(self, resp: requests.Response, url: str) -> None:
        if resp.status_code < 200 or resp.status_code >= 300:
            detail = self._error_detail(resp)
            msg = (
                f"FreshService API {resp.status_code} for {url}: "
                f"{detail if not isinstance(detail, dict) else json.dumps(detail)}"
            )
            raise FreshServiceError(resp.status_code, msg, detail)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
    ) -> requests.Response:
        url = f"{self.base_url}{self.API_PREFIX}{path}"
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        self._raise_for(resp, url)
        return resp

    def get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("GET", path, params=params)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def post_json(self, path: str, json_body: Any = None, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("POST", path, params=params, json_body=json_body)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def put_json(self, path: str, json_body: Any = None, *, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("PUT", path, params=params, json_body=json_body)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------- envelope
    @staticmethod
    def _unwrap(data: Any, key: str) -> List[Any]:
        """Return the ``key`` list from a paginated envelope, or the raw data."""
        if isinstance(data, dict):
            if key in data and isinstance(data[key], list):
                return data[key]
            # Some endpoints wrap as {key: {...}} for a single resource.
            if key in data and isinstance(data[key], dict):
                return [data[key]]
            return []
        if isinstance(data, list):
            return data
        return [data] if data else []

    # ------------------------------------------------------------- pagination
    def get_list(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        per_page: int = DEFAULT_PAGE_SIZE,
        max_items: int = 1000,
        envelope_key: str = "tickets",
    ) -> List[Any]:
        """Fetch pages of a paginated list endpoint until exhausted or a cap.

        FreshService list endpoints return ``{"<envelope_key>": [...], "meta":
        {...}}`` and accept ``page`` + ``per_page``. ``max_items`` guards
        against unbounded pulls.
        """
        per_page = max(1, min(int(per_page), self.MAX_PAGE_SIZE))
        collected: List[Any] = []
        page = 1
        p = dict(params or {})
        while True:
            p["per_page"] = per_page
            p["page"] = page
            batch = self.get_json(path, params=p)
            items = self._unwrap(batch, envelope_key)
            if not items:
                break
            collected.extend(items)
            if len(items) < per_page or len(collected) >= max_items:
                break
            page += 1
        return collected[:max_items]

    def get_one(self, path: str, *, params: Optional[Dict[str, Any]] = None, key: str) -> Dict[str, Any]:
        """Fetch a single resource envelope (``{key: {...}}``)."""
        data = self.get_json(path, params=params)
        if isinstance(data, dict) and key in data:
            return data[key] or {}
        if isinstance(data, dict):
            return data
        return {}

    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and return basic connectivity info (no sensitive data).
        Genuinely probes the API so callers can distinguish success from a bad
        key/unreachable host."""
        try:
            # Authenticated probe: fetching a page of tickets returns 200 only
            # when the API key is valid.
            data = self.get_json("/tickets", params={"per_page": 1, "page": 1})
            tickets = self._unwrap(data, "tickets")
        except FreshServiceError as exc:
            return {
                "connected": False,
                "base_url": self.base_url,
                "error": exc.message,
            }
        # Best-effort identity (non-fatal): who is this API key?
        name = None
        try:
            me = self.get_json("/agents/me")
            contact = me.get("contact") if isinstance(me, dict) else None
            name = contact.get("name") if isinstance(contact, dict) else None
        except FreshServiceError:
            name = None
        return {
            "connected": True,
            "base_url": self.base_url,
            "fetchable_tickets": bool(tickets),
            "account": name or "unknown",
        }


# Registry so tools can share one client per config (keyed by base URL).
_client_registry: Dict[str, FreshServiceClient] = {}
_client_registry_lock = threading.Lock()


def get_client(config: FreshServiceConfig) -> FreshServiceClient:
    key = config.resolved_base_url()
    with _client_registry_lock:
        client = _client_registry.get(key)
        if client is None or client.config != config:
            client = FreshServiceClient(config)
            _client_registry[key] = client
        return client


def clear_client(key: str) -> None:
    """Drop a cached client (used on shutdown)."""
    with _client_registry_lock:
        _client_registry.pop(key, None)
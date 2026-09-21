"""Thin REST client for the FreshService (Service Desk) v2 API.

Authentication uses a FreshService API key via HTTP Basic auth. This client
sends the API key as the Basic-auth *username* (``curl -u <api_key>:<pass>``),
which is the form this account accepts and returns HTTP 200 against; the
password field is ignored by FreshService. (Some documentation shows a literal
``api_key`` username with the real key as the password, but that form returns
403 ``access_denied`` on the target tenant.) The key itself is never logged.

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
import time
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

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
        self._session.headers.update({"Content-Type": "application/json"})
        # FreshService authenticates with Basic auth; send the API key as the
        # username (matches the working `curl -u <api_key>:X` form). The password
        # field is ignored, so it is left empty.
        self._session.auth = HTTPBasicAuth(config.api_key, "")

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

    MAX_RETRIES = 3
    RATE_LIMIT_MIN_BACKOFF = 1.0
    RATE_LIMIT_MAX_BACKOFF = 20.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        form_data: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = f"{self.base_url}{self.API_PREFIX}{path}"
        resp = None
        attempt = 0
        while True:
            attempt += 1
            if form_data is not None:
                # multipart/form-data (FreshService notes use `-F` form fields).
                # ``files`` triggers multipart encoding with a boundary; drop the
                # session's JSON Content-Type so requests can set it correctly.
                files = {k: (None, v) for k, v in form_data.items()}
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    files=files,
                    headers={"Content-Type": None},
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            else:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            if resp.status_code != 429:
                break
            # Rate limited: back off and retry (Retry-After honored when present).
            if attempt >= self.MAX_RETRIES:
                break
            wait = self.RATE_LIMIT_MIN_BACKOFF * (2 ** (attempt - 1))
            if "Retry-After" in resp.headers:
                try:
                    wait = max(wait, float(resp.headers["Retry-After"]))
                except ValueError:
                    pass
            wait = min(wait, self.RATE_LIMIT_MAX_BACKOFF)
            time.sleep(wait)

        if resp.status_code == 429:
            raise FreshServiceError(
                429,
                f"FreshService rate limit exceeded for {url} (retried {self.MAX_RETRIES} times). "
                "Wait a minute and try again, or raise the account API rate limit.",
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

    def post_form(self, path: str, data: Dict[str, Any], *, params: Optional[Dict[str, Any]] = None) -> Any:
        """POST as multipart/form-data (FreshService's notes endpoint writes via
        form fields, e.g. ``body`` + ``private``)."""
        resp = self._request("POST", path, params=params, form_data=data)
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
        page: int = 1,
        envelope_key: str = "tickets",
        max_pages: Optional[int] = None,
    ) -> List[Any]:
        """Fetch items from a paginated list endpoint.

        FreshService list endpoints return ``{"<envelope_key>": [...], "meta":
        {...}}`` and accept ``page`` + ``per_page``.

        By default this returns a *single* page (the requested ``page``). Pass
        ``max_pages`` > 1 to walk forward through subsequent pages. Auto-walking
        from page 1 is deliberately NOT done because FreshService rate-limits
        aggressively (HTTP 429) and cascading pages for a small per_page would
        burn the whole request budget on a handful of results.
        """
        per_page = max(1, min(int(per_page), self.MAX_PAGE_SIZE))
        collected: List[Any] = []
        cur = max(1, int(page))
        p = dict(params or {})
        while True:
            p["per_page"] = per_page
            p["page"] = cur
            batch = self.get_json(path, params=p)
            items = self._unwrap(batch, envelope_key)
            if not items:
                break
            collected.extend(items)
            # Default: single page. Otherwise stop once the requested window is
            # exhausted or the server returns a short page.
            if max_pages is None:
                break
            if cur >= (int(page) + max_pages - 1):
                break
            if len(items) < per_page:
                break
            cur += 1
        return collected

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
"""Configuration and secure credential handling for the FreshService MCP server.

Credentials can be supplied from (in order of precedence):
  1. Explicit keyword arguments (e.g. when called programmatically)
  2. Environment variables (FRESHSERVICE_*)
  3. A JSON config file (FRESHSERVICE_CONFIG_FILE, or --config)

The API key is never logged and config files are written with 0600
permissions when created via the ``freshservice config init`` wizard.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# Environment variable names
ENV_DOMAIN = "FRESHSERVICE_DOMAIN"
ENV_BASE_URL = "FRESHSERVICE_BASE_URL"
ENV_API_KEY = "FRESHSERVICE_API_KEY"
ENV_VERIFY_SSL = "FRESHSERVICE_VERIFY_SSL"
ENV_CONFIG_FILE = "FRESHSERVICE_CONFIG_FILE"
ENV_TIMEOUT = "FRESHSERVICE_TIMEOUT"
# Optional API key that gates the network MCP endpoints when set. This is the
# key an MCP client presents (Authorization: Bearer <key>) to the daemon, kept
# separate from the FreshService account API key above.
ENV_MCP_TOKEN = "FRESHSERVICE_MCP_AUTH_TOKEN"

_PASSWORD_TAG = "***REDACTED***"


@dataclass
class FreshServiceConfig:
    """Resolved configuration for a single FreshService/Service Desk account."""

    # Account subdomain, e.g. "acme" for https://acme.freshservice.com
    domain: str = ""
    # Optional full base URL override (wins over ``domain`` when both set).
    base_url: str = ""
    # FreshService API key (sent as the HTTP Basic auth username, i.e.
    # `curl -u <api_key>:X`; the password field is ignored).
    api_key: str = ""
    verify_ssl: bool = True
    # Connection / request timeout in seconds.
    timeout: int = 30
    # Optional API key that gates the HTTP/SSE MCP transport.
    mcp_auth_token: str = ""

    def resolved_base_url(self) -> str:
        """Return the full base URL for the FreshService REST API root."""
        if self.base_url:
            return self.base_url.rstrip("/")
        domain = (self.domain or "").strip().rstrip("/")
        if not domain:
            return ""
        domain = domain.replace("https://", "").replace("http://", "")
        return f"https://{domain}.freshservice.com"

    def redacted(self) -> dict:
        """Return a dict safe for logging (api key/token redacted)."""
        d = asdict(self)
        d["api_key"] = _PASSWORD_TAG if d.get("api_key") else ""
        d["mcp_auth_token"] = _PASSWORD_TAG if d.get("mcp_auth_token") else ""
        d["base_url"] = self.resolved_base_url()
        return d

    def is_complete(self) -> bool:
        return bool(self.resolved_base_url() and self.api_key)


class ConfigError(Exception):
    """Raised when configuration/credentials are missing or invalid."""


def _as_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(
    config_file: Optional[str] = None,
    *,
    domain: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
    timeout: Optional[int] = None,
    mcp_auth_token: Optional[str] = None,
) -> FreshServiceConfig:
    """Load and merge configuration from kwargs, env and a config file.

    Raises :class:`ConfigError` if essential credentials are missing.
    """
    cfg = FreshServiceConfig()

    # 1. Load from well-known config file (env or explicit path).
    config_file = config_file or os.environ.get(ENV_CONFIG_FILE)
    if config_file and Path(config_file).exists():
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        for key in ("domain", "base_url", "api_key", "verify_ssl",
                    "timeout", "mcp_auth_token"):
            if key in data and data[key] is not None:
                setattr(cfg, key, data[key])

    # 2. Env variables override the file.
    if os.environ.get(ENV_DOMAIN):
        cfg.domain = os.environ[ENV_DOMAIN].strip()
    if os.environ.get(ENV_BASE_URL):
        cfg.base_url = os.environ[ENV_BASE_URL].strip()
    if os.environ.get(ENV_API_KEY):
        cfg.api_key = os.environ[ENV_API_KEY].strip()
    if os.environ.get(ENV_VERIFY_SSL) is not None:
        cfg.verify_ssl = _as_bool(os.environ[ENV_VERIFY_SSL], True)
    if os.environ.get(ENV_TIMEOUT):
        try:
            cfg.timeout = int(os.environ[ENV_TIMEOUT])
        except ValueError:
            pass
    if os.environ.get(ENV_MCP_TOKEN):
        cfg.mcp_auth_token = os.environ[ENV_MCP_TOKEN].strip()

    # 3. Explicit arguments win.
    if domain is not None:
        cfg.domain = domain.strip()
    if base_url is not None:
        cfg.base_url = base_url.strip()
    if api_key is not None:
        cfg.api_key = api_key.strip()
    if verify_ssl is not None:
        cfg.verify_ssl = bool(verify_ssl)
    if timeout is not None:
        cfg.timeout = int(timeout)
    if mcp_auth_token is not None:
        cfg.mcp_auth_token = mcp_auth_token.strip()

    if not cfg.is_complete():
        base = cfg.resolved_base_url()
        missing = []
        if not base:
            missing.append("domain (or base_url)")
        if not cfg.api_key:
            missing.append("api_key")
        raise ConfigError(
            "Incomplete FreshService credentials. Missing: "
            + ", ".join(missing)
            + ". Set FRESHSERVICE_* env vars or run `python -m freshservice_mcp "
              "config init --config <file>`."
        )
    return cfg


def configure_interactive(config_file: str) -> str:
    """Prompt securely for credentials and write a 0600 config file.

    The API key is requested with ``getpass`` so it is never echoed to the
    terminal, and the resulting file is only readable by the owner.
    """
    import getpass

    data = {}
    # Seed from an existing file if present.
    p = Path(config_file).expanduser()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    print("FreshService (Service Desk) MCP\n-------------------------------")
    data["domain"] = input(
        f"FreshService subdomain (e.g. 'acme' for acme.freshservice.com) "
        f"[{data.get('domain', '')}]: "
    ).strip() or data.get("domain", "")
    data["api_key"] = getpass.getpass("FreshService API key: ") or data.get("api_key", "")
    data["verify_ssl"] = data.get("verify_ssl", True)

    if not ((data.get("domain") or data.get("base_url")) and data.get("api_key")):
        raise ConfigError("domain and api_key are required.")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    return str(p)
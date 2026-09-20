"""Tool definition functions for the FreshService MCP server.

Each ``register_*`` function wires fastmcp ``@tool`` decorators bound to a
resolved :class:`FreshServiceConfig`. Splitting imports keeps the server lean
and allows tests to build clients on demand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastmcp import FastMCP
    from ..config import FreshServiceConfig

from . import conversation_tools, directory_tools, ticket_tools


def register_all(mcp: "FastMCP", config: "FreshServiceConfig") -> None:
    ticket_tools.register(mcp, config)
    conversation_tools.register(mcp, config)
    directory_tools.register(mcp, config)
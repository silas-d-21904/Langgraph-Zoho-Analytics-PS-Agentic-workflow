"""Shared logging helpers for ZA MCP tools.

The server runs on ``mcp.server.fastmcp.FastMCP``, not standalone fastmcp, so
``fastmcp.server.dependencies.get_context()`` is unavailable in except blocks.
Use standard logging so real API errors are surfaced to callers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("zoho_analytics_mcp.tools")


def log_tool_exception(tool_name: str) -> None:
    """Log the active exception with a full traceback."""
    logger.exception("%s failed", tool_name)

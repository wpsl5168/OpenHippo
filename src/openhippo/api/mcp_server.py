"""MCP Server for OpenHippo — stdio transport."""

from __future__ import annotations

import json
from mcp.server.fastmcp import FastMCP

from ..core.engine import HippoEngine

mcp = FastMCP("openhippo")
engine: HippoEngine | None = None


def _engine() -> HippoEngine:
    global engine
    if engine is None:
        engine = HippoEngine()
    return engine


@mcp.tool()
def memory_add(content: str, target: str = "memory") -> str:
    """Add a memory entry.
    
    Args:
        content: Memory content to store
        target: 'memory' (agent notes) or 'user' (user profile)
    """
    result = _engine().add(target, content)
    return json.dumps(result)


@mcp.tool()
def memory_search(query: str, target: str = "", source: str = "all", limit: int = 20) -> str:
    """Search memories by keywords.
    
    Args:
        query: Search keywords
        target: Filter by 'memory' or 'user', empty for both
        source: 'all', 'hot', 'cold'
        limit: Max results
    """
    result = _engine().search(query, target or None, source, limit)
    return json.dumps(result, default=str, ensure_ascii=False)


@mcp.tool()
def memory_replace(target: str, old_text: str, new_content: str) -> str:
    """Replace a hot memory entry.
    
    Args:
        target: 'memory' or 'user'
        old_text: Unique substring identifying the entry to replace
        new_content: New content
    """
    result = _engine().replace(target, old_text, new_content)
    return json.dumps(result)


@mcp.tool()
def memory_remove(target: str, old_text: str) -> str:
    """Remove a hot memory entry.
    
    Args:
        target: 'memory' or 'user'
        old_text: Unique substring identifying the entry to remove
    """
    result = _engine().remove(target, old_text)
    return json.dumps(result)


@mcp.tool()
def memory_archive(target: str, old_text: str) -> str:
    """Archive a hot memory to cold storage.
    
    Args:
        target: 'memory' or 'user'
        old_text: Unique substring identifying the entry
    """
    result = _engine().archive(target, old_text)
    return json.dumps(result)


@mcp.tool()
def memory_promote(memory_id: str) -> str:
    """Promote a cold memory back to hot storage.
    
    Args:
        memory_id: Cold memory entry ID
    """
    result = _engine().promote(memory_id)
    return json.dumps(result)


@mcp.tool()
def memory_stats() -> str:
    """Get memory statistics."""
    result = _engine().stats()
    return json.dumps(result)


def run():
    mcp.run()

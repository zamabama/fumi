"""
Claude Bridge — MCP Server

Gives Claude Code tools to send/read messages through the Cloudflare Worker.
Runs on both Mac and PC. Machine identity set via BRIDGE_MACHINE_ID env var.

Usage (stdio, via .mcp.json):
    python3 tools/bridge/mcp_server.py
"""

import os
import httpx
from mcp.server.fastmcp import FastMCP

# --- Config from environment ---

WORKER_URL = os.environ.get("BRIDGE_WORKER_URL", "").rstrip("/")
API_KEY = os.environ.get("BRIDGE_API_KEY", "")
MACHINE_ID = os.environ.get("BRIDGE_MACHINE_ID", "unknown")

if not WORKER_URL:
    raise RuntimeError("BRIDGE_WORKER_URL environment variable is required")
if not API_KEY:
    raise RuntimeError("BRIDGE_API_KEY environment variable is required")

# --- MCP Server ---

mcp = FastMCP(
    name="claude-bridge",
    instructions=(
        "Message bridge between Mac and PC Claude Code instances. "
        "Use check_messages at the start of each session to see if there are "
        "pending messages from the other machine."
    ),
)


def _headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


@mcp.tool(
    name="send_message",
    description="Send a message to the other machine's Claude Code instance.",
)
async def send_message(content: str, tags: list[str] | None = None) -> dict:
    """Send a message through the bridge.

    Args:
        content: The message text (handover notes, task direction, status updates, etc.)
        tags: Optional tags for categorization (e.g. ["vlt", "handover", "tracking"])
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            json={"content": content, "from": MACHINE_ID, "tags": tags or []},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(
    name="read_messages",
    description="Read messages from the bridge, optionally filtered.",
)
async def read_messages(
    unread_only: bool = False,
    from_machine: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> dict:
    """Read messages, optionally filtered.

    Args:
        unread_only: Only return unread messages
        from_machine: Filter by sender ("mac" or "pc")
        tag: Filter by tag
        limit: Maximum number of messages to return (default 20)
    """
    params = {"limit": str(limit)}
    if unread_only:
        params["unread"] = "true"
    if from_machine:
        params["from"] = from_machine
    if tag:
        params["tag"] = tag

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(
    name="check_messages",
    description=(
        "Quick check — how many unread messages are waiting? "
        "Call this at the start of every session."
    ),
)
async def check_messages() -> dict:
    """Check for unread messages. Returns count and latest sender info."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params={"unread": "true", "limit": "100"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

    messages = data.get("messages", [])
    if not messages:
        return {"unread_count": 0, "latest_from": None, "latest_timestamp": None}

    latest = messages[0]  # already sorted most-recent-first by Worker
    return {
        "unread_count": len(messages),
        "latest_from": latest["from"],
        "latest_timestamp": latest["timestamp"],
    }


@mcp.tool(
    name="mark_read",
    description="Mark a specific message as read.",
)
async def mark_read(message_id: str) -> dict:
    """Mark a message as read by its ID.

    Args:
        message_id: UUID of the message to mark as read
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WORKER_URL}/messages/{message_id}/read",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(
    name="clear_messages",
    description="Delete all messages. Requires confirm=true as a safety check.",
)
async def clear_messages(confirm: bool = False) -> dict:
    """Clear all messages from the bridge.

    Args:
        confirm: Must be True to actually delete. Safety check.
    """
    if not confirm:
        return {"error": "Pass confirm=true to clear all messages"}

    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params={"confirm": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run(transport="stdio")

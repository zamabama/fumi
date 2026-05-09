"""
Photon — Message Bridge MCP Server

Gives Claude Code tools to send/read messages through the Cloudflare Worker.
Runs on both Mac and PC. Machine identity set via BRIDGE_MACHINE_ID env var.

Messages are scoped by project. If BRIDGE_PROJECT is set, agents only see
messages addressed to their project (or same-project broadcasts).

Usage (stdio, via .mcp.json):
    python3 tools/photon/mcp_server.py
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# --- Config from environment ---

WORKER_URL = os.environ.get("BRIDGE_WORKER_URL", "").rstrip("/")
API_KEY = os.environ.get("BRIDGE_API_KEY", "")
MACHINE_ID = os.environ.get("BRIDGE_MACHINE_ID", "unknown")
PROJECT = os.environ.get("BRIDGE_PROJECT", "") or None

# Composite identity: "mac/cheetos", "pc/autonomy", etc.
IDENTITY = f"{MACHINE_ID}/{PROJECT}" if PROJECT else MACHINE_ID

if not WORKER_URL:
    raise RuntimeError("BRIDGE_WORKER_URL environment variable is required")
if not API_KEY:
    raise RuntimeError("BRIDGE_API_KEY environment variable is required")

# How recent a message must be to count as "fresh" for the default unread fetch.
# The user's actual workflow: send a message, then immediately go to the recipient
# agent and ask it to check. Old "unread" backlog is almost never what was meant.
DEFAULT_FRESHNESS_MINUTES = 10

# Where resolve_project() looks for sibling projects on this machine.
PROJECT_SEARCH_ROOTS = [Path.home() / "dev", Path.home() / "Documents"]
PROJECT_SEARCH_MAX_DEPTH = 4

# --- MCP Server ---

mcp = FastMCP(
    name="photon",
    instructions=(
        "Photon — message bridge between Claude Code instances across machines and projects.\n"
        "\n"
        "## At session start\n"
        "Call check_messages. By default it only surfaces messages from the last "
        f"{DEFAULT_FRESHNESS_MINUTES} minutes — that is almost always what the user means "
        "when they say 'check photon' (they just sent something and walked over to you). "
        "If check_messages returns 0 fresh but reports older_unread_count > 0, do NOT pull "
        "those old messages unless the user explicitly asks for them. They are stale.\n"
        "\n"
        "## Reading messages\n"
        "Use read_messages to actually read what check_messages flagged. By default it "
        "only returns recent unread and auto-marks them read so they do not pile up. "
        "If you need older history, pass max_age_minutes= or since=.\n"
        "\n"
        "## Sending to another project — you MUST resolve the project first\n"
        "When the user says 'send a photon message to the agent in charge of project X', "
        "do NOT guess the project name. Call resolve_project(hint='X') first. It scans "
        "~/dev and ~/Documents for Photon-enabled projects and returns candidate identities.\n"
        "  - 1 confident match → use that identity as target= in send_message\n"
        "  - 0 matches or multiple ambiguous matches → ASK THE USER which one they meant\n"
        "  - Never invent a target string from imagination\n"
        "\n"
        "## Same-project messaging\n"
        "Messages without target= are broadcast to other agents in your own project. "
        "If you are talking to a specific agent in another project, target= is required — "
        "the server will reject sends that look cross-project but have no target.\n"
        "\n"
        "## Replying\n"
        "When you complete a task that was ASSIGNED to you via Photon (a work package, "
        "a bug fix request, a specific ask), send a brief status message back with matching tags. "
        "Don't reply to one-way informational messages — only respond when work was requested and completed."
    ),
)


def _headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def _visible_to_me(msg: dict) -> bool:
    """Single source of truth for 'is this message for me?'.

    Visibility rules:
      - Explicitly addressed to my IDENTITY → yes
      - Broadcast (to=None) and message's project matches mine → yes
      - Broadcast with project=None → only visible to agents that also have no PROJECT
      - Anything else → no
    """
    msg_to = msg.get("to")
    msg_project = msg.get("project")

    if msg_to == IDENTITY:
        return True
    if msg_to is not None:
        # Addressed to someone else
        return False
    # to is None — it's a broadcast
    if msg_project is None and PROJECT is None:
        return True
    if msg_project is not None and msg_project == PROJECT:
        return True
    return False


def _default_since_iso(max_age_minutes: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    # Worker compares timestamps lexicographically as ISO strings — keep the same format.
    return cutoff.isoformat().replace("+00:00", "Z")


def _find_photon_projects() -> list[dict]:
    """Scan known dev/document roots for .mcp.json files containing a photon config.

    Returns one entry per discovered project with {directory, project, machine, identity, path}.
    Skips hidden dirs and node_modules.
    """
    found: list[dict] = []
    seen: set[Path] = set()

    for root in PROJECT_SEARCH_ROOTS:
        if not root.exists():
            continue
        for mcp_file in root.rglob(".mcp.json"):
            # Skip if any path component is hidden (besides .mcp.json itself) or node_modules
            parent_parts = mcp_file.parent.parts
            if any(p == "node_modules" or (p.startswith(".") and p != "") for p in parent_parts[len(root.parts):]):
                continue
            depth = len(mcp_file.relative_to(root).parts)
            if depth > PROJECT_SEARCH_MAX_DEPTH:
                continue
            if mcp_file in seen:
                continue
            seen.add(mcp_file)

            try:
                config = json.loads(mcp_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            servers = config.get("mcpServers", {})
            photon_cfg = servers.get("photon")
            if not isinstance(photon_cfg, dict):
                continue
            env = photon_cfg.get("env", {}) or {}
            project = env.get("BRIDGE_PROJECT")
            machine = env.get("BRIDGE_MACHINE_ID", "unknown")
            if not project:
                continue
            found.append({
                "directory_name": mcp_file.parent.name,
                "project": project,
                "machine": machine,
                "identity": f"{machine}/{project}",
                "path": str(mcp_file.parent),
            })

    return found


def _score_match(hint: str, candidate: dict) -> int:
    """Score how well a candidate matches the hint. 0 = no match."""
    h = hint.lower().strip()
    if not h:
        return 0
    dir_name = candidate["directory_name"].lower()
    project = candidate["project"].lower()
    path = candidate["path"].lower()

    if h == dir_name or h == project:
        return 100
    if h in dir_name or dir_name in h:
        return 80
    if h in project or project in h:
        return 70
    if h in path:
        return 40
    return 0


@mcp.tool(
    name="send_message",
    description=(
        "Send a message to another Claude Code instance. "
        "For cross-project messaging, FIRST call resolve_project(hint=...) to get the correct target identity. "
        "Use 'target' to route to a specific machine/project (e.g. 'mac/cheetos')."
    ),
)
async def send_message(content: str, tags: list[str] | None = None, target: str | None = None) -> dict:
    """Send a message through the bridge.

    Args:
        content: The message text (handover notes, task direction, status updates, etc.)
        tags: Optional tags for categorization (e.g. ["vlt", "handover", "tracking"])
        target: Target recipient (e.g. "mac/cheetos", "pc/autonomy"). Omit for same-project broadcast.
    """
    tags = tags or []

    # Cross-project safety: if any tag looks like a known project name OTHER than ours,
    # but the sender forgot to set target=, refuse the send. Silent drops are the #1
    # cause of "I sent it but the other agent says nothing's there."
    if not target and tags:
        try:
            known = _find_photon_projects()
        except Exception:
            known = []
        own_project = (PROJECT or "").lower()
        own_dir = ""
        for k in known:
            if k["project"].lower() == own_project:
                own_dir = k["directory_name"].lower()
                break
        foreign_project_names = {
            k["directory_name"].lower() for k in known
            if k["project"].lower() != own_project and k["directory_name"].lower() != own_dir
        } | {
            k["project"].lower() for k in known
            if k["project"].lower() != own_project
        }
        for tag in tags:
            if tag.lower() in foreign_project_names:
                return {
                    "error": "cross_project_send_without_target",
                    "message": (
                        f"Tag '{tag}' looks like another project, but no target= was set. "
                        f"Without target=, this message is scoped to your project '{PROJECT}' "
                        f"and the other project will not see it. "
                        f"Call resolve_project(hint='{tag}') to get the right target identity, "
                        f"then retry with target=<that identity>."
                    ),
                    "hint_tag": tag,
                }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            json={
                "content": content,
                "from": IDENTITY,
                "project": PROJECT,
                "to": target or None,  # 'target' param → 'to' in wire format (FastMCP drops 'to' as a param name)
                "tags": tags,
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()

    if PROJECT and not target:
        result["scope"] = f"same-project broadcast (project='{PROJECT}')"
    elif target:
        result["scope"] = f"direct to {target}"
    else:
        result["scope"] = "global broadcast (no project, no target)"

    return result


@mcp.tool(
    name="read_messages",
    description=(
        "Read messages from the bridge, auto-filtered to your identity. "
        "Defaults: only fresh unread (last 10 min) and auto-marks them read so the queue doesn't pile up. "
        "Pass max_age_minutes= or since= to look further back."
    ),
)
async def read_messages(
    unread_only: bool = True,
    from_machine: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    all_projects: bool = False,
    max_age_minutes: int | None = None,
    since: str | None = None,
    keep_unread: bool = False,
) -> dict:
    """Read messages, auto-filtered to this project's context.

    Args:
        unread_only: Only return unread messages (default True)
        from_machine: Filter by sender (e.g. "mac", "pc", "mac/cheetos")
        tag: Filter by tag
        limit: Maximum number of messages to return (default 20)
        all_projects: Set True to bypass identity filtering (for debugging)
        max_age_minutes: Only return messages newer than this many minutes (default 10 when unread_only=True; no default otherwise)
        since: ISO timestamp; only return messages newer than this. Overrides max_age_minutes.
        keep_unread: Set True to NOT auto-mark fetched messages as read
    """
    # Recency: when checking unread, default to fresh-only. The whole point of "unread"
    # in practice is "did anything just arrive," not "give me 6 months of backlog."
    effective_since = since
    if effective_since is None:
        if max_age_minutes is not None:
            effective_since = _default_since_iso(max_age_minutes)
        elif unread_only:
            effective_since = _default_since_iso(DEFAULT_FRESHNESS_MINUTES)

    params: dict[str, str] = {"limit": str(limit)}
    if unread_only:
        params["unread"] = "true"
    if from_machine:
        params["from"] = from_machine
    if tag:
        params["tag"] = tag
    if effective_since:
        params["since"] = effective_since
    # Auto-mark on the wire so two agents racing don't both grab it.
    if unread_only and not keep_unread:
        params["mark_read"] = "true"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

    raw_messages = data.get("messages", [])
    raw_count = len(raw_messages)

    if not all_projects:
        visible = [m for m in raw_messages if _visible_to_me(m)]
    else:
        visible = raw_messages

    data["messages"] = visible
    data["count"] = len(visible)
    data["filtered_out"] = raw_count - len(visible)
    data["identity"] = IDENTITY
    if effective_since:
        data["since"] = effective_since
        data["since_note"] = (
            f"Only messages newer than {effective_since} were considered. "
            f"Pass since=<older ISO ts> or max_age_minutes=<bigger> to look further back."
        )

    return data


@mcp.tool(
    name="check_messages",
    description=(
        "Quick check — is anything fresh waiting for me? "
        "Defaults to the last 10 minutes (which matches the user saying 'check photon, I just sent it'). "
        "Reports older_unread_count separately so you know if there's stale backlog without confusing it with new mail."
    ),
)
async def check_messages(max_age_minutes: int = DEFAULT_FRESHNESS_MINUTES) -> dict:
    """Check for FRESH unread messages addressed to this identity.

    Args:
        max_age_minutes: How recent counts as 'fresh' (default 10).
    """
    fresh_since = _default_since_iso(max_age_minutes)

    async with httpx.AsyncClient() as client:
        # Pull a wider net so we can also report on old backlog without surfacing it.
        resp = await client.get(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params={"unread": "true", "limit": "200"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

    all_unread = [m for m in data.get("messages", []) if _visible_to_me(m)]
    fresh = [m for m in all_unread if m.get("timestamp", "") > fresh_since]
    older = [m for m in all_unread if m.get("timestamp", "") <= fresh_since]

    if fresh:
        latest = fresh[0]  # sorted most-recent-first by Worker
        latest_info = {
            "from": latest.get("from"),
            "timestamp": latest.get("timestamp"),
            "tags": latest.get("tags", []),
            "preview": (latest.get("content") or "")[:120],
        }
    else:
        latest_info = None

    result = {
        "fresh_unread_count": len(fresh),
        "older_unread_count": len(older),
        "fresh_window_minutes": max_age_minutes,
        "fresh_since": fresh_since,
        "latest_fresh": latest_info,
        "identity": IDENTITY,
    }
    if not fresh and older:
        result["note"] = (
            f"Nothing fresh in the last {max_age_minutes} min. "
            f"There are {len(older)} older unread message(s) but the user almost certainly "
            f"did NOT mean those — they're stale. Only surface them if explicitly asked."
        )
    return result


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
    name="resolve_project",
    description=(
        "Find Photon-enabled projects on this machine matching a name hint. "
        "Call this BEFORE send_message whenever the user names another project. "
        "Returns candidates with their composite identities to use as target=."
    ),
)
async def resolve_project(hint: str) -> dict:
    """Resolve a user-spoken project name to a Photon identity.

    Args:
        hint: The project name the user said (e.g. "Cocobun", "saku", "lexus").
              Matched against both the directory name and the BRIDGE_PROJECT value.
    """
    candidates = _find_photon_projects()

    # Score and rank
    scored = []
    for c in candidates:
        s = _score_match(hint, c)
        if s > 0:
            scored.append({**c, "score": s})
    scored.sort(key=lambda x: -x["score"])

    # Detect identity collisions (multiple projects sharing the same composite identity).
    # This is the "everyone is mac/global" problem — sends to that identity hit ALL of them.
    identity_buckets: dict[str, list[str]] = {}
    for c in candidates:
        identity_buckets.setdefault(c["identity"], []).append(c["directory_name"])
    collisions = {ident: dirs for ident, dirs in identity_buckets.items() if len(dirs) > 1}

    if scored:
        top = scored[0]
        confident = (
            len(scored) == 1
            or scored[0]["score"] >= 100
            or (len(scored) > 1 and scored[0]["score"] - scored[1]["score"] >= 30)
        )
        # If the top match's identity is shared with other projects, it's NOT a confident route.
        if top["identity"] in collisions:
            confident = False
        if confident:
            recommendation = f"Use target='{top['identity']}' for project '{top['directory_name']}'."
        else:
            recommendation = (
                "Multiple plausible matches. ASK THE USER which one they meant before sending. "
                "Never guess."
            )
    else:
        recommendation = (
            f"No project matching '{hint}' found in {[str(p) for p in PROJECT_SEARCH_ROOTS]}. "
            f"ASK THE USER to clarify the project name or path. Do not guess."
        )

    result = {
        "hint": hint,
        "matches": scored,
        "all_known_projects_count": len(candidates),
        "recommendation": recommendation,
    }
    if collisions:
        result["identity_collisions"] = collisions
        result["collision_warning"] = (
            "These identities are shared by multiple project directories (BRIDGE_PROJECT values "
            "are not unique). A send to a colliding identity will be visible to ALL of them. "
            "Tell the user to give each project a unique BRIDGE_PROJECT in its .mcp.json."
        )
    return result


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

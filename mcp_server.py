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
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# --- Config from environment ---

WORKER_URL = os.environ.get("BRIDGE_WORKER_URL", "").rstrip("/")
API_KEY = os.environ.get("BRIDGE_API_KEY", "")
MACHINE_ID = os.environ.get("BRIDGE_MACHINE_ID", "unknown")
PROJECT = os.environ.get("BRIDGE_PROJECT", "") or None

# Base identity for this project: "mac/cheetos", "pc/autonomy", etc.
BASE_IDENTITY = f"{MACHINE_ID}/{PROJECT}" if PROJECT else MACHINE_ID

# Optional workstream sub-identity, e.g. "website" → "mac/primogen#website".
# This is what lets several agents open at the SAME project root keep their own
# independent read state. Seeded from PHOTON_WORKSTREAM, but overridable at
# runtime via set_identity() — agents that share one .mcp.json also share its
# env, so the sub-identity has to be settable per-agent, in-process.
_workstream = os.environ.get("PHOTON_WORKSTREAM", "").strip() or None

# Short human-readable note about what this agent is doing (shown in the roster).
_description = os.environ.get("PHOTON_DESCRIPTION", "").strip() or None

# Stable per-process token. Each agent/chat gets its own MCP subprocess, so this
# uniquely identifies THIS agent — it's how the roster tells "me refreshing" apart
# from "a different agent tried to claim my workstream label".
AGENT_TOKEN = uuid.uuid4().hex

# Throttle for best-effort presence heartbeats (seconds). Keeps KV writes bounded
# even with many agents reading frequently.
_HEARTBEAT_INTERVAL_S = 60
_last_heartbeat = 0.0


def base_identity() -> str:
    """The project-level identity, without any workstream suffix."""
    return BASE_IDENTITY


def current_identity() -> str:
    """This agent's full identity, including its workstream suffix if set."""
    return f"{BASE_IDENTITY}#{_workstream}" if _workstream else BASE_IDENTITY

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
        "## Multiple agents at the same project root (workstreams + roster)\n"
        "When several agents/chats are open on the SAME project and each needs to receive the "
        "same broadcasts independently, claim a workstream sub-identity at session start:\n"
        "  1. Call list_identities() to see who's already active and which labels are taken.\n"
        "  2. Pick a short label that matches YOUR task and isn't taken (e.g. 'website', "
        "'crypto', 'ops'), then call set_identity('<label>', description='<what you're doing>'). "
        "If the label is already held the call is refused — pick another. Only ask the user if "
        "it's genuinely ambiguous.\n"
        "Each workstream has its OWN read cursor, so one agent reading a message does NOT mark it "
        "read for the others. To message a specific lane, call list_identities() to find it and "
        "send with target='mac/<project>#<workstream>' (works cross-project too: "
        "list_identities(project='X')). A lone agent doesn't need any of this — with no workstream "
        "you get the normal single project mailbox, exactly as before.\n"
        "\n"
        "## Stay in your lane (IMPORTANT)\n"
        "You ONLY ever receive mail addressed to you (your identity, your project's broadcasts). "
        "check_messages and read_messages never surface other agents' messages. Do NOT go looking "
        "for, read, or act on mail meant for another workstream. Another lane's task is not yours — "
        "act only on what is addressed to you. If you have no fresh mail, the answer is 'nothing for "
        "me', not 'let me check what others got'.\n"
        "Only when the USER explicitly tells you to look elsewhere:\n"
        "  - read_messages(as_recipient='mac/<project>#<lane>') reads one specific other lane's mail "
        "(e.g. 'grab the message I sent to crypto by mistake').\n"
        "  - read_messages(include_all_lanes=true) searches every lane in your project, read-only "
        "(e.g. 'I sent you something but you can't find it — look across all lanes').\n"
        "(Senders: a message to a lane no live agent is using is held by default so it can't get "
        "lost; force with allow_unregistered=true.)\n"
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


def _parse_identity(identity: str) -> tuple[str, str | None, str | None]:
    """Split an identity into (base, project, workstream).

    "mac/primogen#website" → ("mac/primogen", "primogen", "website")
    "mac/primogen"         → ("mac/primogen", "primogen", None)
    "mac"                  → ("mac", None, None)
    """
    base, _, workstream = identity.partition("#")
    workstream = workstream or None
    project = base.split("/", 1)[1] if "/" in base else None
    return base, project, workstream


def _visible_to(msg: dict, identity: str) -> bool:
    """Is this message addressed to (visible to) the given identity?

    Visibility only — read/unread is tracked separately, per-identity, by the
    Worker. Used both for 'me' and for reading on behalf of another lane.

    Rules:
      - Addressed to that exact identity (including #workstream) → yes
      - Addressed to that identity's project base (e.g. a cross-project send to
        'mac/primogen') → visible to every agent/workstream in the project
      - Addressed to a different identity / workstream → no
      - Broadcast (to=None): project-scoped — every agent in the project sees it;
        a project=None broadcast only reaches no-project agents
    """
    base, project, _ws = _parse_identity(identity)
    msg_to = msg.get("to")
    msg_project = msg.get("project")

    if msg_to is not None:
        return msg_to == identity or msg_to == base

    # to is None — it's a broadcast
    if msg_project is None and project is None:
        return True
    if msg_project is not None and msg_project == project:
        return True
    return False


def _visible_to_me(msg: dict) -> bool:
    """Visibility for this session's own identity."""
    return _visible_to(msg, current_identity())


def _default_since_iso(max_age_minutes: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    # Worker compares timestamps lexicographically as ISO strings — keep the same format.
    return cutoff.isoformat().replace("+00:00", "Z")


async def _register_presence() -> dict:
    """Register/refresh this agent's identity in the project roster (best-effort).

    Returns the Worker's response dict. On a label collision the Worker replies
    with status='collision' (HTTP 409) instead of stealing the slot. Never raises:
    presence is auxiliary and must not break messaging.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{WORKER_URL}/presence",
                headers=_headers(),
                json={
                    "project": PROJECT,
                    "identity": current_identity(),
                    "workstream": _workstream,
                    "description": _description,
                    "token": AGENT_TOKEN,
                },
                timeout=8,
            )
        if resp.status_code == 409:
            return resp.json()
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — presence is best-effort
        return {"status": "error", "error": str(exc)}


async def _heartbeat() -> None:
    """Throttled, best-effort presence refresh, called on normal activity."""
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < _HEARTBEAT_INTERVAL_S:
        return
    _last_heartbeat = now
    await _register_presence()


async def _fetch_roster(project: str | None) -> list[dict]:
    """Fetch the live identities for a project from the Worker's presence registry."""
    params: dict[str, str] = {}
    if project:
        params["project"] = project
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WORKER_URL}/presence", headers=_headers(), params=params, timeout=8
        )
        resp.raise_for_status()
        return resp.json().get("identities", [])


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
async def send_message(
    content: str,
    tags: list[str] | None = None,
    target: str | None = None,
    allow_unregistered: bool = False,
) -> dict:
    """Send a message through the bridge.

    Args:
        content: The message text (handover notes, task direction, status updates, etc.)
        tags: Optional tags for categorization (e.g. ["vlt", "handover", "tracking"])
        target: Target recipient (e.g. "mac/cheetos", "pc/autonomy"). Omit for same-project broadcast.
        allow_unregistered: Force-send to a #workstream target even if no live agent is
            using it (bypasses the dead-lane guard). Default False.
    """
    await _heartbeat()
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

    # Dead-lane guard: a #workstream target that no live agent is using is almost
    # always a typo or an idle lane — the message would sit unread and invisible to
    # everyone else. Hold it and show the roster unless explicitly forced.
    if target and "#" in target and not allow_unregistered:
        _t_base, t_project, _t_ws = _parse_identity(target)
        try:
            roster = await _fetch_roster(t_project)
        except Exception:
            roster = []
        live_ids = {r.get("identity") for r in roster}
        if target not in live_ids:
            same_base = sorted(
                r["identity"] for r in roster
                if r.get("identity", "").partition("#")[0] == _t_base
            )
            return {
                "error": "target_workstream_not_live",
                "message": (
                    f"No live agent is using '{target}', so the message was NOT sent — it would "
                    "sit unread and invisible to the other lanes. Pick a live lane below, or "
                    "re-send with allow_unregistered=true to force it."
                ),
                "target": target,
                "live_lanes_same_project": same_base or [r.get("identity") for r in roster],
                "roster": roster,
            }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            json={
                "content": content,
                "from": current_identity(),
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
        "Read messages addressed to YOU. Defaults: only fresh unread (last 10 min), auto-marked read. "
        "Pass max_age_minutes= or since= to look further back. "
        "You only ever see your own mail — do NOT try to read other lanes' messages unless the USER "
        "explicitly directs you to. When the user does: as_recipient='mac/<project>#<lane>' reads one "
        "specific other lane's mail (e.g. to pick up something mis-sent there); include_all_lanes=true "
        "searches ALL lanes in your project read-only (use when the user says a message should exist "
        "for you but you can't find it)."
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
    as_recipient: str | None = None,
    include_all_lanes: bool = False,
) -> dict:
    """Read messages, auto-filtered to this project's context.

    Args:
        unread_only: Only return unread messages (default True)
        from_machine: Filter by sender (e.g. "mac", "pc", "mac/cheetos")
        tag: Filter by tag
        limit: Maximum number of messages to return (default 20)
        all_projects: Set True to bypass all visibility filtering, every project (debugging)
        max_age_minutes: Only return messages newer than this many minutes (default 10 when unread_only=True; no default otherwise)
        since: ISO timestamp; only return messages newer than this. Overrides max_age_minutes.
        keep_unread: Set True to NOT auto-mark fetched messages as read
        as_recipient: USER-DIRECTED ONLY. Read on behalf of another lane's identity (e.g. to
            retrieve mail mis-sent to "mac/<project>#crypto") WITHOUT changing your own identity.
            By default this consumes that lane's copy; pass keep_unread=True to peek only.
        include_all_lanes: USER-DIRECTED ONLY. Search ALL lanes in your project (read-only, never
            marks read) — for finding a message the user says should be here but isn't in your lane.
    """
    await _heartbeat()
    # Identity to read/mark AS. Normally me; with as_recipient, another lane (used,
    # only when the user directs it, to pick up mail mis-sent to that lane).
    read_identity = as_recipient or current_identity()

    # Cross-lane visibility opt-outs — both are explicit, user-directed escape hatches.
    # include_all_lanes: every lane in MY project. all_projects: everything, everywhere.
    bypass_visibility = include_all_lanes or all_projects

    # Recency: when checking unread, default to fresh-only. The whole point of "unread"
    # in practice is "did anything just arrive," not "give me 6 months of backlog."
    effective_since = since
    if effective_since is None:
        if max_age_minutes is not None:
            effective_since = _default_since_iso(max_age_minutes)
        elif unread_only:
            effective_since = _default_since_iso(DEFAULT_FRESHNESS_MINUTES)

    params: dict[str, str] = {"limit": str(limit), "identity": read_identity}
    if unread_only:
        params["unread"] = "true"
    if from_machine:
        params["from"] = from_machine
    if tag:
        params["tag"] = tag
    if effective_since:
        params["since"] = effective_since
    if bypass_visibility:
        params["all_lanes"] = "true"
        # Keep an all-lanes search scoped to my own project unless explicitly all_projects.
        if include_all_lanes and not all_projects and PROJECT:
            params["project"] = PROJECT
    # Auto-mark on the wire, scoped to read_identity. Never auto-mark when searching
    # across lanes — that's a read-only look at others' mail, not mine to consume.
    if unread_only and not keep_unread and not bypass_visibility:
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

    if bypass_visibility:
        visible = raw_messages
    else:
        visible = [m for m in raw_messages if _visible_to(m, read_identity)]

    data["messages"] = visible
    data["count"] = len(visible)
    data["filtered_out"] = raw_count - len(visible)
    data["identity"] = read_identity
    if as_recipient:
        data["read_as"] = as_recipient
        data["your_identity"] = current_identity()
        data["read_as_note"] = (
            f"Read on behalf of '{as_recipient}'. Your own identity is unchanged "
            f"('{current_identity()}'). "
            + ("Their copy was left unread (peek)." if keep_unread
               else "Their copy was marked read (pass keep_unread=True to peek instead).")
        )
    if include_all_lanes:
        data["scope_note"] = (
            "Showing ALL lanes in your project because you asked to search wide. These are NOT all "
            "addressed to you — check each message's 'to' field before acting, and only act on the "
            "one the user actually wants. Read-only: nothing was marked read."
        )
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
        "Only ever returns mail addressed to YOU. Reports older_unread_count separately so you know if "
        "there's stale backlog without confusing it with new mail."
    ),
)
async def check_messages(max_age_minutes: int = DEFAULT_FRESHNESS_MINUTES) -> dict:
    """Check for FRESH unread messages addressed to this identity.

    Args:
        max_age_minutes: How recent counts as 'fresh' (default 10).
    """
    await _heartbeat()
    fresh_since = _default_since_iso(max_age_minutes)

    async with httpx.AsyncClient() as client:
        # Pull a wider net so we can also report on old backlog without surfacing it.
        resp = await client.get(
            f"{WORKER_URL}/messages",
            headers=_headers(),
            params={"unread": "true", "limit": "200", "identity": current_identity()},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

    # The Worker only returns mail addressed to me (or my project's broadcasts), so
    # this is just defence-in-depth — other lanes' messages never arrive here.
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
        "identity": current_identity(),
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
            params={"identity": current_identity()},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(
    name="set_identity",
    description=(
        "Declare this agent's workstream sub-identity (e.g. 'website', 'crypto', 'ops') so it "
        "gets its OWN independent read state AND shows up in the project roster. Call this once at "
        "session start when several agents are open on the SAME project root. Best practice: call "
        "list_identities() first, pick a label that fits your task and isn't already taken, then "
        "claim it here with a short description. If the label is already held by another live "
        "agent the call is refused so you can pick another. A single agent / cross-project "
        "messaging does NOT need this. Pass workstream='' to clear and use the plain project mailbox."
    ),
)
async def set_identity(workstream: str, description: str = "") -> dict:
    """Set (or clear) this session's workstream sub-identity and register it.

    State lives in this MCP server process, which is per-agent, so two agents at
    the same project root that call set_identity with different workstreams get
    fully independent read cursors.

    Args:
        workstream: Short label for this agent's lane (e.g. "website"). Must not
            contain '/' or '#'. Pass "" to clear and use the base project mailbox.
        description: Optional short note shown in the roster (e.g. "landing redesign").
    """
    global _workstream, _description, _last_heartbeat
    ws = (workstream or "").strip()
    if ws and ("/" in ws or "#" in ws):
        return {
            "error": "invalid_workstream",
            "message": "Workstream must not contain '/' or '#'. Use a short label like 'website'.",
        }

    prev_ws, prev_desc = _workstream, _description
    _workstream = ws or None
    _description = (description or "").strip() or None

    # Claim the label in the roster. If a different live agent already holds this
    # workstream, the Worker refuses — revert and tell the caller to pick another.
    reg = await _register_presence()
    if reg.get("status") == "collision":
        _workstream, _description = prev_ws, prev_desc
        held = reg.get("collision", {})
        held_desc = f" ({held['description']})" if held.get("description") else ""
        return {
            "error": "workstream_taken",
            "message": (
                f"Workstream '{ws}' is already held by another active agent on project "
                f"'{PROJECT}'{held_desc}, last seen {held.get('last_seen')}. Pick a different "
                "label (call list_identities to see what's taken) or ask the user."
            ),
            "still_using": current_identity(),
        }
    _last_heartbeat = time.time()  # the register above counts as a heartbeat

    if _workstream:
        note = (
            f"This session now reads and sends as '{current_identity()}'. Its read cursor is "
            f"independent from any other workstream at project '{PROJECT}', and it's now listed "
            "in the roster (list_identities)."
        )
    else:
        note = f"Workstream cleared. Reading and sending as the base identity '{base_identity()}'."
    return {
        "identity": current_identity(),
        "base_identity": base_identity(),
        "workstream": _workstream,
        "description": _description,
        "note": note,
    }


@mcp.tool(
    name="list_identities",
    description=(
        "List the agents currently active on a project and the workstream sub-identities they've "
        "claimed (the roster). Call this BEFORE set_identity to pick a free label, and before "
        "sending to a specific lane to find the right target (e.g. 'mac/primogen#website'). "
        "Defaults to your own project; pass project= to inspect another project's roster."
    ),
)
async def list_identities(project: str | None = None) -> dict:
    """List active identities (the roster) for a project.

    Args:
        project: Optional project name/hint. Omit for your own project. If given,
            it's resolved against the Photon projects known on this machine.
    """
    target_project = PROJECT
    resolution = None

    if project:
        candidates = _find_photon_projects()
        scored = sorted(
            ({**c, "score": _score_match(project, c)} for c in candidates),
            key=lambda x: -x["score"],
        )
        scored = [c for c in scored if c["score"] > 0]
        if not scored:
            return {
                "error": "project_not_found",
                "message": (
                    f"No Photon project matching '{project}' found. Ask the user to clarify, "
                    "or call resolve_project for candidates."
                ),
            }
        top = scored[0]
        confident = (
            len(scored) == 1
            or top["score"] >= 100
            or (len(scored) > 1 and top["score"] - scored[1]["score"] >= 30)
        )
        if not confident:
            return {
                "error": "ambiguous_project",
                "message": "Multiple projects match that hint. Ask the user which one before listing.",
                "candidates": scored,
            }
        target_project = top["project"]
        resolution = {
            "hint": project,
            "resolved_project": target_project,
            "directory": top["directory_name"],
        }

    params: dict[str, str] = {}
    if target_project:
        params["project"] = target_project

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WORKER_URL}/presence",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

    data["you_are"] = current_identity()
    if resolution:
        data["resolution"] = resolution
    return data


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

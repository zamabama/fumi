#!/usr/bin/env python3
"""Photon inbox watcher — a cheap, non-LLM poller meant to run as a Monitor command.

It polls the Photon Worker for UNREAD mail addressed to THIS agent's identity and
prints one line per NEW message. Wired through Claude Code's Monitor tool, each of
those lines becomes an async event that wakes the agent — so the model is only
invoked when real mail actually arrives, never for empty polls.

Idle cost is a single authenticated GET every interval (default 30s) — featherweight.

Config resolution (so it "just works" in an agent's shell):
  - Worker URL / API key / machine / project: from env (BRIDGE_WORKER_URL,
    BRIDGE_API_KEY, BRIDGE_MACHINE_ID, BRIDGE_PROJECT) if set, else parsed from
    <CLAUDE_PROJECT_DIR>/.mcp.json (the same file that configures the Photon MCP).
  - Identity: PHOTON_WATCH_IDENTITY overrides everything (handy for tests); else
    it's <machine>/<project> plus any workstream persisted for this chat's
    CLAUDE_CODE_SESSION_ID in ~/.photon/claims.json — i.e. the exact identity the
    MCP server would use, including a restored lane.

Env knobs: PHOTON_WATCH_INTERVAL (seconds, default 30), PHOTON_WATCH_IDENTITY.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _session_runtime() -> dict:
    """Config the Photon MCP server published for this session (keyed by session id).

    This is the primary source: the agent's Bash shell has CLAUDE_CODE_SESSION_ID but
    not the BRIDGE_* env, so the MCP server drops ~/.photon/session-<id>.json for us.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not sid:
        return {}
    try:
        return json.loads((Path.home() / ".photon" / f"session-{sid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _mcp_photon_env() -> dict:
    """Fallback: the photon server's env from <CLAUDE_PROJECT_DIR|cwd>/.mcp.json."""
    proj_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        cfg = json.loads((Path(proj_dir) / ".mcp.json").read_text())
        return cfg.get("mcpServers", {}).get("photon", {}).get("env", {}) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_identity(machine: str, project: str | None) -> str:
    override = os.environ.get("PHOTON_WATCH_IDENTITY", "").strip()
    if override:
        return override
    base = f"{machine}/{project}" if project else machine
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if session_id:
        try:
            claims = json.loads((Path.home() / ".photon" / "claims.json").read_text())
            claim = claims.get(session_id)
            if claim and claim.get("project") == project and claim.get("machine") == machine:
                ws = claim.get("workstream")
                if ws:
                    return f"{base}#{ws}"
        except (OSError, json.JSONDecodeError):
            pass
    return base


# stdlib urllib's default UA ("Python-urllib/…") trips Cloudflare's bad-bot rule
# (HTTP 403, error 1010). A plain custom UA sails through.
USER_AGENT = "photon-watch/1.0"


def _fetch_unread(worker_url: str, api_key: str, identity: str) -> list:
    qs = urllib.parse.urlencode({"unread": "true", "identity": identity, "limit": "50"})
    req = urllib.request.Request(
        f"{worker_url}/messages?{qs}",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("messages", [])


def main() -> int:
    rt = _session_runtime()
    mcp_env = _mcp_photon_env()

    def pick(env_key: str, rt_key: str, default: str = "") -> str:
        # Priority: explicit env > MCP-published session file > project .mcp.json.
        return os.environ.get(env_key) or rt.get(rt_key) or mcp_env.get(env_key, default)

    worker_url = pick("BRIDGE_WORKER_URL", "worker_url").rstrip("/")
    api_key = pick("BRIDGE_API_KEY", "api_key")
    machine = pick("BRIDGE_MACHINE_ID", "machine", "unknown")
    project = pick("BRIDGE_PROJECT", "project") or None
    interval = int(os.environ.get("PHOTON_WATCH_INTERVAL", "30"))

    if not worker_url or not api_key:
        print("photon-watch: missing BRIDGE_WORKER_URL / BRIDGE_API_KEY "
              "(set env or provide <project>/.mcp.json)", flush=True)
        return 1

    identity = _resolve_identity(machine, project)
    print(f"photon-watch armed for {identity} (every {interval}s) — will emit only on new mail",
          flush=True)

    seen: set[str] = set()
    first_pass = True
    errors = 0
    while True:
        try:
            messages = _fetch_unread(worker_url, api_key, identity)
            errors = 0
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            errors += 1
            # Surface a broken watcher instead of failing silently (silence reads as
            # "no mail"). Announce the first failure and then roughly every ~5 min.
            if errors == 1 or errors % max(1, (300 // max(interval, 1))) == 0:
                reason = getattr(exc, "code", None) or type(exc).__name__
                print(f"⚠️ photon-watch: cannot reach Photon for {identity} ({reason}) — "
                      f"retrying every {interval}s", flush=True)
            time.sleep(interval)
            continue

        # On the very first pass, learn the current backlog WITHOUT emitting — the
        # agent already handles existing unread on its own; we only announce arrivals.
        if first_pass:
            seen = {m.get("id") for m in messages if m.get("id")}
            first_pass = False
            time.sleep(interval)
            continue

        for m in sorted(messages, key=lambda x: x.get("timestamp", "")):
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            frm = m.get("from", "?")
            tags = ",".join(m.get("tags", []) or [])
            preview = (m.get("content") or "").replace("\n", " ")[:160]
            tagstr = f" [{tags}]" if tags else ""
            print(f"📬 PHOTON: new message for {identity} from {frm}{tagstr} — {preview}",
                  flush=True)

        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

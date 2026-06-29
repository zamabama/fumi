# Report — Photon sub-project mailboxes (per-workstream read state + roster)

**WP:** `~/.claude/work-packages/WP-photon-subproject-mailboxes.md`
**Status:** ✅ Done. Worker deployed to `https://photon.zamtaranto-dev.workers.dev` (version `d5fff4e1`) and verified live end-to-end. MCP server changes activate per-agent on the next IDE reload / CLI session.

## What the problem was

Multiple agents at the same project root shared one Photon identity (`mac/<project>`) and one global per-message `read` flag. The first agent to read a broadcast marked it read for everyone, so every other same-project agent saw "no fresh messages."

## What changed

### 1. Read state is now per-recipient (the core fix)
- **Worker** (`worker/src/worker.js`): each message's global `read: bool` became `readBy: [identities]`. "Unread for identity X" = X isn't in `readBy`. `GET /messages` now takes an `identity` param and filters/marks-read relative to it, so reading as `mac/x#website` never clears `mac/x#crypto`'s copy. `POST /messages/:id/read` is also identity-scoped.
- Broadcast fan-out is now **implicit**: a never-before-seen identity has read nothing, so it sees all matching messages fresh — no roster of recipients to maintain.

### 2. Workstream sub-identities
- **Addressing:** `mac/<project>#<workstream>` (e.g. `mac/primogen#website`). Set per agent at runtime via the new `set_identity` tool, or seeded from a `PHOTON_WORKSTREAM` env var. (An env var alone can't separate two agents at the *same* root — they share one `.mcp.json` — so the sub-identity is settable in-process, which is per-agent because each chat/CLI session gets its own MCP subprocess. Verified: 29 distinct subprocesses were running live during implementation.)
- **Visibility:** a message to `mac/<project>#website` reaches only that lane; a broadcast (or a cross-project send to the bare `mac/<project>`) reaches every lane in the project, each reading independently.

### 3. Roster / presence (so agents self-organise)
- Agents **register** their identity on `set_identity` and refresh it via a throttled (60s) best-effort heartbeat on send/read/check. Records live in a per-project map on the Worker (`POST/GET /presence`) and are pruned after 15 min of inactivity.
- New **`list_identities(project=…)`** tool returns the live roster (identity, workstream, description, "seconds ago") for your own or another project.
- **Collision protection:** claiming a `#workstream` already held by another live agent is refused (HTTP 409), so two agents can't silently land on the same cursor. Base identities (no `#`) stay shareable (legacy single-mailbox mode).

## How to use it

Single agent / cross-project messaging: **nothing changes** — no workstream = the normal project mailbox, exactly as before.

Several agents on one project:
1. `list_identities()` — see what's taken.
2. `set_identity("website", description="landing redesign")` — claim a free lane (auto-declared from your task; only ask the user if ambiguous).
3. Send to a specific lane with `target="mac/<project>#<workstream>"` (find it via `list_identities`, cross-project too).

## Migration

- **No data migration needed.** Legacy messages with `read: true` are treated as read-by-everyone (`readBy` wildcard `*`) so nothing resurfaces; `read: false` stays unread. Old clients that call without an `identity` fall back to the previous global semantics.
- **Backward compatible:** existing single-agent and cross-project flows are unchanged; `set_identity` / `list_identities` are additive; all existing tool signatures gained only optional params.
- **Deploy required:** the Worker change must be pushed (`cd worker && npm install && npx wrangler deploy`). The MCP server change is picked up when each agent's MCP subprocess restarts (reload IDE window / new CLI session).

## Testing

32 checks, all passing — run them with:
- `node /tmp/photon_worker_test.mjs` — per-recipient read state + legacy migration (9)
- `node /tmp/photon_presence_test.mjs` — register / collision / roster / TTL prune (9)
- `.venv/bin/python3 /tmp/photon_visibility_test.py` — identity resolution + visibility + set_identity (14)

(The two Node suites import the real `worker.fetch` against an in-memory KV stub; no deploy needed to test.)

## Files changed
- `worker/src/worker.js` — readBy, identity-scoped list/mark, `/presence` register+list+TTL.
- `mcp_server.py` — workstream identity, `set_identity`, `list_identities`, heartbeat, updated tool docs + server instructions.

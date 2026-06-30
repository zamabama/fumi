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

## Follow-up — mis-addressed mail (MCP-only, no redeploy)

A usability gap surfaced in real use: a message sent to the *wrong* lane (`to mac/primogen#crypto`) was invisible to every other lane, retrievable only by an agent temporarily impersonating that identity (`set_identity` there and back). Root cause: visibility conflated "addressed to" with "only visible to." Fixed at all three stages, entirely in `mcp_server.py` (the Worker already had the needed data):

1. **Prevent — send-time dead-lane guard.** `send_message` to a `#workstream` target that isn't in the live roster is held (not sent) and returns the roster + same-project lane suggestions. Force with `allow_unregistered=true`.
2. **Discover — `check_messages.other_lanes_waiting`.** Reports mail addressed to another workstream in your project that the intended lane hasn't read yet (so "check photon" surfaces a likely mis-target instead of "nothing fresh").
3. **Retrieve — `read_messages(as_recipient='mac/<project>#<lane>')`.** Reads/handles another lane's mail in one call **without changing your own identity** (no impersonation dance). Defaults to consuming that lane's copy; `keep_unread=true` to peek.

Internals: `_visible_to_me` generalised to `_visible_to(msg, identity)`; added `_parse_identity` and `_fetch_roster`.

## Follow-up 2 — default isolation (regression fix)

The first follow-up over-corrected: surfacing `other_lanes_waiting` in `check_messages` and nudging agents to retrieve other lanes' mail made agents snoop — and in one case auto-start work that wasn't theirs. Corrected to **strict default isolation**:

- **Server-side visibility (Worker, deployed).** `GET /messages` with an `identity` now only returns mail addressed to that identity (or its project's broadcasts); `all_lanes=true` opts out. Enforced on the Worker, so it takes effect for already-running agents with no reload, and misdirected mail is never even transmitted to the wrong agent.
- **`check_messages` returns only your mail** — `other_lanes_waiting` removed entirely.
- **Cross-lane reads are user-directed only.** `read_messages(as_recipient=…)` (one lane) and the new `read_messages(include_all_lanes=true)` (search all lanes, read-only) exist for "the user told me to look" / "a message should be here but isn't." Server instructions now say emphatically: stay in your lane; act only on what's addressed to you.
- The send-time dead-lane guard is unchanged (still the best prevention).

## Follow-up 3 — lane persistence across MCP restarts (MCP-only, no redeploy)

Implements `WP-photon-identity-persistence.md`. Before this, `set_identity` stored the lane in-process only, so any MCP subprocess restart (IDE reload / reconnect / idle recycle) silently dropped the agent back to base `mac/primogen` — and lane-targeted mail then failed to deliver (the base identity doesn't receive lane mail). The roster decayed to base within minutes, forcing broadcast-everything.

**Crux (the WP flagged this as solve-first): is a stable per-session key exposed to the MCP subprocess?** Yes — `CLAUDE_CODE_SESSION_ID` is in the subprocess launch env, unique per chat, and durable across restarts (verified: each id has persisted state under `~/.claude/session-env/<id>` and `~/.claude/file-history/<id>`, i.e. it's the resumable session identity, not a per-process value).

**Fix (in `mcp_server.py`):**
- `set_identity(workstream)` now persists `{session_id → workstream/description/project/machine}` to `~/.photon/claims.json` (machine-local, like the session id); `set_identity('')` removes it.
- At subprocess boot, `_restore_claim()` reads `CLAUDE_CODE_SESSION_ID` and silently re-claims the stored lane (only if project + machine match), so a reload restores `#crypto` instead of dropping to base — no manual re-call. The first activity heartbeat re-registers it in the roster.
- An explicit `PHOTON_WORKSTREAM` env still wins; never-claimed agents stay on base; collision rules unchanged.

**Acceptance test (needs a real reload, for you to run):** claim a lane → reload the IDE window → `list_identities` shows you still on your lane, and a lane-targeted send arrives — with no manual re-claim.

## Migration

- **No data migration needed.** Legacy messages with `read: true` are treated as read-by-everyone (`readBy` wildcard `*`) so nothing resurfaces; `read: false` stays unread. Old clients that call without an `identity` fall back to the previous global semantics.
- **Backward compatible:** existing single-agent and cross-project flows are unchanged; `set_identity` / `list_identities` are additive; all existing tool signatures gained only optional params.
- **Deploy required:** the Worker change must be pushed (`cd worker && npm install && npx wrangler deploy`). The MCP server change is picked up when each agent's MCP subprocess restarts (reload IDE window / new CLI session).

## Testing

51 checks, all passing — run them with:
- `node /tmp/photon_worker_test.mjs` — per-recipient read state + legacy migration (9)
- `node /tmp/photon_presence_test.mjs` — register / collision / roster / TTL prune (9)
- `.venv/bin/python3 /tmp/photon_visibility_test.py` — identity resolution + visibility + set_identity (14)
- `.venv/bin/python3 /tmp/photon_misdirect_test.py` — send guard / other_lanes_waiting / as_recipient retrieval (19)

(The two Node suites import the real `worker.fetch` against an in-memory KV stub; the Python suites use the pure helpers / a fake httpx layer. No deploy needed to test.)

## Files changed
- `worker/src/worker.js` — readBy, identity-scoped list/mark, `/presence` register+list+TTL.
- `mcp_server.py` — workstream identity, `set_identity`, `list_identities`, heartbeat, updated tool docs + server instructions.

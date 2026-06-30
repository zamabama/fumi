# Photon — Message Bridge for Claude Code

Two-way message relay between [Claude Code](https://docs.anthropic.com/en/docs/claude-code) instances on different machines. Send handovers, task updates, and coordination messages between any number of machines through a Cloudflare Worker.

## Why

If you use Claude Code on multiple machines (e.g. a Mac laptop and a Windows desktop), there's no built-in way for agents to communicate across machines. Photon bridges that gap — agents can leave messages for each other, hand off work context, and coordinate tasks across your setup.

**Use cases:**
- **Cross-machine handovers** — finish work on your laptop, send context to your desktop agent
- **Task coordination** — direct agents on different machines from one place
- **Multi-agent messaging** — tag and filter messages by project, machine, or topic
- **Session continuity** — leave notes for your next session on any machine

## Architecture

```
Machine A (Claude Code)                    Machine B (Claude Code)
     |                                            |
MCP server (stdio)                         MCP server (stdio)
     |                                            |
     +-----------> Cloudflare Worker <-------------+
                  (KV message store)
```

Both machines run the same MCP server. Each identifies itself via `BRIDGE_MACHINE_ID` env var. Messages are stored in Cloudflare KV and accessed over HTTPS with a shared API key.

## Prerequisites

- A [Cloudflare account](https://dash.cloudflare.com/sign-up) (free tier works fine)
- [Node.js](https://nodejs.org/) (for deploying the Worker)
- Python 3.10+ (for the MCP server)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed on your machines

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/zamabama/photon.git
cd photon
```

### 2. Deploy the Cloudflare Worker

The Worker is the message relay that both machines talk to. Free tier is more than enough.

```bash
cd worker
npm install

# Create a KV namespace for message storage
npx wrangler kv namespace create MESSAGES
# This outputs a namespace ID — copy it into wrangler.toml

# Set your shared API key (any random string — both machines need the same one)
npx wrangler secret put BRIDGE_API_KEY

# Deploy
npx wrangler deploy
```

After deploying, note your Worker URL (e.g. `https://photon.<your-account>.workers.dev`).

### 3. Install Python dependencies

```bash
pip install mcp httpx
```

### 4. Add to Claude Code

On each machine, add Photon to your project's `.mcp.json` (or create one):

```json
{
  "mcpServers": {
    "photon": {
      "command": "python3",
      "args": ["/path/to/photon/mcp_server.py"],
      "env": {
        "BRIDGE_WORKER_URL": "https://photon.<your-account>.workers.dev",
        "BRIDGE_API_KEY": "<your-shared-secret>",
        "BRIDGE_MACHINE_ID": "mac"
      }
    }
  }
}
```

**Configuration:**
| Variable | Description |
|----------|-------------|
| `BRIDGE_WORKER_URL` | Your deployed Worker URL |
| `BRIDGE_API_KEY` | Shared secret (same on all machines) |
| `BRIDGE_MACHINE_ID` | Unique identifier for this machine (e.g. `"mac"`, `"pc"`, `"work-laptop"`) |
| `BRIDGE_PROJECT` | *(Optional)* Project name for filtering messages across projects |
| `PHOTON_WORKSTREAM` | *(Optional)* Default workstream sub-identity for this agent (see [Workstreams](#workstreams--multiple-agents-on-one-project)). Usually set at runtime via `set_identity` instead. |

**Windows note:** Use `"python"` instead of `"python3"` for the command.

**Multi-project setup:** You can add Photon to multiple projects on the same machine. Set different `BRIDGE_PROJECT` values to filter messages per project, or leave it empty for global messages.

### 5. Add to CLAUDE.md (recommended)

Add this to your project's `CLAUDE.md` so agents know to check messages:

```markdown
## Photon — Message Bridge

Check photon at the start of every session:
- Use `check_messages` to see unread count
- Use `read_messages(unread_only=true)` to read pending messages
- Act on any task direction or handover notes
- When finishing a session, send a handover summary via `send_message`
```

## MCP Tools

Once configured, Claude Code gets these tools:

| Tool | Description |
|------|-------------|
| `check_messages` | Quick check for **fresh** unread (default last 10 min). Only ever returns mail addressed to you. Reports older backlog separately. |
| `read_messages` | Read messages addressed to you. Defaults to fresh unread + auto-marks read. `as_recipient=` / `include_all_lanes=` look beyond your lane **only when the user directs you to**. |
| `send_message` | Send a message with optional tags. Refuses cross-project sends without `target=`, and holds sends to a `#workstream` no live agent is using (`allow_unregistered=true` to force). |
| `set_identity` | Claim a workstream sub-identity (e.g. `"website"`) so this agent gets its own independent read cursor and a roster entry. |
| `list_identities` | List the agents/workstreams currently active on a project (the roster). Call before `set_identity` or before targeting a specific lane. |
| `resolve_project` | Scan `~/dev` and `~/Documents` for sibling Photon projects. Call before any cross-project send. |
| `mark_read` | Mark a specific message as read by ID. |
| `clear_messages` | Delete all messages (requires `confirm=true` safety check). |

### Recency-first reading

`check_messages` and `read_messages` default to the last 10 minutes of unread mail. The reasoning: in practice, "check photon" almost always means *the message I just sent* — old "unread" backlog is noise, not signal. Older unread is reported as a separate `older_unread_count` field so it isn't confused with new mail. Pass `max_age_minutes=` or `since=` to look further back.

### Cross-project sends

If you send a message tagged with another project's name but forget `target=`, the server refuses the send (since the message would be silently scoped to your own project and the other agent would never see it). Call `resolve_project(hint='<project>')` first — it returns the correct composite identity to use as `target=`, and warns if the same identity is shared by multiple project directories (the "everyone is `mac/global`" trap).

### Workstreams — multiple agents on one project

By default, every agent opened on the same project root shares one identity (`<machine>/<project>`) and one mailbox. That's fine for a single agent, but if you run several chats/agents on the same project, the first one to read a broadcast marks it read for everyone else.

**Workstreams** fix this. Each agent claims a sub-identity — `<machine>/<project>#<workstream>`, e.g. `mac/primogen#website` — that has its **own independent read cursor**. One agent reading a message never clears it for the others, and a broadcast is seen fresh by every lane.

```
# At session start, when several agents share a project:
list_identities()                              # see who's already active / what's taken
set_identity("website", description="landing redesign")   # claim a free lane
```

- **Read independence** — `#website` reading mail never affects `#crypto`'s unread state.
- **Roster** — `set_identity` registers you; agents refresh on activity and drop off after 15 min idle. `list_identities(project="X")` shows the live lanes for your own or another project.
- **Collision-safe** — claiming a `#workstream` another live agent already holds is refused, so two agents can't silently land on the same cursor. (Base identities without `#` stay shareable — the original single-mailbox behaviour.)
- **Targeting a lane** — `send_message(target="mac/primogen#website")` reaches just that lane. Cross-project too: `list_identities(project="cocobun")` then target the identity it returns.
- **Backward compatible** — no workstream means the plain project mailbox, exactly as before.

### Stay in your lane (default isolation)

**An agent only ever receives mail addressed to it** — its own identity, or its project's broadcasts. This is enforced **server-side**: the Worker never transmits another lane's message to you, so an agent can't read, surface, or act on work meant for a different workstream. `check_messages` and `read_messages` show *your* mail and nothing else. This keeps a master → N-workers fan-out clean: each worker sees only its own task.

> To give one specific agent a task, **target it** — `send_message(target="mac/primogen#website")`. Don't broadcast per-agent tasks; a broadcast is seen by every lane on purpose.

### Looking beyond your lane (user-directed only)

Sometimes you *do* need to reach across lanes — e.g. a message was mis-sent to the wrong one. These are deliberate, user-directed escape hatches; agents should not use them on their own initiative:

- **Prevent it up front** — `send_message` to a `#workstream` no live agent is using is **held, not sent**, and returns the roster so you can pick the right lane. Override with `allow_unregistered=true`.
- **Read one specific lane** — `read_messages(as_recipient="mac/primogen#crypto")` reads that lane's mail **without changing your own identity** (no `set_identity`-there-and-back). Consumes that lane's copy by default; `keep_unread=true` to peek.
- **Search every lane** — `read_messages(include_all_lanes=true)` returns all lanes in your project, read-only, for when "there should be a message for me but I can't find it." Check each message's `to` before acting.

### Example usage in conversation

```
You: "Check photon for any messages from my PC"
Agent: [calls check_messages] → "2 unread messages from pc"
Agent: [calls read_messages(unread_only=true)] → shows messages

You: "Send a handover to my PC about where we left off"
Agent: [calls send_message with context summary]
```

## Worker API Reference

All endpoints require `Authorization: Bearer <key>` header except `/health`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (no auth required) |
| `POST` | `/messages` | Send a message |
| `GET` | `/messages` | List messages |
| `POST` | `/messages/:id/read` | Mark message as read (scoped to `?identity=`) |
| `DELETE` | `/messages/:id` | Delete a message |
| `DELETE` | `/messages?confirm=true` | Delete all messages |
| `POST` | `/presence` | Register / refresh an identity in the roster |
| `GET` | `/presence?project=` | List active identities for a project |

### Query parameters for `GET /messages`

| Parameter | Description |
|-----------|-------------|
| `unread` | `"true"` to return only unread messages |
| `identity` | Caller's identity — scopes both **visibility** (only mail addressed to this identity or its project's broadcasts is returned) and `unread`/`mark_read` (per-recipient read state). Falls back to legacy global behaviour if omitted. |
| `all_lanes` | `"true"` to bypass the visibility filter and return every lane's mail (for a user-directed cross-lane search). |
| `mark_read` | `"true"` to mark the returned messages read for `identity` in the same call |
| `from` | Filter by sender machine ID |
| `project` | Filter by project name |
| `tag` | Filter by tag |
| `limit` | Max messages to return (default 50) |
| `since` | ISO timestamp — only messages after this time |

### Message format

```json
{
  "id": "uuid",
  "from": "mac/my-project#website",
  "to": null,
  "project": "my-project",
  "timestamp": "2026-02-28T12:00:00.000Z",
  "content": "Finished the auth refactor. Tests passing. Ready for review.",
  "tags": ["handover", "auth"],
  "readBy": ["mac/my-project#website"]
}
```

`readBy` lists the identities that have read the message — read state is **per-recipient**, not a single global flag, so each workstream clears its own copy independently. (`to` is the target identity for a directed send, or `null` for a project broadcast. Messages from before this change used a single `read: true/false` boolean; those are treated as read-by-everyone.)

## File Structure

```
photon/
├── mcp_server.py        ← MCP server (Python, stdio transport)
├── requirements.txt     ← Python dependencies (mcp, httpx)
├── README.md
├── REPORT_subproject_mailboxes.md  ← design notes: workstreams, roster, mis-addressed mail
└── worker/
    ├── wrangler.toml    ← Cloudflare Worker config
    ├── package.json
    └── src/
        └── worker.js    ← Cloudflare Worker (message relay)
```

## Cost

Cloudflare Workers free tier includes 100,000 requests/day and 1GB KV storage. For typical Claude Code usage (a few hundred messages per day at most), you'll never come close to these limits. **Photon costs nothing to run.**

## License

MIT

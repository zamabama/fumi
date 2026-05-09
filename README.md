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
| `check_messages` | Quick check for **fresh** unread (default last 10 min). Reports older backlog separately. |
| `read_messages` | Read messages, auto-filtered to your identity. Defaults to fresh unread + auto-marks read. |
| `send_message` | Send a message with optional tags. Refuses cross-project sends without `target=`. |
| `resolve_project` | Scan `~/dev` and `~/Documents` for sibling Photon projects. Call before any cross-project send. |
| `mark_read` | Mark a specific message as read by ID. |
| `clear_messages` | Delete all messages (requires `confirm=true` safety check). |

### Recency-first reading

`check_messages` and `read_messages` default to the last 10 minutes of unread mail. The reasoning: in practice, "check photon" almost always means *the message I just sent* — old "unread" backlog is noise, not signal. Older unread is reported as a separate `older_unread_count` field so it isn't confused with new mail. Pass `max_age_minutes=` or `since=` to look further back.

### Cross-project sends

If you send a message tagged with another project's name but forget `target=`, the server refuses the send (since the message would be silently scoped to your own project and the other agent would never see it). Call `resolve_project(hint='<project>')` first — it returns the correct composite identity to use as `target=`, and warns if the same identity is shared by multiple project directories (the "everyone is `mac/global`" trap).

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
| `POST` | `/messages/:id/read` | Mark message as read |
| `DELETE` | `/messages/:id` | Delete a message |
| `DELETE` | `/messages?confirm=true` | Delete all messages |

### Query parameters for `GET /messages`

| Parameter | Description |
|-----------|-------------|
| `unread` | `"true"` to return only unread messages |
| `from` | Filter by sender machine ID |
| `project` | Filter by project name |
| `tag` | Filter by tag |
| `limit` | Max messages to return (default 50) |
| `since` | ISO timestamp — only messages after this time |

### Message format

```json
{
  "id": "uuid",
  "from": "mac",
  "project": "my-project",
  "timestamp": "2026-02-28T12:00:00.000Z",
  "content": "Finished the auth refactor. Tests passing. Ready for review.",
  "tags": ["handover", "auth"],
  "read": false
}
```

## File Structure

```
photon/
├── mcp_server.py        ← MCP server (Python, stdio transport)
├── requirements.txt     ← Python dependencies (mcp, httpx)
├── README.md
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

# Claude Bridge

Two-way message relay between Claude Code instances on different machines. Send handovers, task direction, and status updates between Mac and PC (or any two machines) through a Cloudflare Worker.

## Architecture

```
Machine A (Claude Code)                    Machine B (Claude Code)
     |                                            |
MCP server (stdio)                         MCP server (stdio)
     |                                            |
     +-----------> Cloudflare Worker <-------------+
                  (KV message store)
```

Both machines run the same MCP server. Each identifies itself via `BRIDGE_MACHINE_ID` env var. Messages are stored in Cloudflare KV and accessed over HTTPS.

## Setup

### 1. Deploy the Cloudflare Worker

```bash
cd worker
npm install

# Create KV namespace
npx wrangler kv namespace create MESSAGES
# Copy the namespace ID into wrangler.toml

# Set your shared API key
npx wrangler secret put BRIDGE_API_KEY

# Deploy
npx wrangler deploy
```

### 2. Install Python dependencies

```bash
pip install mcp httpx
```

### 3. Add to .mcp.json

On each machine, add to the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "claude-bridge": {
      "command": "python3",
      "args": ["/path/to/claude-bridge/mcp_server.py"],
      "env": {
        "BRIDGE_WORKER_URL": "https://claude-bridge.<account>.workers.dev",
        "BRIDGE_API_KEY": "<your-shared-secret>",
        "BRIDGE_MACHINE_ID": "mac"
      }
    }
  }
}
```

Set `BRIDGE_MACHINE_ID` to `"mac"`, `"pc"`, or any identifier for that machine. On Windows, use `"python"` instead of `"python3"`.

## MCP Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a message (content + optional tags) |
| `read_messages` | Read messages with optional filters (unread, sender, tag, limit) |
| `check_messages` | Quick unread count — call at session start |
| `mark_read` | Mark a message as read by ID |
| `clear_messages` | Delete all messages (requires confirm=true) |

## CLAUDE.md Integration

Add to your project's CLAUDE.md on both machines:

```markdown
## Mac-PC Bridge

Check for messages at the start of every session:
- Use `check_messages` to see unread count
- Use `read_messages(unread_only=true)` to read pending messages
- Act on any task direction or handover notes
- When finishing a session, send a handover summary via `send_message`
```

## Worker API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health check (no auth) |
| `POST` | `/messages` | Send a message |
| `GET` | `/messages` | List messages (query: `unread`, `from`, `tag`, `limit`, `since`) |
| `POST` | `/messages/:id/read` | Mark as read |
| `DELETE` | `/messages/:id` | Delete one |
| `DELETE` | `/messages?confirm=true` | Clear all |

Auth: `Authorization: Bearer <key>` header on all endpoints except `/health`.

## File Structure

```
claude-bridge/
├── mcp_server.py        ← MCP server (Python, stdio)
├── requirements.txt     ← Python dependencies
├── README.md            ← This file
└── worker/
    ├── wrangler.toml    ← Cloudflare Worker config
    ├── package.json     ← npm config
    └── src/
        └── worker.js    ← Cloudflare Worker script
```

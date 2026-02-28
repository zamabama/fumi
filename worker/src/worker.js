/**
 * Fumi (文) — Cloudflare Worker
 *
 * Message relay between Claude Code instances on different machines.
 * Messages stored in Cloudflare KV. Auth via shared API key.
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // Health check — no auth
    if (path === "/health" && method === "GET") {
      return json({ status: "ok", timestamp: new Date().toISOString() });
    }

    // CORS preflight
    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    // Auth check for all other endpoints
    const authHeader = request.headers.get("Authorization") || "";
    const token = authHeader.replace("Bearer ", "");
    if (!token || token !== env.BRIDGE_API_KEY) {
      return json({ error: "Unauthorized" }, 401);
    }

    // Route
    try {
      // POST /messages — send a message
      if (path === "/messages" && method === "POST") {
        return await handleSend(request, env);
      }

      // GET /messages — list messages
      if (path === "/messages" && method === "GET") {
        return await handleList(url, env);
      }

      // DELETE /messages — clear all
      if (path === "/messages" && method === "DELETE") {
        if (url.searchParams.get("confirm") !== "true") {
          return json({ error: "Pass ?confirm=true to clear all messages" }, 400);
        }
        return await handleClearAll(env);
      }

      // POST /messages/:id/read — mark read
      const markReadMatch = path.match(/^\/messages\/([^/]+)\/read$/);
      if (markReadMatch && method === "POST") {
        return await handleMarkRead(markReadMatch[1], env);
      }

      // DELETE /messages/:id — delete one
      const deleteMatch = path.match(/^\/messages\/([^/]+)$/);
      if (deleteMatch && method === "DELETE") {
        return await handleDelete(deleteMatch[1], env);
      }

      return json({ error: "Not found" }, 404);
    } catch (err) {
      return json({ error: err.message }, 500);
    }
  },
};

// --- Handlers ---

async function handleSend(request, env) {
  const body = await request.json();
  const { content, from, tags, project } = body;

  if (!content || !from) {
    return json({ error: "content and from are required" }, 400);
  }

  const id = crypto.randomUUID();
  const timestamp = new Date().toISOString();

  const message = { id, from, project: project || null, timestamp, content, tags: tags || [], read: false };

  // Store the message
  await env.MESSAGES.put(`msg:${id}`, JSON.stringify(message));

  // Update index
  const index = await getIndex(env);
  index.push({ id, timestamp, from, project: project || null, read: false });
  await env.MESSAGES.put("index", JSON.stringify(index));

  return json({ id, timestamp, status: "sent" }, 201);
}

async function handleList(url, env) {
  const unreadOnly = url.searchParams.get("unread") === "true";
  const fromFilter = url.searchParams.get("from");
  const tagFilter = url.searchParams.get("tag");
  const projectFilter = url.searchParams.get("project");
  const limit = parseInt(url.searchParams.get("limit") || "50", 10);
  const since = url.searchParams.get("since");

  let index = await getIndex(env);

  // Filter index
  if (unreadOnly) index = index.filter((e) => !e.read);
  if (fromFilter) index = index.filter((e) => e.from === fromFilter);
  if (projectFilter) index = index.filter((e) => e.project === projectFilter);
  if (since) index = index.filter((e) => e.timestamp > since);

  // Most recent first, apply limit
  index = index.sort((a, b) => b.timestamp.localeCompare(a.timestamp)).slice(0, limit);

  // Fetch full messages
  const messages = await Promise.all(
    index.map(async (entry) => {
      const raw = await env.MESSAGES.get(`msg:${entry.id}`);
      return raw ? JSON.parse(raw) : null;
    })
  );

  let results = messages.filter(Boolean);

  // Tag filter requires full message body
  if (tagFilter) {
    results = results.filter((m) => m.tags && m.tags.includes(tagFilter));
  }

  return json({ messages: results, count: results.length });
}

async function handleMarkRead(id, env) {
  const raw = await env.MESSAGES.get(`msg:${id}`);
  if (!raw) return json({ error: "Message not found" }, 404);

  const message = JSON.parse(raw);
  message.read = true;
  await env.MESSAGES.put(`msg:${id}`, JSON.stringify(message));

  // Update index
  const index = await getIndex(env);
  const entry = index.find((e) => e.id === id);
  if (entry) {
    entry.read = true;
    await env.MESSAGES.put("index", JSON.stringify(index));
  }

  return json({ id, status: "marked_read" });
}

async function handleDelete(id, env) {
  await env.MESSAGES.delete(`msg:${id}`);

  const index = await getIndex(env);
  const filtered = index.filter((e) => e.id !== id);
  await env.MESSAGES.put("index", JSON.stringify(filtered));

  return json({ id, status: "deleted" });
}

async function handleClearAll(env) {
  const index = await getIndex(env);
  await Promise.all(index.map((e) => env.MESSAGES.delete(`msg:${e.id}`)));
  await env.MESSAGES.delete("index");

  return json({ status: "cleared", count: index.length });
}

// --- Helpers ---

async function getIndex(env) {
  const raw = await env.MESSAGES.get("index");
  return raw ? JSON.parse(raw) : [];
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
  };
}

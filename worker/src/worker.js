/**
 * Photon — Cloudflare Worker
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

      // POST /presence — register / heartbeat this identity
      if (path === "/presence" && method === "POST") {
        return await handleRegister(request, env);
      }

      // GET /presence — list active identities for a project
      if (path === "/presence" && method === "GET") {
        return await handleListPresence(url, env);
      }

      // POST /messages/:id/read — mark read
      const markReadMatch = path.match(/^\/messages\/([^/]+)\/read$/);
      if (markReadMatch && method === "POST") {
        return await handleMarkRead(markReadMatch[1], env, url.searchParams.get("identity"));
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
  const { content, from, tags, project, to } = body;

  if (!content || !from) {
    return json({ error: "content and from are required" }, 400);
  }

  const id = crypto.randomUUID();
  const timestamp = new Date().toISOString();

  const message = {
    id, from, to: to || null, project: project || null,
    timestamp, content, tags: tags || [], readBy: [],
  };

  // Store the message
  await env.MESSAGES.put(`msg:${id}`, JSON.stringify(message));

  // Update index
  const index = await getIndex(env);
  index.push({ id, timestamp, from, to: to || null, project: project || null, readBy: [] });
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
  const markRead = url.searchParams.get("mark_read") === "true";
  // Read state is per-recipient. `identity` is the caller's full identity
  // (e.g. "mac/primogen#website"). Unread = this identity isn't in readBy yet,
  // so reading as #website never clears #crypto's copy.
  const identity = url.searchParams.get("identity");
  // Visibility is enforced HERE, server-side: with an identity set, callers only
  // ever receive mail addressed to them (or their project's broadcasts). Mail for
  // a different lane is never transmitted, so an agent can't read or even see it.
  // `all_lanes=true` opts out (explicit, user-directed "search everywhere").
  const allLanes = url.searchParams.get("all_lanes") === "true";

  let index = await getIndex(env);

  // Filter index
  if (identity && !allLanes) index = index.filter((e) => visibleTo(e, identity));
  if (unreadOnly) index = index.filter((e) => !isReadBy(e, identity));
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

  // Batch mark-read for THIS identity only: add `identity` to each returned
  // message's readBy. Done after tag filtering so we only mark messages the
  // caller actually saw. Without an identity (legacy/debug caller) we skip —
  // there is no single recipient to attribute the read to.
  if (markRead && identity && results.length > 0) {
    const idsToMark = new Set(results.map((m) => m.id));
    const writes = [];
    for (const m of results) {
      const readBy = ensureReadBy(m);
      if (!readBy.includes(identity)) {
        readBy.push(identity);
        m.readBy = readBy;
        writes.push(env.MESSAGES.put(`msg:${m.id}`, JSON.stringify(m)));
      }
    }
    // Mirror into the index entries (used for the fast unread filter above).
    const fullIndex = await getIndex(env);
    let indexDirty = false;
    for (const entry of fullIndex) {
      if (idsToMark.has(entry.id)) {
        const readBy = ensureReadBy(entry);
        if (!readBy.includes(identity)) {
          readBy.push(identity);
          entry.readBy = readBy;
          indexDirty = true;
        }
      }
    }
    if (indexDirty) writes.push(env.MESSAGES.put("index", JSON.stringify(fullIndex)));
    await Promise.all(writes);
  }

  return json({ messages: results, count: results.length });
}

async function handleMarkRead(id, env, identity) {
  const raw = await env.MESSAGES.get(`msg:${id}`);
  if (!raw) return json({ error: "Message not found" }, 404);

  const message = JSON.parse(raw);
  addReader(message, identity);
  await env.MESSAGES.put(`msg:${id}`, JSON.stringify(message));

  // Mirror into the index
  const index = await getIndex(env);
  const entry = index.find((e) => e.id === id);
  if (entry) {
    addReader(entry, identity);
    await env.MESSAGES.put("index", JSON.stringify(index));
  }

  return json({ id, status: "marked_read", identity: identity || null });
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

// --- Presence / roster ---
//
// Each agent registers its identity (and refreshes it on activity). Records are
// kept in a per-project map and pruned once they go stale, so list_identities
// shows only the agents currently working a project. A per-agent `token`
// distinguishes "me refreshing" from "a different agent claimed my label".

const PRESENCE_TTL_MS = 15 * 60 * 1000;

function nowMs() {
  return Date.now();
}

function presenceKey(project) {
  return `presence:${project || "_none"}`;
}

async function getPresence(env, project) {
  const raw = await env.MESSAGES.get(presenceKey(project));
  return raw ? JSON.parse(raw) : {};
}

function pruneStale(map, now) {
  for (const [k, v] of Object.entries(map)) {
    if (now - (v.last_seen || 0) >= PRESENCE_TTL_MS) delete map[k];
  }
}

async function handleRegister(request, env) {
  const body = await request.json();
  const { project, identity, workstream, description, token } = body;
  if (!identity) return json({ error: "identity is required" }, 400);

  const now = nowMs();
  const map = await getPresence(env, project);
  const existing = map[identity];

  // Collision: a #workstream sub-identity held by a DIFFERENT, still-live agent.
  // Don't steal the slot — report it so the newcomer picks another workstream.
  // Base identities (no '#') are intentionally shareable (legacy single mailbox),
  // so they never collide.
  if (
    identity.includes("#") &&
    existing && existing.token && token && existing.token !== token &&
    now - (existing.last_seen || 0) < PRESENCE_TTL_MS
  ) {
    return json({
      status: "collision",
      identity,
      collision: {
        held_by_other: true,
        last_seen: new Date(existing.last_seen).toISOString(),
        description: existing.description || null,
      },
    }, 409);
  }

  map[identity] = {
    identity,
    workstream: workstream || null,
    description: description || null,
    token: token || null,
    last_seen: now,
  };
  pruneStale(map, now);

  await env.MESSAGES.put(presenceKey(project), JSON.stringify(map));
  return json({ status: "registered", identity });
}

async function handleListPresence(url, env) {
  const project = url.searchParams.get("project");
  const now = nowMs();
  const map = await getPresence(env, project);

  const identities = Object.values(map)
    .filter((v) => now - (v.last_seen || 0) < PRESENCE_TTL_MS)
    .map((v) => ({
      identity: v.identity,
      workstream: v.workstream,
      description: v.description,
      last_seen: new Date(v.last_seen).toISOString(),
      seconds_ago: Math.round((now - v.last_seen) / 1000),
    }))
    .sort((a, b) => a.seconds_ago - b.seconds_ago);

  return json({ project: project || null, count: identities.length, identities });
}

// --- Helpers ---

async function getIndex(env) {
  const raw = await env.MESSAGES.get("index");
  return raw ? JSON.parse(raw) : [];
}

// --- Per-recipient read state ---
//
// Each message/index entry carries `readBy`: the list of identities that have
// read it. "*" is a wildcard meaning read-by-everyone, used to migrate legacy
// messages that only had a global `read: true` boolean.

const READ_ALL = "*";

// Return the entry's readBy array, migrating a legacy `read` boolean on the fly.
// A legacy read:true becomes ["*"] (read by all); read:false/absent becomes [].
function ensureReadBy(obj) {
  if (Array.isArray(obj.readBy)) return obj.readBy;
  return obj.read === true ? [READ_ALL] : [];
}

// Has this identity already read the entry? With no identity (legacy/debug
// caller) fall back to "read by anyone", preserving the old global semantics.
function isReadBy(entry, identity) {
  const readBy = ensureReadBy(entry);
  if (!identity) return readBy.length > 0;
  return readBy.includes(READ_ALL) || readBy.includes(identity);
}

// Record that `identity` has read the entry (mutates in place). With no
// identity, mark it read-by-all so an old client still clears it for everyone.
function addReader(entry, identity) {
  const readBy = ensureReadBy(entry);
  const marker = identity || READ_ALL;
  if (!readBy.includes(marker)) readBy.push(marker);
  entry.readBy = readBy;
}

// --- Visibility ---
//
// Mirrors the client's _visible_to: a message is visible to an identity if it is
// addressed to that exact identity, to that identity's project base (e.g. a
// cross-project send to "mac/primogen" reaches every lane), or is a broadcast in
// that identity's project. Enforced server-side so misdirected mail never leaks.

function parseIdentity(identity) {
  const base = identity.split("#")[0];
  const project = base.includes("/") ? base.split("/").slice(1).join("/") : null;
  return { base, project };
}

function visibleTo(entry, identity) {
  const { base, project } = parseIdentity(identity);
  const to = entry.to ?? null;
  if (to !== null) return to === identity || to === base;
  // broadcast (to === null): project-scoped
  const proj = entry.project ?? null;
  if (proj === null && project === null) return true;
  return proj !== null && proj === project;
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

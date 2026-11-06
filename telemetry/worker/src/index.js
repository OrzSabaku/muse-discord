// muse-discord usage stats: a Cloudflare Worker + D1.
//
//   POST /v1/report         one day's report from an install (telemetry.py)
//   GET  /v1/stats          public aggregate totals
//   GET  /v1/badge/servers  a shields.io endpoint badge ("used in N servers")
//
// What is stored: the validated report fields only (validate.js), keyed
// by (install_id, day), plus a per-install count of reports received per
// UTC day for the rate limit. Never the IP address, headers, or the
// time of the request: this code reads none of them.

import {
  MAX_BODY_BYTES, REPORTS_PER_INSTALL_PER_DAY, addDays, parseReport,
  serversBadge, utcDay,
} from "./validate.js";

const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };
const PUBLIC_HEADERS = {
  ...JSON_HEADERS,
  "Access-Control-Allow-Origin": "*",
  "Cache-Control": "public, max-age=3600",
};

function json(obj, status = 200, headers = JSON_HEADERS) {
  return new Response(JSON.stringify(obj), { status, headers });
}

// Read at most MAX_BODY_BYTES of the body; null when it is longer.
async function readBody(request) {
  const len = Number(request.headers.get("Content-Length") || "0");
  if (len > MAX_BODY_BYTES) return null;
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_BODY_BYTES) return null;
  return new TextDecoder().decode(buf);
}

const COLUMNS = [
  "install_id", "day", "version", "ahead", "modified", "python", "os",
  "servers",
  "members_bucket", "messages_seen", "queued", "replies_sent",
  "quarantined", "wakes", "latency_p50", "latency_p90", "media_sent",
  "restarts", "refusals", "settings_changes", "presets_applied", "crashes",
  "features",
];
const OBJECT_COLUMNS = new Set(["refusals", "settings_changes",
  "presets_applied", "crashes", "features"]);

export function upsertSql() {
  const cols = COLUMNS.join(", ");
  const marks = COLUMNS.map(() => "?").join(", ");
  const sets = COLUMNS.filter((c) => c !== "install_id" && c !== "day")
    .map((c) => `${c} = excluded.${c}`).join(", ");
  return `INSERT INTO reports (${cols}) VALUES (${marks}) `
    + `ON CONFLICT (install_id, day) DO UPDATE SET ${sets}`;
}

export function rowValues(report) {
  return COLUMNS.map((c) => {
    if (OBJECT_COLUMNS.has(c)) return JSON.stringify(report[c]);
    if (c === "modified") return report[c] ? 1 : 0;
    return report[c];
  });
}

async function handleReport(request, env, now) {
  const text = await readBody(request);
  if (text === null) return json({ error: "body too large" }, 413);
  const res = parseReport(text, now);
  if (!res.ok) return json({ error: res.error }, 400);
  const report = res.report;
  const today = utcDay(now);
  // The rate limit: reports received from this install today.
  const rate = await env.DB.prepare(
    "INSERT INTO rate (install_id, received_day, n) VALUES (?, ?, 1) "
    + "ON CONFLICT (install_id, received_day) DO UPDATE SET n = n + 1 "
    + "RETURNING n",
  ).bind(report.install_id, today).first();
  if (rate && rate.n > REPORTS_PER_INSTALL_PER_DAY) {
    return json({ error: "too many reports today" }, 429);
  }
  await env.DB.prepare(upsertSql()).bind(...rowValues(report)).run();
  return new Response(null, { status: 204 });
}

export async function stats(env, now) {
  const today = utcDay(now);
  const d7 = addDays(today, -7);
  const d30 = addDays(today, -30);
  const row = await env.DB.prepare(
    "SELECT "
    + "(SELECT COUNT(DISTINCT install_id) FROM reports WHERE day >= ?1) "
    + "AS installs_7d, "
    + "(SELECT COUNT(DISTINCT install_id) FROM reports WHERE day >= ?2) "
    + "AS installs_30d, "
    + "(SELECT COALESCE(SUM(servers), 0) FROM (SELECT servers, MAX(day) "
    + "FROM reports WHERE day >= ?1 GROUP BY install_id)) AS servers, "
    + "(SELECT COALESCE(SUM(replies_sent), 0) FROM reports WHERE day >= ?1) "
    + "AS replies_7d, "
    + "(SELECT COALESCE(SUM(replies_sent), 0) FROM reports WHERE day >= ?2) "
    + "AS replies_30d",
  ).bind(d7, d30).first();
  return {
    installs_7d: row?.installs_7d ?? 0,
    installs_30d: row?.installs_30d ?? 0,
    servers: row?.servers ?? 0,
    replies_7d: row?.replies_7d ?? 0,
    replies_30d: row?.replies_30d ?? 0,
    as_of: today,
  };
}

export async function handle(request, env, now = new Date()) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  try {
    if (path === "/v1/report") {
      if (request.method !== "POST") {
        return json({ error: "use POST" }, 405);
      }
      return await handleReport(request, env, now);
    }
    if (path === "/v1/stats" || path === "/v1/badge/servers") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return json({ error: "use GET" }, 405);
      }
      const s = await stats(env, now);
      return json(path === "/v1/stats" ? s : serversBadge(s.servers), 200,
        PUBLIC_HEADERS);
    }
    return json({ error: "not found" }, 404);
  } catch {
    return json({ error: "internal error" }, 500);
  }
}

// Daily: forget rate-limit counts older than yesterday.
export async function prune(env, now = new Date()) {
  await env.DB.prepare("DELETE FROM rate WHERE received_day < ?")
    .bind(addDays(utcDay(now), -1)).run();
}

export default {
  fetch: (request, env) => handle(request, env),
  scheduled: (_event, env, ctx) => ctx.waitUntil(prune(env)),
};

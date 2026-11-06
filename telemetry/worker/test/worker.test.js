// node --test (no packages): the report validation and the Worker's
// routes against a small fake D1.
import { test } from "node:test";
import assert from "node:assert/strict";

import { handle, prune } from "../src/index.js";
import {
  MAX_BODY_BYTES, REPORTS_PER_INSTALL_PER_DAY, parseReport, validateReport,
} from "../src/validate.js";

const NOW = new Date("2026-10-21T05:00:00Z");

function report(over = {}) {
  return {
    schema: 1,
    install_id: "0b6f8f0e-6c1a-4f3e-9a2b-1c2d3e4f5a6b",
    day: "2026-10-20",
    version: "unreleased",
    ahead: 0,
    modified: false,
    python: "3.12",
    os: "linux",
    servers: 3,
    members_bucket: 256,
    messages_seen: 420,
    queued: 12,
    replies_sent: 11,
    quarantined: 0,
    refusals: { cap: 2, "not-allowed": 1 },
    wakes: 9,
    latency_p50: 6,
    latency_p90: 21,
    settings_changes: { settings: 2, dm: 1 },
    presets_applied: { community: 1 },
    media_sent: 1,
    restarts: 1,
    crashes: {},
    features: { server_replies: "conversational", memory: "public-only",
      daily_cap: "off", media: "on" },
    ...over,
  };
}

test("a well-formed report is accepted, as a clean copy", () => {
  const r = validateReport(report(), NOW);
  assert.equal(r.ok, true, r.error);
  assert.deepEqual(Object.keys(r.report), Object.keys(report()));
});

test("anything unknown, missing or out of range is refused", () => {
  const bad = [
    { extra: "x" },
    { content: "hello" },
    { schema: 2 },
    { install_id: "123456789012345678" },
    { install_id: "0B6F8F0E-6C1A-4F3E-9A2B-1C2D3E4F5A6B" },
    { day: "2026-10-22" }, // the future
    { day: "2026-09-01" }, // too old
    { day: "2026-02-30" },
    { day: "2026-10-20T10:00:00Z" },
    { version: "v1.2.3; rm -rf" },
    { python: "3.12.1" },
    { os: "Ubuntu 24.04" },
    { servers: -1 },
    { servers: 1.5 },
    { servers: "3" },
    { members_bucket: 300 },
    { messages_seen: 1e12 },
    { refusals: { "user 123": 1 } },
    { refusals: { cap: 0 } },
    { refusals: [] },
    { latency_p50: 30, latency_p90: 10 },
    { latency_p50: 99999 },
    { settings_changes: { web: 1 } },
    { presets_applied: { custom: 1 } },
    { crashes: { "ValueError: token abc": 1 } },
    { crashes: Object.fromEntries(
      Array.from({ length: 21 }, (_, i) => [`E${i}`, 1])) },
    { features: { memory: "#general" } },
    { features: { channel: "on" } },
  ];
  for (const over of bad) {
    const r = validateReport(report(over), NOW);
    assert.equal(r.ok, false, `accepted ${JSON.stringify(over)}`);
  }
  const missing = report();
  delete missing.wakes;
  assert.equal(validateReport(missing, NOW).ok, false);
  assert.equal(validateReport([report()], NOW).ok, false);
  assert.equal(validateReport(null, NOW).ok, false);
});

test("bodies that are not JSON or too large are refused", () => {
  assert.equal(parseReport("not json", NOW).ok, false);
  assert.equal(parseReport("x".repeat(MAX_BODY_BYTES + 1), NOW).ok, false);
});

// A fake D1: enough of prepare/bind/first/run for the Worker's queries.
function fakeDB() {
  const db = { reports: new Map(), rate: new Map(), binds: [] };
  db.prepare = (sql) => ({
    bind: (...args) => {
      db.binds.push(...args);
      return {
        first: async () => {
          if (sql.startsWith("INSERT INTO rate")) {
            const k = `${args[0]}|${args[1]}`;
            db.rate.set(k, (db.rate.get(k) || 0) + 1);
            return { n: db.rate.get(k) };
          }
          if (sql.startsWith("SELECT")) {
            const rows = [...db.reports.values()];
            return {
              installs_7d: new Set(rows.map((r) => r[0])).size,
              installs_30d: new Set(rows.map((r) => r[0])).size,
              servers: rows.reduce((a, r) => a + r[7], 0),
              replies_7d: rows.reduce((a, r) => a + r[11], 0),
              replies_30d: rows.reduce((a, r) => a + r[11], 0),
            };
          }
          return null;
        },
        run: async () => {
          if (sql.startsWith("INSERT INTO reports")) {
            db.reports.set(`${args[0]}|${args[1]}`, args);
          }
          if (sql.startsWith("DELETE FROM rate")) db.pruned = args[0];
          return { success: true };
        },
      };
    },
  });
  return db;
}

function post(body, headers = {}) {
  return new Request("https://t.example/v1/report", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

test("a report is stored once per install and day, without the IP", async () => {
  const env = { DB: fakeDB() };
  const ip = "203.0.113.77";
  let res = await handle(post(report(), { "CF-Connecting-IP": ip,
    "User-Agent": "muse-discord-telemetry/1" }), env, NOW);
  assert.equal(res.status, 204);
  res = await handle(post(report({ replies_sent: 12 })), env, NOW);
  assert.equal(res.status, 204);
  assert.equal(env.DB.reports.size, 1);
  const row = [...env.DB.reports.values()][0];
  assert.equal(row[11], 12); // the resent day replaced the row
  const bound = JSON.stringify(env.DB.binds);
  assert.ok(!bound.includes(ip));
  assert.ok(!bound.includes("muse-discord-telemetry"));
});

test("an install is rate limited per day", async () => {
  const env = { DB: fakeDB() };
  for (let i = 0; i < REPORTS_PER_INSTALL_PER_DAY; i++) {
    const res = await handle(post(report()), env, NOW);
    assert.equal(res.status, 204);
  }
  const res = await handle(post(report()), env, NOW);
  assert.equal(res.status, 429);
  const other = await handle(post(report({
    install_id: "1b6f8f0e-6c1a-4f3e-9a2b-1c2d3e4f5a6b" })), env, NOW);
  assert.equal(other.status, 204);
});

test("bad requests store nothing", async () => {
  const env = { DB: fakeDB() };
  const cases = [
    [post(report({ content: "secret" })), 400],
    [post("x".repeat(MAX_BODY_BYTES + 10)), 413],
    [new Request("https://t.example/v1/report"), 405],
    [new Request("https://t.example/v1/nope"), 404],
    [new Request("https://t.example/v1/stats", { method: "POST" }), 405],
  ];
  for (const [req, status] of cases) {
    const res = await handle(req, env, NOW);
    assert.equal(res.status, status);
  }
  assert.equal(env.DB.reports.size, 0);
});

test("stats and the servers badge", async () => {
  const env = { DB: fakeDB() };
  await handle(post(report()), env, NOW);
  await handle(post(report({
    install_id: "1b6f8f0e-6c1a-4f3e-9a2b-1c2d3e4f5a6b", servers: 2 })),
  env, NOW);
  let res = await handle(new Request("https://t.example/v1/stats"), env, NOW);
  assert.equal(res.status, 200);
  const s = await res.json();
  assert.equal(s.installs_7d, 2);
  assert.equal(s.servers, 5);
  res = await handle(new Request("https://t.example/v1/badge/servers"), env,
    NOW);
  const b = await res.json();
  assert.equal(b.schemaVersion, 1);
  assert.equal(b.message, "5 servers");
  await prune(env, NOW);
  assert.equal(env.DB.pruned, "2026-10-20");
});

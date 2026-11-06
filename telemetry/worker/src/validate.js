// Strict validation of one muse-discord usage report (telemetry.py,
// build_payload). Pure: no Cloudflare APIs, so `node --test` covers it.
//
// A report is accepted only when it has exactly the known fields, each
// of the right type and within range. Anything else is refused whole:
// an unknown field could carry something we promised never to keep.

export const SCHEMA = 1;
export const MAX_BODY_BYTES = 4096;
export const MAX_AGE_DAYS = 30; // older days are refused
export const REPORTS_PER_INSTALL_PER_DAY = 10; // 7 waiting days + retries

export const REFUSAL_CODES = [
  "replies-off", "dm-off", "bots-ignore", "not-allowed", "dm-not-allowed",
  "cap", "bot-exchange", "suspicious-ignored", "predates-join", "no-send",
  "internal", "worker-skipped", "send-failed", "never-share", "other",
];
export const SOURCES = ["settings", "dm", "cli"];
export const PRESETS = ["friends", "community", "work"];
export const OS_FAMILIES = ["linux", "darwin", "windows", "freebsd",
  "openbsd", "other"];
const ONOFF = ["on", "off"];
export const FEATURES = {
  server_replies: ["off", "when-addressed", "conversational"],
  dm_replies: ["off", "when-addressed", "conversational"],
  dms_from: ["owners", "shared-servers", "anyone", "list", "nobody"],
  memory: ["everything", "public-only", "nothing"],
  suspicious: ["hold-for-review", "ignore", "answer"],
  bots: ["ignore", "when-addressed"],
  length: ["brief", "normal", "detailed"],
  daily_cap: ONOFF,
  per_person_cap: ONOFF,
  presence: ONOFF,
  update_notices: ONOFF,
  media: ONOFF,
};

const BIG = 100_000_000;
const COUNT = 10_000_000;
// field -> checker(value) returning an error string or null
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const VERSION = /^v(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$/;
const PYTHON = /^3\.\d{1,2}$/;
const CLASS_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

function int(min, max) {
  return (v) => (Number.isInteger(v) && v >= min && v <= max
    ? null : `an integer ${min}-${max}`);
}

function intOrNull(min, max) {
  const check = int(min, max);
  return (v) => (v === null ? null : check(v));
}

function oneOf(values) {
  return (v) => (typeof v === "string" && values.includes(v)
    ? null : `one of ${values.join(", ")}`);
}

function counts(keyOk, maxKeys, keyText) {
  const check = int(1, COUNT);
  return (v) => {
    if (!isPlainObject(v)) return "an object";
    const keys = Object.keys(v);
    if (keys.length > maxKeys) return `at most ${maxKeys} entries`;
    for (const k of keys) {
      if (!keyOk(k)) return `keys must be ${keyText}`;
      if (check(v[k])) return `values must be ${check(v[k])}`;
    }
    return null;
  };
}

function isPlainObject(v) {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const FIELDS = {
  schema: (v) => (v === SCHEMA ? null : `${SCHEMA}`),
  install_id: (v) => (typeof v === "string" && UUID4.test(v)
    ? null : "a lowercase uuid4"),
  day: (v) => (typeof v === "string" && DAY.test(v) && validDay(v)
    ? null : "a date YYYY-MM-DD"),
  version: (v) => (typeof v === "string" && (VERSION.test(v)
    || v === "unreleased" || v === "unknown")
    ? null : "vX.Y.Z, unreleased or unknown"),
  ahead: int(0, 100_000),
  modified: (v) => (typeof v === "boolean" ? null : "true or false"),
  python: (v) => (typeof v === "string" && PYTHON.test(v)
    ? null : "3.N"),
  os: oneOf(OS_FAMILIES),
  servers: int(0, 100_000),
  members_bucket: (v) => (Number.isInteger(v) && v >= 0 && v <= 2 ** 31
    && (v === 0 || (v & (v - 1)) === 0) ? null : "0 or a power of 2"),
  messages_seen: int(0, BIG),
  queued: int(0, COUNT),
  replies_sent: int(0, COUNT),
  quarantined: int(0, COUNT),
  refusals: counts((k) => REFUSAL_CODES.includes(k), REFUSAL_CODES.length,
    "refusal codes"),
  wakes: int(0, COUNT),
  latency_p50: intOrNull(0, 3600),
  latency_p90: intOrNull(0, 3600),
  settings_changes: counts((k) => SOURCES.includes(k), SOURCES.length,
    SOURCES.join(", ")),
  presets_applied: counts((k) => PRESETS.includes(k), PRESETS.length,
    PRESETS.join(", ")),
  media_sent: int(0, COUNT),
  restarts: int(0, 100_000),
  crashes: counts((k) => CLASS_NAME.test(k), 20, "class names"),
  features: (v) => {
    if (!isPlainObject(v)) return "an object";
    for (const [k, x] of Object.entries(v)) {
      if (!(k in FEATURES)) return `unknown feature ${JSON.stringify(k).slice(0, 40)}`;
      if (!(FEATURES[k].includes(x) || x === "other")) {
        return `${k} must be one of ${FEATURES[k].join(", ")} or other`;
      }
    }
    return null;
  },
};
export const FIELD_NAMES = Object.keys(FIELDS);

function validDay(s) {
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

export function utcDay(date) {
  return date.toISOString().slice(0, 10);
}

export function addDays(day, n) {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return utcDay(d);
}

// {ok: true, report} or {ok: false, error}. now: a Date (the day may
// not be after today, UTC, nor more than MAX_AGE_DAYS old).
export function validateReport(body, now = new Date()) {
  if (!isPlainObject(body)) return { ok: false, error: "expected an object" };
  for (const k of Object.keys(body)) {
    if (!(k in FIELDS)) {
      return { ok: false, error: `unknown field ${JSON.stringify(k).slice(0, 40)}` };
    }
  }
  for (const [k, check] of Object.entries(FIELDS)) {
    if (!(k in body)) return { ok: false, error: `missing ${k}` };
    const err = check(body[k]);
    if (err) return { ok: false, error: `${k} must be ${err}` };
  }
  const today = utcDay(now);
  if (body.day > today) {
    return { ok: false, error: "day is in the future (UTC)" };
  }
  if (body.day < addDays(today, -MAX_AGE_DAYS)) {
    return { ok: false, error: `day is more than ${MAX_AGE_DAYS} days old` };
  }
  if (body.latency_p50 !== null && body.latency_p90 !== null
      && body.latency_p50 > body.latency_p90) {
    return { ok: false, error: "latency_p50 must not exceed latency_p90" };
  }
  // A clean copy with only the known fields, in a fixed order.
  const report = {};
  for (const k of FIELD_NAMES) report[k] = body[k];
  return { ok: true, report };
}

// Parse a request body text (already size-checked) into a report.
export function parseReport(text, now = new Date()) {
  if (typeof text !== "string" || text.length > MAX_BODY_BYTES) {
    return { ok: false, error: "body too large" };
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return { ok: false, error: "not JSON" };
  }
  return validateReport(body, now);
}

// The shields.io endpoint badge for N servers.
export function serversBadge(n) {
  const v = Number.isInteger(n) && n >= 0 ? n : 0;
  return {
    schemaVersion: 1,
    label: "used in",
    message: `${v.toLocaleString("en-US")} server${v === 1 ? "" : "s"}`,
    color: "5865F2",
  };
}

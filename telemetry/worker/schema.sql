-- muse-discord usage stats (D1 / SQLite). Apply with:
--   npx wrangler d1 execute muse-telemetry --remote --file=schema.sql
-- One row per install per UTC day (a resent day replaces its row). No
-- IP addresses, headers or request times are stored anywhere.

CREATE TABLE IF NOT EXISTS reports (
  install_id       TEXT    NOT NULL,  -- random uuid4 made by the install
  day              TEXT    NOT NULL,  -- the UTC day counted, YYYY-MM-DD
  version          TEXT    NOT NULL,  -- vX.Y.Z, unreleased or unknown
  ahead            INTEGER NOT NULL DEFAULT 0,  -- commits past that release
  modified         INTEGER NOT NULL DEFAULT 0,  -- 1 = code edited locally
  python           TEXT    NOT NULL,  -- 3.N
  os               TEXT    NOT NULL,  -- linux, darwin, windows, ...
  servers          INTEGER NOT NULL,
  members_bucket   INTEGER NOT NULL,  -- total members, rounded down to 2^k
  messages_seen    INTEGER NOT NULL,
  queued           INTEGER NOT NULL,
  replies_sent     INTEGER NOT NULL,
  quarantined      INTEGER NOT NULL,
  wakes            INTEGER NOT NULL,
  latency_p50      INTEGER,           -- seconds, NULL = no replies
  latency_p90      INTEGER,
  media_sent       INTEGER NOT NULL,
  restarts         INTEGER NOT NULL,
  refusals         TEXT    NOT NULL,  -- JSON {code: n}
  settings_changes TEXT    NOT NULL,  -- JSON {settings|dm|cli: n}
  presets_applied  TEXT    NOT NULL,  -- JSON {friends|community|work: n}
  crashes          TEXT    NOT NULL,  -- JSON {ExceptionClass: n}
  features         TEXT    NOT NULL,  -- JSON {flag: enum}
  PRIMARY KEY (install_id, day)
);
CREATE INDEX IF NOT EXISTS reports_day ON reports (day);

-- Reports received per install per UTC day (the rate limit); rows older
-- than yesterday are deleted daily by the Worker's cron trigger.
CREATE TABLE IF NOT EXISTS rate (
  install_id   TEXT    NOT NULL,
  received_day TEXT    NOT NULL,
  n            INTEGER NOT NULL,
  PRIMARY KEY (install_id, received_day)
);

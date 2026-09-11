CREATE TABLE IF NOT EXISTS pings (
  id          TEXT NOT NULL,
  version     TEXT,
  python      TEXT,
  platform    TEXT,
  ts          INTEGER NOT NULL,
  received_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_pings_ts    ON pings (ts);
CREATE INDEX IF NOT EXISTS idx_pings_id_ts ON pings (id, ts);

# PyreCrawl telemetry collector (Cloudflare Worker + D1)

Receives the anonymous opt-out ping from `src/pyrecrawl/telemetry.py` and serves a
DAU/MAU dashboard. Free Cloudflare plan is more than enough (~100k requests/day).

## One-time deploy (~5 min)

```bash
npm exec wrangler login                        # opens browser, authorize
cd workers/telemetry
npm exec wrangler d1 create pyrecrawl-stats    # copy database_id into wrangler.toml
npm exec wrangler d1 execute pyrecrawl-stats --remote --file=schema.sql
npm exec wrangler secret put STATS_KEY         # type any long random string
npm exec wrangler deploy
```

Check your workers.dev subdomain (dashboard → Workers → pyrecrawl-stats) and update
`ENDPOINT` in `src/pyrecrawl/telemetry.py` if it differs from
`pyrecrawl-stats.sanggonboy.workers.dev`.

## View stats

```bash
curl "https://<your-worker>.workers.dev/stats?key=<STATS_KEY>"
```

Returns: DAU (24 h), WAU (7 d), MAU (30 d), total unique installs, top versions,
OS breakdown, and a 30-day daily unique-user series.

## What a ping contains

Exactly 4 fields: hashed machine id (SHA-256 of hostname+MAC, 16 hex chars),
pyrecrawl version, python version, OS family. No IP is stored, no URLs, no content.

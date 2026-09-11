// PyreCrawl telemetry collector — Cloudflare Worker + D1 (free plan is enough).
//
// POST /ping  body {id, version, python, platform, ts}  — insert one anonymous ping.
// GET  /stats?key=STATS_KEY                             — DAU/MAU/totals dashboard JSON.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);

    if (url.pathname === "/ping" && request.method === "POST") {
      const len = Number(request.headers.get("content-length") || 0);
      if (len > 4096) return json({ error: "payload too large" }, 413);
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "bad json" }, 400);
      }
      const id = String(body.id || "").slice(0, 64);
      if (!/^[0-9a-f]{8,64}$/.test(id)) return json({ error: "bad id" }, 400);
      const version = String(body.version || "").slice(0, 32);
      const python = String(body.python || "").slice(0, 32);
      const platform = String(body.platform || "").slice(0, 32);
      const ts = Number(body.ts) || Math.floor(Date.now() / 1000);
      await env.DB.prepare(
        "INSERT INTO pings (id, version, python, platform, ts) VALUES (?1, ?2, ?3, ?4, ?5)"
      ).bind(id, version, python, platform, ts).run();
      return json({ ok: true });
    }

    if (url.pathname === "/stats" && request.method === "GET") {
      const key = url.searchParams.get("key") || request.headers.get("x-stats-key") || "";
      if (!env.STATS_KEY || key !== env.STATS_KEY) return json({ error: "unauthorized" }, 401);
      const now = Math.floor(Date.now() / 1000);
      const [dau, wau, mau, total, versions, platforms, daily] = await Promise.all([
        countSince(env, now - 86400),
        countSince(env, now - 86400 * 7),
        countSince(env, now - 86400 * 30),
        env.DB.prepare("SELECT COUNT(DISTINCT id) AS n FROM pings").first(),
        env.DB.prepare(
          "SELECT version, COUNT(DISTINCT id) AS users FROM pings WHERE ts > ?1 GROUP BY version ORDER BY users DESC LIMIT 10"
        ).bind(now - 86400 * 30).all(),
        env.DB.prepare(
          "SELECT platform, COUNT(DISTINCT id) AS users FROM pings WHERE ts > ?1 GROUP BY platform ORDER BY users DESC"
        ).bind(now - 86400 * 30).all(),
        env.DB.prepare(
          "SELECT date(ts, 'unixepoch') AS day, COUNT(DISTINCT id) AS users FROM pings WHERE ts > ?1 GROUP BY day ORDER BY day"
        ).bind(now - 86400 * 30).all(),
      ]);
      return json({
        dau_24h: dau,
        wau_7d: wau,
        mau_30d: mau,
        total_unique: total.n,
        versions_30d: versions.results,
        platforms_30d: platforms.results,
        daily_30d: daily.results,
      });
    }

    if (url.pathname === "/badge/users" && request.method === "GET") {
      const now = Math.floor(Date.now() / 1000);
      const mau = await countSince(env, now - 86400 * 30);
      return badge("active users / 30d", `${mau}`, "brightgreen");
    }

    if (url.pathname === "/badge/downloads" && request.method === "GET") {
      // Sum of "without_mirrors" PyPI downloads over the last 30 days (pypistats.org).
      const cutoff = new Date(Date.now() - 86400 * 1000 * 30).toISOString().slice(0, 10);
      try {
        const r = await fetch("https://pypistats.org/api/packages/pyrecrawl/overall", {
          headers: { "User-Agent": "pyrecrawl-stats-worker" },
        });
        if (r.ok) {
          const data = await r.json();
          const total = data.data
            .filter((d) => d.category === "without_mirrors" && d.date >= cutoff)
            .reduce((s, d) => s + d.downloads, 0);
          return badge("downloads / 30d", `${total}`, "orange");
        }
      } catch { /* fall through to unknown */ }
      return badge("downloads / 30d", "unknown", "lightgrey");
    }

    return json({ error: "not found" }, 404);
  },
};

async function countSince(env, since) {
  const row = await env.DB.prepare(
    "SELECT COUNT(DISTINCT id) AS n FROM pings WHERE ts > ?1"
  ).bind(since).first();
  return row.n;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

// shields.io endpoint-badge payload; cached 1 h at the edge so badge churn is low.
function badge(label, message, color) {
  return new Response(
    JSON.stringify({ schemaVersion: 1, label, message, color }),
    {
      status: 200,
      headers: { "Content-Type": "application/json", "Cache-Control": "public, max-age=3600", ...CORS },
    }
  );
}

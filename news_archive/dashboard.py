"""Local, read-only news viewer. Run independently of the Telegram collector."""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .collector_runtime import read_health
from .article_fetcher import pending_urls
from haber_extractor.url_utils import normalize_url, parse_urls
from haber_extractor.news_extractor import should_skip

WEB = Path(__file__).resolve().parent / "web"
TR = ZoneInfo("Europe/Istanbul")


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.create_function("tr_lower", 1, lambda s: (s or "").replace("İ", "i").replace("I", "ı").lower())
    return db


def feed(db, params):
    limit = min(max(int(params.get("limit", "40")), 1), 100)
    offset = max(int(params.get("offset", "0")), 0)
    where, values = ["trim(m.text)!=''"], []
    if params.get("channel"):
        where.append("m.channel_id=?")
        values.append(int(params["channel"]))
    query = params.get("q", "").strip()[:200]
    if query:
        where.append("instr(tr_lower(m.text),tr_lower(?))>0")
        values.append(query)
    for name, operator in (("from", ">="), ("to", "<")):
        if params.get(name):
            bound = datetime.strptime(params[name], "%Y-%m-%d").replace(tzinfo=TR)
            if name == "to":
                bound += timedelta(days=1)
            where.append(f"m.date_utc{operator}?")
            values.append(bound.astimezone(timezone.utc).isoformat())
    if params.get("mode") == "live":
        where.append("EXISTS (SELECT 1 FROM message_versions v WHERE v.channel_id=m.channel_id "
                     "AND v.message_id=m.message_id AND v.source='live_new')")
    clause = " AND ".join(where)
    total = db.execute("SELECT COUNT(*) FROM messages m WHERE " + clause, values).fetchone()[0]
    rows = db.execute(
        "SELECT m.channel_id,m.message_id,c.title,m.date_utc,m.collected_at,m.edit_date_utc,"
        "substr(m.text,1,360) AS preview,"
        "EXISTS (SELECT 1 FROM message_versions v WHERE v.channel_id=m.channel_id "
        "AND v.message_id=m.message_id AND v.source='live_new') AS live "
        "FROM messages m LEFT JOIN channels c USING(channel_id) WHERE " + clause +
        " ORDER BY m.date_utc DESC,m.channel_id DESC,m.message_id DESC LIMIT ? OFFSET ?",
        values + [limit, offset],
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None}


def detail(db, cid, mid):
    row = db.execute("SELECT m.*,c.title,c.username FROM messages m "
                     "LEFT JOIN channels c USING(channel_id) WHERE m.channel_id=? AND m.message_id=?",
                     (cid, mid)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["urls"] = json.loads(data["urls"] or "[]")
    data["versions"] = [dict(r) for r in db.execute(
        "SELECT version_id,observed_at,edit_date_utc,text,source,snapshot_json FROM message_versions "
        "WHERE channel_id=? AND message_id=? ORDER BY observed_at,version_id", (cid, mid))]
    deleted = db.execute("SELECT observed_at FROM message_deletions WHERE channel_id=? AND message_id=?",
                         (cid, mid)).fetchone()
    data["deleted_at"] = deleted[0] if deleted else None
    data["telegram_url"] = f"https://t.me/c/{cid}/{mid}"
    urls = set(data["urls"])
    for version in data["versions"]:
        version["urls"] = parse_urls(json.loads(version.pop("snapshot_json")).get("urls"))
        urls.update(version["urls"])
    data["articles"] = []
    has_articles = db.execute("SELECT 1 FROM sqlite_master WHERE name='articles'").fetchone()
    for url in sorted(urls):
        normalized = normalize_url(url)
        article = db.execute("SELECT url,title,text,status,error,final_url,fetched_at,last_checked_at "
                             "FROM articles WHERE url=?", (normalized,)).fetchone() if has_articles else None
        data["articles"].append(dict(article) if article else {
            "url": normalized or url, "status": "skipped" if not normalized or should_skip(normalized) else "pending",
            "title": None, "text": None, "error": None})
    return data


def article_queue(db, params):
    """Inspect durable jobs and download results without starting any work."""
    ids = {int(params['channel'])} if params.get('channel') else {r[0] for r in db.execute('SELECT channel_id FROM channels')}
    titles = {r[0]: r[1] for r in db.execute('SELECT channel_id,title FROM channels')}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='articles'").fetchone():
        return {"pending_count": 0, "counts": {}, "pending": [], "recent": []}
    jobs = pending_urls(db, ids)
    marks = ','.join('?' for _ in ids)
    rows = db.execute("SELECT a.url,a.title,a.status,a.error,a.source_channel AS channel_id,"
                      "a.source_message AS message_id,a.fetched_at,length(a.text) AS characters,c.title AS channel_title "
                      "FROM articles a LEFT JOIN channels c ON c.channel_id=a.source_channel "
                      f"WHERE a.source_channel IN ({marks}) ORDER BY a.fetched_at DESC LIMIT 12", sorted(ids))
    counts = dict(db.execute(f"SELECT status,COUNT(*) FROM articles WHERE source_channel IN ({marks}) GROUP BY status", sorted(ids)))
    return {"pending_count": len(jobs), "counts": counts,
            "pending": [{"url": u, "channel_id": c, "message_id": m, "channel_title": titles.get(c)}
                        for u, c, m in jobs[:12]], "recent": [dict(r) for r in rows]}


def status(db, health_path):
    health, ok = read_health(health_path)
    channels = [dict(r) for r in db.execute(
        "SELECT c.channel_id,c.title,c.username,COUNT(m.message_id) AS count,MIN(m.date_utc) AS oldest "
        "FROM channels c LEFT JOIN messages m USING(channel_id) GROUP BY c.channel_id ORDER BY c.title")]
    for c in channels:
        c["collection"] = health.get("channels", {}).get(str(c["channel_id"]), {})
    return {"channels": channels, "total": sum(c["count"] for c in channels),
            "healthy": ok, "health": health, "now": datetime.now(timezone.utc).isoformat()}


def handler_for(db_path, health_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, content_type="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in {"127.0.0.1", "localhost"}:
                self.send(403, b'{"error":"Local access only"}')
                return
            url = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path.startswith("/api/"):
                    with closing(connect(db_path)) as db:
                        if url.path == "/api/feed":
                            data = feed(db, params)
                        elif url.path == "/api/status":
                            data = status(db, health_path)
                        elif url.path == "/api/articles":
                            data = article_queue(db, params)
                        elif url.path == "/api/message":
                            data = detail(db, int(params["channel"]), int(params["message"]))
                            if data is None:
                                self.send(404, b'{"error":"Message not found"}')
                                return
                        else:
                            self.send(404, b'{"error":"Not found"}')
                            return
                    self.send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))
                elif url.path in {"/", "/app.js", "/style.css"}:
                    filename = "index.html" if url.path == "/" else url.path[1:]
                    mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[filename]
                    self.send(200, (WEB / filename).read_bytes(), mime + "; charset=utf-8")
                elif url.path == "/favicon.ico":
                    self.send(204, b"")
                else:
                    self.send(404, b'{"error":"Not found"}')
            except (ValueError, KeyError):
                self.send(400, b'{"error":"Invalid filter"}')
            except (sqlite3.Error, OSError):
                self.send(503, b'{"error":"Archive temporarily unavailable"}')
    return Handler


def main():
    p = argparse.ArgumentParser(description="Yerel haber izleme sayfası")
    p.add_argument("--db", default="local-pilot/archive.db")
    p.add_argument("--health", default="local-pilot/health.json")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    with closing(connect(args.db)) as db:
        db.execute("SELECT 1 FROM messages LIMIT 1")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.db, args.health))
    print(f"Haber masası: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

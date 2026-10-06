"""Telegram mesajlarındaki URL'lerden makale tam metnini çeker.

messages tablosundaki URL'leri tarar, henüz çekilmemiş olanları indirir ve
trafilatura ile ana metni çıkarır. Domain başına rate limiting uygular.
"""

import heapq
import itertools
import json
import hashlib
import logging
import time
from collections import namedtuple
from datetime import datetime, timezone
import zlib

from haber_extractor.news_extractor import (ArticleResult, EmptyArticleError, DEFAULT_DELAY,
                            domain_of, should_skip, fetch_article, extract_html, extract_page,
                            request_stop, reset_stop, _stop)
from haber_extractor.url_utils import normalize_url, parse_urls

log = logging.getLogger("fetcher")

PARALLEL_SITES = 4          # sites fetched at once; a site still gets one request at a time
REFUSALS = ("HTTP 403", "HTTP 429", "HTTP 503")   # the site turning requests away, not a missing page
REFUSAL_PAUSE = 60.0        # seconds a refusing site is left alone; doubles while it keeps refusing
REFUSAL_PAUSE_MAX = 3600.0

ARTICLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url             TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    title           TEXT,
    text            TEXT,
    source_channel  INTEGER,
    source_message  INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',  -- ok | empty | error
    error           TEXT,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_articles_domain ON articles(domain);
CREATE INDEX IF NOT EXISTS ix_articles_status ON articles(status);
CREATE TABLE IF NOT EXISTS article_versions (
    url TEXT NOT NULL, version_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    title TEXT, text TEXT NOT NULL, final_url TEXT, content_type TEXT,
    fetched_at TEXT NOT NULL, raw_content_zlib BLOB,
    PRIMARY KEY(url, version_id)
);
CREATE INDEX IF NOT EXISTS ix_article_versions_time ON article_versions(url, fetched_at);
CREATE TABLE IF NOT EXISTS article_revision_checks (
    url TEXT NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    version_id TEXT NOT NULL, checked_at TEXT NOT NULL,
    PRIMARY KEY(url, channel_id, message_id, version_id)
);
CREATE TABLE IF NOT EXISTS article_extractions (
    url TEXT NOT NULL, version_id TEXT NOT NULL,
    details TEXT NOT NULL,  -- JSON: method, coverage and what the page declares
    PRIMARY KEY(url, version_id)
);
"""

def ensure_schema(db):
    checks_existed = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='article_revision_checks'"
    ).fetchone()
    db.executescript(ARTICLES_SCHEMA)
    columns = {r[1] for r in db.execute("PRAGMA table_info(articles)")}
    for name in ("final_url", "content_type", "last_checked_at"):
        if name not in columns:
            db.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT")
    # Preserve legacy successful content before any new attempt can overwrite it.
    for url, title, text, fetched, final, kind in db.execute(
        "SELECT url,title,text,fetched_at,final_url,content_type FROM articles "
        "WHERE text IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM article_versions v WHERE v.url=articles.url)"
    ).fetchall():
        _save_version(db, url, title, text, fetched, final or url, kind, None)
    if not checks_existed:
        for url, cid, mid, vid, observed in _edited_revisions(db):
            previous = db.execute("SELECT fetched_at FROM articles WHERE url=?", (url,)).fetchone()
            if previous and observed <= previous[0]:
                db.execute("INSERT OR IGNORE INTO article_revision_checks VALUES (?,?,?,?,?)",
                           (url, cid, mid, vid, previous[0]))
    db.commit()


def _edited_revisions(db, url=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_versions'").fetchone():
        return []
    records = []
    for cid, mid, vid, observed, payload in db.execute(
        "SELECT channel_id,message_id,version_id,observed_at,snapshot_json "
        "FROM message_versions WHERE edit_date_utc IS NOT NULL"
    ):
        for raw in parse_urls(json.loads(payload).get("urls")):
            normalized = normalize_url(raw)
            if normalized and (url is None or normalized == url):
                records.append((normalized, cid, mid, vid, observed))
    return records


Link = namedtuple("Link", "job published revisions files")


def _year(published):
    """Publication year of a message time; None when it cannot be read."""
    return int(published[:4]) if isinstance(published, str) and published[:4].isdigit() else None


def discover(db, channel_ids=None, after=None):
    """Links still to fetch, as (mark, [Link(job, published, revisions, files), ...]).

    `job` is (url, channel_id, message_id), `published` the time of the message
    naming the link, and `revisions` the edited message versions carrying it
    that no fetch has checked yet. A link is due when it has no article, or
    when such a revision exists: the page may have changed with the message.
    `files` are the (channel_id, year) source files of the messages this call
    saw carrying the link: the files its article will appear in.

    `mark` is the newest message version this call read. Passed back as
    `after`, only later versions are searched, so a running collector reads
    the whole archive once and not again at every message.
    """
    if channel_ids is not None and not channel_ids:
        return after, []
    chosen, params = "", []
    if channel_ids is not None:
        chosen = " AND channel_id IN (" + ",".join("?" for _ in channel_ids) + ")"
        params = sorted(channel_ids)
    rows = []   # channel, message, urls, published, then version, observed and edit time
    if after is None:
        rows = [(*row, None, None, None) for row in db.execute(
            "SELECT channel_id, message_id, urls, date_utc FROM messages "
            "WHERE trim(urls, '[]') != ''" + chosen, params)]
    mark = after
    # Edited URLs also need durable discovery after a restart. The main message
    # intentionally retains its first observed text and URLs.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='message_versions'").fetchone():
        mark = db.execute("SELECT max(rowid) FROM message_versions").fetchone()[0] or 0
        # Channels are filtered here, not in SQL: with a channel condition SQLite
        # walks the channel index over every version instead of the new rows only.
        rows.extend(row for row in db.execute(
            "SELECT channel_id, message_id, json_extract(snapshot_json,'$.urls'), "
            "json_extract(snapshot_json,'$.date_utc'), version_id, observed_at, edit_date_utc "
            "FROM message_versions WHERE rowid > ? AND rowid <= ? ORDER BY rowid", (after or 0, mark))
            if channel_ids is None or row[0] in channel_ids)

    found, files = {}, {}   # by URL: the link when it is due, and the source files carrying it
    for cid, mid, urls_json, published, vid, observed, edited in rows:
        year = _year(published)
        for raw in json.loads(urls_json or "[]"):
            url = normalize_url(raw)
            if should_skip(url):
                continue
            first = url not in files
            carried = files.setdefault(url, set())
            if year:
                carried.add((cid, year))
            if first and not db.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
                found[url] = Link((url, cid, mid), published, [], carried)
            if edited and not db.execute(
                "SELECT 1 FROM article_revision_checks WHERE url=? AND channel_id=? AND message_id=? AND version_id=?",
                (url, cid, mid, vid),
            ).fetchone():
                found.setdefault(url, Link((url, cid, mid), published, [], carried)).revisions.append(
                    (url, cid, mid, vid, observed))
    return mark, list(found.values())


def pending_urls(db, channel_ids=None):
    """Henüz çekilmemiş URL'leri döndürür: [(url, channel_id, message_id), ...]"""
    return [link.job for link in discover(db, channel_ids)[1]]


def failed(db, channel_ids=None, limit=None):
    """Links whose last fetch failed, oldest attempt first, as `discover` reports links."""
    if channel_ids is not None and not channel_ids:
        return []
    where, params = "WHERE status IN ('error','empty')", []
    if channel_ids is not None:
        where += " AND source_channel IN (" + ",".join("?" for _ in channel_ids) + ")"
        params.extend(sorted(channel_ids))
    where += " ORDER BY fetched_at,url"
    if limit is not None:
        if limit <= 0:
            raise ValueError("Limit pozitif olmalı")
        where += " LIMIT ?"
        params.append(limit)
    jobs = db.execute(f"SELECT url, source_channel, source_message FROM articles {where}", params).fetchall()
    if not jobs:
        return []
    # One pass over the archive for all of them, not one per link.
    edits, files = {}, {job[0]: set() for job in jobs}
    for revision in _edited_revisions(db):
        edits.setdefault(revision[0], []).append(revision)
    carriers = ["SELECT channel_id, urls, date_utc FROM messages WHERE trim(urls, '[]') != ''"]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='message_versions'").fetchone():
        carriers.append("SELECT channel_id, json_extract(snapshot_json,'$.urls'), "
                        "json_extract(snapshot_json,'$.date_utc') FROM message_versions")
    for query in carriers:
        for cid, urls_json, published in db.execute(query):
            year = _year(published)
            for raw in json.loads(urls_json or "[]") if year else ():
                carried = files.get(normalize_url(raw))
                if carried is not None:
                    carried.add((cid, year))
    return [Link(tuple(job), None, edits.get(job[0], []), files[job[0]]) for job in jobs]


def _newest_first(published):
    """Sort key by message time, newest first; an unreadable time goes last."""
    try:
        return -datetime.fromisoformat(published).timestamp()
    except (TypeError, ValueError):
        return 0.0


class LinkQueue:
    """Jobs waiting for a fetch: one at a time per site, the newest message first.

    It holds no connection and starts no thread: the caller takes a job, runs
    it and reports it done. Sites are told apart by host, as request spacing is.
    A site that turns a request away is left alone for a while, so its queue is
    not spent on refusals.
    """

    def __init__(self, parallel=PARALLEL_SITES):
        self.parallel = parallel
        self.sites = {}       # host -> heap of (time rank, arrival, job)
        self.queued = set()   # URLs in the heaps
        self.active = {}      # host -> URL being fetched
        self.revisions = {}   # URL -> edited message versions its next fetch checks
        self.files = {}       # URL -> (channel_id, year) source files carrying it
        self.resume = {}      # host -> monotonic time a refusing site may be tried again
        self.refusals = {}    # host -> refusals in a row
        self.arrival = itertools.count()

    def __len__(self):
        return len(self.queued)

    def add(self, links):
        """Queue what `discover` found; a link already waiting keeps its place."""
        for job, published, revisions, files in links:
            url = job[0]
            self.files.setdefault(url, set()).update(files)
            if url in self.active.values() and not revisions:
                continue   # The fetch under way stores it; only an edit asks for another.
            if revisions:
                waiting = self.revisions.setdefault(url, [])
                waiting.extend(r for r in revisions if r not in waiting)
            if url not in self.queued:
                self.queued.add(url)
                heapq.heappush(self.sites.setdefault(domain_of(url), []),
                               (_newest_first(published), next(self.arrival), job))

    def take(self):
        """(job, revisions) for the free site holding the newest message; None when no fetch can start."""
        if len(self.active) >= self.parallel:
            return None
        now = time.monotonic()
        free = [host for host in self.sites if host not in self.active and self.resume.get(host, 0) <= now]
        if not free:
            return None
        host = min(free, key=lambda name: self.sites[name][0])
        job = heapq.heappop(self.sites[host])[2]
        if not self.sites[host]:
            del self.sites[host]
        self.queued.discard(job[0])
        self.active[host] = job[0]
        return job, self.revisions.pop(job[0], [])

    def done(self, job, refused=False):
        """Free the job's site (after a refusal, only once its pause is over).

        Returns the source files carrying the link, whose content the fetch may have changed.
        """
        url, host = job[0], domain_of(job[0])
        self.active.pop(host, None)
        if refused:
            count = self.refusals[host] = self.refusals.get(host, 0) + 1
            self.resume[host] = time.monotonic() + min(REFUSAL_PAUSE * 2 ** min(count - 1, 10), REFUSAL_PAUSE_MAX)
        else:
            self.refusals.pop(host, None)
            self.resume.pop(host, None)
        # Queued again for an edit that came during the fetch: the files stay known for that one.
        return set(self.files[url] if url in self.queued else self.files.pop(url, ()))

    def paused(self):
        """Refusing sites with jobs waiting, as {host: seconds until the next try}."""
        now = time.monotonic()
        return {host: round(self.resume[host] - now) for host in self.sites if self.resume.get(host, 0) > now}


def _digest(title, text, final_url, content_type):
    return hashlib.sha256(json.dumps([title, text, final_url, content_type],
                                     ensure_ascii=False).encode()).hexdigest()


def _save_version(db, url, title, text, fetched_at, final_url, content_type, raw, extraction=None):
    digest = _digest(title, text, final_url, content_type)
    latest = db.execute("SELECT version_id,content_hash FROM article_versions WHERE url=? "
                        "ORDER BY fetched_at DESC,rowid DESC LIMIT 1", (url,)).fetchone()
    if latest and latest[1] == digest:
        vid = latest[0]
    else:
        vid = hashlib.sha256(f"{url}:{fetched_at}:{digest}".encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO article_versions VALUES (?,?,?,?,?,?,?,?,?)",
                   (url, vid, digest, title, text, final_url, content_type, fetched_at,
                    zlib.compress(raw) if raw is not None else None))
    if extraction:
        # A table of its own: the version tables keep the shape older code expects.
        db.execute("INSERT OR REPLACE INTO article_extractions VALUES (?,?,?)",
                   (url, vid, json.dumps(extraction, ensure_ascii=False)))
    return vid


def reextract(db, apply=False):
    """Read each article's latest stored page again with the current extractor.

    Yields (url, final_url, stored text, Page). With `apply`, the version is
    rewritten in place: it is the same download, not a change on the site, so
    it must not appear as a new version. The raw page stays as downloaded.
    """
    rows = db.execute(
        "SELECT url,version_id,title,text,final_url,content_type,raw_content_zlib "
        "FROM article_versions v WHERE raw_content_zlib IS NOT NULL AND rowid=("
        "SELECT v2.rowid FROM article_versions v2 WHERE v2.url=v.url "
        "ORDER BY v2.fetched_at DESC,v2.rowid DESC LIMIT 1) ORDER BY url"
    ).fetchall()
    for url, vid, title, stored, final, kind, compressed in rows:
        page = extract_page(zlib.decompress(compressed), final or url)
        if apply and page.text:
            title = page.title or title
            with db:
                db.execute("UPDATE article_versions SET title=?,text=?,content_hash=? WHERE url=? AND version_id=?",
                           (title, page.text, _digest(title, page.text, final, kind), url, vid))
                db.execute("INSERT OR REPLACE INTO article_extractions VALUES (?,?,?)",
                           (url, vid, json.dumps(page.details, ensure_ascii=False)))
                # Only where the article still shows this version's text.
                db.execute("UPDATE articles SET title=?,text=? WHERE url=? AND text IS ?",
                           (title, page.text, url, stored))
        yield url, final or url, stored, page


def fetch_urls(db, urls, delay=DEFAULT_DELAY, stop_event=None, revisions=None):
    """Fetch precisely these jobs, preserving existing records until completion.

    `revisions` maps a URL to the edited message versions its fetch answers for,
    as `discover` reports them. Without it they are looked up here, which reads
    every edited version in the archive once per URL.
    """
    stop = stop_event if stop_event is not None else _stop
    stats = {"ok": 0, "empty": 0, "error": 0, "skipped": 0, "refused": 0, "total": len(urls)}

    for i, (url, cid, mid) in enumerate(urls, 1):
        if stop.is_set():
            log.info("Makale çekimi durduruldu (%d/%d)", i, len(urls))
            break

        d = domain_of(url)

        if should_skip(url):
            stats["skipped"] += 1
            continue
        known = _edited_revisions(db, url) if revisions is None else revisions.get(url, [])
        log.info("[%d/%d] %s", i, len(urls), url)
        metadata = {}
        try:
            result = fetch_article(url, delay=delay, stop_event=stop)
            title, text, error = result
            metadata = getattr(result, "metadata", {})
        except Exception as e:
            log.warning("  ✗ %s: %s", url, e)
            error = str(e)
            title = text = None

        if stop.is_set():
            break  # Leave interrupted jobs pending rather than acknowledging them.

        if not error and not (text or "").strip():
            error = EmptyArticleError("metin çıkarılamadı")
        status = "empty" if isinstance(error, EmptyArticleError) else "error" if error else "ok"
        stats[status] += 1
        stats["refused"] += error in REFUSALS
        fetched_at = datetime.now(timezone.utc).isoformat()
        with db:
            if status == "ok":
                _save_version(db, url, title, text, fetched_at,
                              metadata.get("final_url", url), metadata.get("content_type"),
                              metadata.get("raw_content"), metadata.get("extraction"))
            db.execute(
            """INSERT INTO articles (url, domain, title, text, source_channel,
               source_message, status, error, fetched_at, final_url, content_type, last_checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 title=CASE WHEN excluded.status='ok' THEN excluded.title ELSE articles.title END,
                 text=CASE WHEN excluded.status='ok' THEN excluded.text ELSE articles.text END,
                 status=excluded.status, error=excluded.error,
                 fetched_at=CASE WHEN excluded.status='ok' THEN excluded.fetched_at ELSE articles.fetched_at END,
                 final_url=CASE WHEN excluded.status='ok' THEN excluded.final_url ELSE articles.final_url END,
                 content_type=CASE WHEN excluded.status='ok' THEN excluded.content_type ELSE articles.content_type END,
                 last_checked_at=excluded.last_checked_at""",
            (url, d, title, text, cid, mid, status, error,
             fetched_at, metadata.get("final_url", url), metadata.get("content_type"), fetched_at),
            )
            # Only acknowledge revisions known before the request. A concurrent
            # edit during the download must still cause another check.
            db.executemany("INSERT OR REPLACE INTO article_revision_checks VALUES (?,?,?,?,?)",
                           [(u, c, m, v, fetched_at) for u, c, m, v, _ in known])

        if status == "ok":
            log.info("  ✓ %s — %d karakter", title or "(başlıksız)", len(text))
        else:
            log.info("  ✗ %s", error)

    return stats


def _fetch_links(db, links, delay, stop_event=None):
    return fetch_urls(db, [link.job for link in links], delay, stop_event,
                      {link.job[0]: link.revisions for link in links})


def fetch_all(db, channel_ids=None, delay=DEFAULT_DELAY, limit=None, stop_event=None):
    """Tüm bekleyen URL'leri sırayla çeker. İlerlemeyi döndürür."""
    ensure_schema(db)
    links = discover(db, channel_ids)[1]
    if limit is not None:
        if limit <= 0:
            raise ValueError("Limit pozitif olmalı")
        links = links[:limit]
    return _fetch_links(db, links, delay, stop_event)


def retry_errors(db, channel_ids=None, delay=DEFAULT_DELAY, limit=None):
    """Daha önce başarısız olan URL'leri sırayla yeniden dener."""
    ensure_schema(db)
    return _fetch_links(db, failed(db, channel_ids, limit), delay)

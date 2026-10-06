#!/usr/bin/env python3
"""Telegram metin arşivleyici.

Geçmiş mesajları ve canlı metin olaylarını toplar; ilk gözlenen metni ve
sonraki sürümleri korur. Başlangıç ve yeniden bağlantı taraması boşlukları tamamlar.
Kaynak başına okunabilir JSON çıktısı için export-json kullanılır.
Kurulum, komutlar ve çalışma sınırları README.md'dedir.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageService,
    PeerChannel,
)
from telethon.utils import resolve_id
from .collector_runtime import Health, process_lock, read_health, setup_logging

log = logging.getLogger("tg")

DB_PATH = os.environ.get("TG_DB", "telegram_haber.db")
SESSION = os.environ.get("TG_SESSION", "haber_toplayici")
CHANNELS_FILE = os.environ.get("TG_CHANNELS", "channels.txt")
BATCH = 500
EXPORT_PAUSE = 60.0   # seconds before the same JSON source file is written again

# Sadece etiket biçimi: "Reklam yasağı getirildi" gibi gerçek haberler işaretlenmesin
SPONSOR_RE = re.compile(r"#(sponsorlu|sponsor|reklam|işbirliği|isbirligi|ad)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id      INTEGER PRIMARY KEY,
    username        TEXT,
    title           TEXT,
    first_post_utc  TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    date_utc        TEXT NOT NULL,
    edit_date_utc   TEXT,
    text            TEXT,
    views           INTEGER,
    forwards        INTEGER,
    fwd_channel_id  INTEGER,  -- forward ise orijinal kanal
    fwd_message_id  INTEGER,
    fwd_date_utc    TEXT,     -- forward ise orijinal yayın zamanı
    urls            TEXT,     -- JSON liste
    sponsored_guess INTEGER,  -- #sponsorlu/reklam içeriyor mu
    collected_at    TEXT,
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_msg_date ON messages(date_utc);
CREATE TABLE IF NOT EXISTS state (
    channel_id     INTEGER PRIMARY KEY,
    newest_id      INTEGER,
    oldest_id      INTEGER,
    backfill_done  INTEGER DEFAULT 0,
    since_utc      TEXT
);
CREATE TABLE IF NOT EXISTS message_versions (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    version_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    edit_date_utc TEXT,
    text TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id, version_id)
);
CREATE INDEX IF NOT EXISTS ix_version_observed
    ON message_versions(channel_id, message_id, observed_at);
CREATE TABLE IF NOT EXISTS channel_refs (
    ref TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS message_deletions (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(channel_id, message_id)
);
"""

MESSAGE_COLUMNS = (
    "channel_id message_id date_utc edit_date_utc text views forwards fwd_channel_id "
    "fwd_message_id fwd_date_utc urls sponsored_guess collected_at"
).split()
# A buffered observation may be written after a newer live edit. Compare capture
# times, not write order, and retain the earliest observed snapshot atomically.
INSERT_MSG = (
    "INSERT INTO messages (" + ",".join(MESSAGE_COLUMNS) + ") VALUES ("
    + ",".join("?" * len(MESSAGE_COLUMNS)) + ") ON CONFLICT(channel_id,message_id) DO UPDATE SET "
    + ",".join(f"{c}=excluded.{c}" for c in MESSAGE_COLUMNS[2:])
    + " WHERE excluded.collected_at < messages.collected_at"
)


# ---------------------------------------------------------------- yardımcılar

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def extract_urls(msg):
    from haber_extractor.url_utils import normalize_url
    urls = set()
    entity_urls = []
    for ent, txt in msg.get_entities_text():
        if isinstance(ent, MessageEntityTextUrl):
            entity_urls.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            entity_urls.append(txt)
    for raw in entity_urls:
        normalized = normalize_url(raw)
        if normalized:
            urls.add(normalized)
    for raw in URL_RE.findall(msg.message or ""):
        normalized = normalize_url(raw, prose=True)
        if normalized and raw not in entity_urls:
            urls.add(normalized)
    return sorted(urls)


def to_row(channel_id, msg):
    text = getattr(msg, "message", None) or ""
    fwd = getattr(msg, "fwd_from", None)
    fwd_ch = fwd_mid = fwd_date = None
    if fwd:
        if isinstance(fwd.from_id, PeerChannel):
            fwd_ch = fwd.from_id.channel_id
        fwd_mid = fwd.channel_post
        fwd_date = iso(fwd.date)
    return (
        channel_id, msg.id, iso(msg.date), iso(getattr(msg, "edit_date", None)), text,
        getattr(msg, "views", None), getattr(msg, "forwards", None), fwd_ch, fwd_mid, fwd_date,
        json.dumps(extract_urls(msg), ensure_ascii=False),
        int(bool(SPONSOR_RE.search(text))), now_iso(),
    )


def version_id(row):
    """Content-version identity; observation time and changing counters are excluded."""
    payload = [row[i] for i in (2, 3, 4, 7, 8, 9, 10)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def save_observation(db, row, source):
    # A cleared caption is a text-history event, not a new textless message.
    if not row[4].strip() and not db.execute(
        "SELECT 1 FROM messages WHERE channel_id=? AND message_id=?", row[:2]
    ).fetchone():
        return
    snapshot = dict(zip(MESSAGE_COLUMNS, row))
    vid = version_id(row)
    # Keep already exported version keys stable across the text-only migration.
    for old_id, old_json in db.execute(
        "SELECT version_id,snapshot_json FROM message_versions "
        "WHERE channel_id=? AND message_id=? AND text=? AND edit_date_utc IS ?",
        (row[0], row[1], row[4], row[3]),
    ):
        old = json.loads(old_json)
        if all(old.get(MESSAGE_COLUMNS[i]) == row[i] for i in (2, 3, 4, 7, 8, 9, 10)):
            vid = old_id
            break
    db.execute(INSERT_MSG, row)
    db.execute(
        """INSERT INTO message_versions VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(channel_id,message_id,version_id) DO UPDATE SET
             observed_at=excluded.observed_at, source=excluded.source,
             snapshot_json=excluded.snapshot_json
           WHERE excluded.observed_at < message_versions.observed_at""",
        (row[0], row[1], vid, row[-1], source, row[3], row[4],
         json.dumps(snapshot, ensure_ascii=False)),
    )


def open_db(path=None):
    db = sqlite3.connect(path if path is not None else DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    schema_version = db.execute("PRAGMA user_version").fetchone()[0]
    db.executescript(SCHEMA)
    if schema_version < 2:
        with db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            if "media_type" in columns or "grouped_id" in columns:
                create = SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS messages ("):].split(";", 1)[0]
                db.execute(create.replace("messages (", "messages_text_only (", 1))
                names = ",".join(MESSAGE_COLUMNS)
                db.execute(f"INSERT INTO messages_text_only ({names}) SELECT {names} FROM messages")
                db.execute("DROP TABLE messages")
                db.execute("ALTER TABLE messages_text_only RENAME TO messages")
                db.execute("CREATE INDEX ix_msg_date ON messages(date_utc)")
            # Strip media payloads while preserving every existing text version key.
            for cid, mid, vid, payload in db.execute(
                "SELECT channel_id,message_id,version_id,snapshot_json FROM message_versions"
            ).fetchall():
                snapshot = json.loads(payload)
                snapshot = {k: v for k, v in snapshot.items() if k in MESSAGE_COLUMNS}
                db.execute("UPDATE message_versions SET snapshot_json=? WHERE channel_id=? AND message_id=? AND version_id=?",
                           (json.dumps(snapshot, ensure_ascii=False), cid, mid, vid))
            for cid, mid in db.execute("SELECT channel_id,message_id FROM messages WHERE text IS NULL OR trim(text)=''").fetchall():
                first_text = db.execute(
                    "SELECT snapshot_json FROM message_versions WHERE channel_id=? AND message_id=? "
                    "AND trim(text)!='' ORDER BY observed_at,version_id LIMIT 1", (cid, mid)
                ).fetchone()
                db.execute("DELETE FROM messages WHERE channel_id=? AND message_id=?", (cid, mid))
                if first_text:
                    snapshot = json.loads(first_text[0])
                    db.execute(INSERT_MSG, [snapshot.get(k) for k in MESSAGE_COLUMNS])
                else:
                    db.execute("DELETE FROM message_versions WHERE channel_id=? AND message_id=?", (cid, mid))
            if schema_version < 1:
                for row in db.execute("SELECT " + ",".join(MESSAGE_COLUMNS) + " FROM messages").fetchall():
                    if row[-1] is None:
                        row = (*row[:-1], now_iso())
                        db.execute("UPDATE messages SET collected_at=? WHERE channel_id=? AND message_id=?",
                                   (row[-1], row[0], row[1]))
                    save_observation(db, row, "legacy")
            db.execute("PRAGMA user_version=2")
    return db


SETTINGS_KEYS = {"since", "since-days", "catchup-minutes", "recent-hours", "parallel-sites"}


def read_channel_list():
    if not os.path.exists(CHANNELS_FILE):
        sys.exit(f"{CHANNELS_FILE} bulunamadı. Önce 'channels' komutunu çalıştırıp listeyi oluştur.")
    refs = []
    settings = {}
    with open(CHANNELS_FILE, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" in line and line.split("=", 1)[0].strip() in SETTINGS_KEYS:
                key, val = line.split("=", 1)
                settings[key.strip()] = val.strip()
                continue
            line = line.replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
            refs.append(int(line) if line.lstrip("-").isdigit() else line)
    return refs, settings


def default_since(settings, now=None):
    """Kanal dosyasındaki başlangıç: sabit `since` tarihi ya da bugünden `since-days` gün geri (UTC)."""
    if "since-days" not in settings:
        return settings.get("since", "2026-08-01")
    if "since" in settings:
        raise ValueError("since ve since-days birlikte kullanılamaz")
    days = settings["since-days"]
    if not days.isdecimal() or int(days) <= 0:
        raise ValueError("since-days pozitif tam sayı olmalı")
    return ((now or datetime.now(timezone.utc)) - timedelta(days=int(days))).strftime("%Y-%m-%d")


def parallel_sites(settings):
    """Kanal dosyasındaki `parallel-sites`: aynı anda kaç siteden makale indirileceği; yazılmamışsa varsayılan."""
    from .article_fetcher import PARALLEL_SITES
    value = settings.get("parallel-sites")
    if value is None:
        return PARALLEL_SITES
    if not value.isdecimal() or not 1 <= int(value) <= 16:
        raise ValueError("parallel-sites 1 ile 16 arasında tam sayı olmalı")
    return int(value)


def active_channel_ids(db):
    """channels.txt'deki kanalların veritabanındaki ID'leri; listeden çıkarılan kanal yeni JSON çıktısına girmez."""
    ids = set()
    for ref in read_channel_list()[0]:
        if isinstance(ref, int):
            ids.add(resolve_id(ref)[0])  # -1001234567 -> 1234567
            continue
        row = db.execute("SELECT channel_id FROM channel_refs WHERE ref=?", (ref.lower(),)).fetchone()
        if not row:
            row = db.execute("SELECT channel_id FROM channels WHERE lower(username) = lower(?)", (ref,)).fetchone()
        if row:
            ids.add(row[0])
        else:
            log.warning("%s veritabanında yok (henüz backfill yapılmadı ya da kullanıcı adı değişti)", ref)
    return ids


def upsert_channel(db, entity, first_post=None):
    db.execute(
        """INSERT INTO channels(channel_id, username, title, first_post_utc, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(channel_id) DO UPDATE SET
             username=excluded.username, title=excluded.title,
             first_post_utc=COALESCE(excluded.first_post_utc, channels.first_post_utc),
             updated_at=excluded.updated_at""",
        (entity.id, getattr(entity, "username", None), entity.title, first_post, now_iso()),
    )


def save_state(db, cid, newest, oldest, done, since):
    db.execute(
        """INSERT INTO state(channel_id, newest_id, oldest_id, backfill_done, since_utc)
           VALUES (?,?,?,?,?)
           ON CONFLICT(channel_id) DO UPDATE SET
             newest_id=excluded.newest_id, oldest_id=excluded.oldest_id,
             backfill_done=excluded.backfill_done, since_utc=excluded.since_utc""",
        (cid, newest, oldest, done, since),
    )


# ---------------------------------------------------------------- komutlar

async def cmd_channels(client):
    rows = []
    skipped_groups = 0
    async for d in client.iter_dialogs():
        e = d.entity
        if not isinstance(e, Channel):
            continue
        if not e.broadcast:
            skipped_groups += 1
            continue
        first = await client.get_messages(e, limit=1, reverse=True)
        first_date = first[0].date.date().isoformat() if first else "?"
        uname = f"@{e.username}" if e.username else "(özel)"
        rows.append((first_date, d.id, e.title, uname))
        await asyncio.sleep(0.5)

    rows.sort()
    with open("channels_candidates.txt", "w", encoding="utf-8") as f:
        f.write("# Kanal ID   # başlık | kullanıcı adı | ilk mesaj tarihi\n")
        for first_date, did, title, uname in rows:
            line = f"{did}  # {title} | {uname} | ilk mesaj: {first_date}"
            f.write(line + "\n")
            print(line)
    print(f"\n{len(rows)} yayın kanalı yazıldı -> channels_candidates.txt "
          f"({skipped_groups} grup/sohbet atlandı)")


async def resolve_channels(client, db, health=None):
    """channels.txt'deki her satırı Telegram kanalına çevirir; çözülemeyeni atlayıp devam eder."""
    entities = []
    seen = set()
    for ref in read_channel_list()[0]:
        key = str(ref).lower()
        pinned = db.execute("SELECT channel_id FROM channel_refs WHERE ref=?", (key,)).fetchone()
        try:
            while True:
                try:
                    e = await client.get_entity(PeerChannel(pinned[0]) if pinned else ref)
                    break
                except FloodWaitError as ex:
                    if health:
                        health.data["resolution_errors"][key] = f"FloodWait: {ex.seconds} saniye"
                    log.warning("Kanal çözümleme bekliyor: %s, %d sn", ref, ex.seconds)
                    await asyncio.sleep(ex.seconds + 5)
            if not isinstance(e, Channel) or not e.broadcast:
                raise ValueError("Yayın kanalı değil")
        except Exception as ex:  # yanlış kullanıcı adı, ayrıldığın özel kanal vb.
            log.error("✗ %s çözülemedi: %s", ref, ex)
            if health:
                health.data["resolution_errors"][key] = str(ex)
            continue
        db.execute("INSERT OR REPLACE INTO channel_refs VALUES (?,?)", (key, e.id))
        if health:
            health.data["resolution_errors"].pop(key, None)
            health.channel(e.id, title=e.title, status="pending")
        if e.id not in seen:
            upsert_channel(db, e)
            entities.append(e)
            seen.add(e.id)
    db.commit()
    return entities


async def backfill_channel(client, db, entity, since, health=None):
    cid = entity.id
    since_s = since.isoformat()

    st = db.execute(
        "SELECT newest_id, oldest_id, backfill_done, since_utc FROM state WHERE channel_id=?", (cid,)
    ).fetchone()
    newest, oldest, done, prev_since = st if st else (None, None, 0, None)
    if newest is None:
        done = 0  # Empty/old-only channels must be checked again next run.
    if prev_since and since_s < prev_since:
        done = 0  # daha geriye gitmek istendi, eski uçtan devam et

    # İlk mesaj tarihi biliniyorsa sonraki eksik kontrollerinde tekrar istenmez.
    known = db.execute("SELECT first_post_utc FROM channels WHERE channel_id=?", (cid,)).fetchone()
    if not (known and known[0]):
        first = await client.get_messages(entity, limit=1, reverse=True)
        upsert_channel(db, entity, iso(first[0].date) if first else None)

    buf, total = [], 0

    def flush():
        nonlocal buf, total
        if buf:
            for row in buf:
                save_observation(db, row, "backfill")
            total += len(buf)
            buf = []
        save_state(db, cid, newest, oldest, done, since_s)
        db.commit()
        if health:
            health.channel(cid, last_progress_at=now_iso())

    def take(msg):
        nonlocal newest, oldest
        newest = max(newest or 0, msg.id)
        oldest = msg.id if oldest is None else min(oldest, msg.id)
        if isinstance(msg, MessageService) or not (msg.message or "").strip():
            return
        buf.append(to_row(cid, msg))
        if len(buf) >= BATCH:
            flush()
            log.info("  %s: %d mesaj, %s", entity.title, total, msg.date.date())

    # 1) Son çalıştırmadan bu yana gelen yeni mesajlar. Eskiden yeniye okuyoruz ki
    #    newest hep ileri gitsin; yarıda kesilirse kaydedilen newest'e kadar her şey alınmış olur.
    if newest:
        async for msg in client.iter_messages(entity, min_id=newest, reverse=True, wait_time=1):
            take(msg)
        flush()

    # 2) Geçmişe doğru, --since tarihine kadar
    if not done:
        async for msg in client.iter_messages(entity, offset_id=oldest or 0, wait_time=1):
            if msg.date < since:
                done = 1
                break
            take(msg)
        else:
            done = 1  # kanalın başına ulaşıldı
        flush()

    log.info("✓ %s: %d mesaj kaydedildi", entity.title, total)


async def refresh_recent(client, db, entity, since, hours=48, health=None):
    """Re-observe a bounded publication-time window without moving history cursors."""
    cutoff = max(since, datetime.now(timezone.utc) - timedelta(hours=hours))
    count = 0
    async for msg in client.iter_messages(entity, wait_time=1):
        if msg.date < cutoff:
            break
        if isinstance(msg, MessageService):
            continue
        save_observation(db, to_row(entity.id, msg), "recent_scan")
        count += 1
        if count % BATCH == 0:
            db.commit()
            if health:
                health.channel(entity.id, last_progress_at=now_iso())
    db.commit()
    if health:
        health.channel(entity.id, last_recent_scan_at=now_iso(), last_progress_at=now_iso())
    return count


async def cmd_backfill(client, db, entities, since, recent_hours=48, health=None, on_channel=None):
    failures = []
    for entity in entities:
        while True:
            try:
                if health:
                    health.channel(entity.id, status="scanning", last_scan_started_at=now_iso(), waiting_until=None)
                await refresh_recent(client, db, entity, since, recent_hours, health)
                await backfill_channel(client, db, entity, since, health)
                if health:
                    health.channel(entity.id, status="ok", last_success_at=now_iso(), waiting_until=None)
                break
            except FloodWaitError as e:
                log.warning("FloodWait: %d sn bekleniyor (%s)", e.seconds, entity.title)
                if health:
                    health.channel(entity.id, status="waiting", waiting_until=iso(
                        datetime.now(timezone.utc) + timedelta(seconds=e.seconds + 5)))
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:  # erişilemeyen kanal vb. -> diğerlerine devam
                db.rollback()
                log.exception("✗ %s atlandı", entity.title)
                failures.append(entity.id)
                if health:
                    health.error(entity.id, e)
                break
        if on_channel:
            on_channel(entity.id)
    return failures


async def reconciliation_loop(client, db, entities, since, wakeup, recent_hours=48,
                              health=None, on_scan=None):
    """Scan at startup and after reconnect; coalesce events without parallel scans."""
    while True:
        await wakeup.wait()
        # Clear before scanning so a reconnect during the scan schedules another pass.
        wakeup.clear()
        try:
            await cmd_backfill(client, db, entities, since, recent_hours, health,
                               on_channel=on_scan)
            if on_scan:
                on_scan()
        except (sqlite3.Error, OSError):
            raise  # Storage/export failures must reach the process supervisor.
        except Exception as exc:
            log.exception("Eksik taraması başarısız; sonraki bağlantıda yeniden denenecek")
            if health:
                for entity in entities:
                    health.error(entity.id, exc)


class SourceFiles:
    """Keeps the yearly JSON source files current without rewriting them needlessly.

    A file is written again when a message or an article of its year changed:
    one file at a time, each at most once per EXPORT_PAUSE, and with rests that
    leave nine tenths of the event loop's time to the collector. Only the
    event-loop thread writes JSON; article threads never export.
    """

    def __init__(self, db, channel_ids, directory):
        self.db, self.channel_ids, self.directory = db, channel_ids, directory
        self.waiting = set()   # (channel_id, year) files to write
        self.written = {}      # file -> loop time its last write ended
        self.rest = 0.0        # loop time before which no write starts
        self.seen = self._newest_version()
        self.kick = asyncio.Event()

    def _newest_version(self):
        return self.db.execute("SELECT max(rowid) FROM message_versions").fetchone()[0] or 0

    def touch(self, files=None):
        """Ask for these (channel_id, year) files to be written; None asks for every file."""
        from .news_export import source_files
        self.waiting |= source_files(self.db, self.channel_ids) if files is None else {
            file for file in files if file[0] in self.channel_ids}
        self.kick.set()

    def touch_new_messages(self):
        """Ask for the files of the message versions stored since the last call."""
        newest = self._newest_version()
        self.touch({(cid, int(year)) for cid, year in self.db.execute(
            "SELECT DISTINCT channel_id, substr(json_extract(snapshot_json,'$.date_utc'),1,4) "
            "FROM message_versions WHERE rowid > ? AND rowid <= ?", (self.seen, newest))
            if year and year.isdigit()})
        self.seen = newest

    def write(self, file):
        from .news_export import export_sources
        self.waiting.discard(file)
        log.info("Kaynak JSON dosyası güncellendi: %s",
                 export_sources(self.db, self.channel_ids, self.directory, {file}))

    def flush(self):
        """Write every waiting file now: the last word before the collector stops."""
        for file in sorted(self.waiting):
            self.write(file)

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            due = {file: self.written.get(file, float("-inf")) + EXPORT_PAUSE for file in self.waiting}
            ready = min((file for file in due if due[file] <= now), key=due.get, default=None)
            if ready is not None and now >= self.rest:
                self.write(ready)
                ended = loop.time()
                self.written[ready] = ended
                self.rest = ended + 9 * (ended - now)
                await asyncio.sleep(0)
                continue
            self.kick.clear()
            wait = max(0.0, max(self.rest, min(due.values())) - now) if due else None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.kick.wait(), wait)


async def article_worker(path, channel_ids, wakeup=None, changed=None, health=None, parallel=None,
                         links=None, delay=None):
    """Fetch article links, several sites at once and the newest message first.

    A site gets one request at a time. `changed` is told the (channel_id, year)
    source files each finished fetch touches.

    In a running collector the worker goes on until cancelled: `wakeup` asks
    for a look at the archive, where the first look reads all of it and later
    ones only the message versions added since. Given `links` instead, it
    fetches exactly those and returns their summed result; links still held by
    a site that keeps turning requests away are left unfetched and counted
    under "left".
    """
    from .article_fetcher import DEFAULT_DELAY, PARALLEL_SITES, LinkQueue, discover, fetch_urls
    if not path:
        # In-memory collectors used by offline callers have no persistent
        # database a worker thread could open.
        await asyncio.Event().wait()
        return
    stop = threading.Event()
    queue = LinkQueue(parallel or PARALLEL_SITES)
    executor = ThreadPoolExecutor(max_workers=queue.parallel + 1, thread_name_prefix="article-fetcher")
    loop = asyncio.get_running_loop()
    running, total, scan, mark, waiter, look = {}, {}, None, None, None, False
    if links is not None:
        queue.add(links)

    def search(after):
        with contextlib.closing(sqlite3.connect(path, timeout=30)) as fdb:
            return discover(fdb, channel_ids, after)

    def fetch_one(job, revisions):
        with contextlib.closing(sqlite3.connect(path, timeout=30)) as fdb:
            return fetch_urls(fdb, [job], DEFAULT_DELAY if delay is None else delay, stop,
                              revisions={job[0]: revisions})

    def finish(job, stats):
        files = queue.done(job, refused=bool(stats["refused"]))
        for key, value in stats.items():
            total[key] = total.get(key, 0) + value
        if changed and stats["ok"] + stats["empty"] + stats["error"]:
            changed(files)   # A URL may be carried by several channels and years.

    def report(**fields):
        if health:
            health.data.setdefault("articles", {}).update(
                pending=len(queue) + len(running), active=len(running), paused=queue.paused(),
                status="running", **fields)

    try:
        while True:
            if links is None and waiter is None:
                waiter = asyncio.ensure_future(wakeup.wait())
            if look and scan is None:
                look = False
                scan = loop.run_in_executor(executor, search, mark)
            while (taken := queue.take()) is not None:
                # Existing URLs can need another fetch after a Telegram edit.
                running[loop.run_in_executor(executor, fetch_one, *taken)] = taken[0]
            report()
            if links is not None and not running:
                # All fetched, or only sites that turn requests away still hold links.
                return {**total, "left": len(queue), "paused": sorted(queue.paused())}
            # Wake at least once a minute, and when a refusing site's pause ends.
            done, _ = await asyncio.wait({*running, *filter(None, (scan, waiter))},
                                         timeout=max(1, min([60, *queue.paused().values()])),
                                         return_when=asyncio.FIRST_COMPLETED)
            if links is None and (not done or waiter in done):
                look = True
            if waiter in done:
                wakeup.clear()
                waiter = None
            if scan in done:
                mark, found = scan.result()
                scan = None
                queue.add(found)
            for future in [f for f in done if f in running]:
                job = running.pop(future)
                stats = future.result()
                finish(job, stats)
                if stats["refused"]:
                    log.warning("Site isteği geri çevirdi, kuyruğu bekletiliyor: %s", queue.paused() or job[0])
                report(last_completed_at=now_iso(), last_result=stats)
    finally:
        stop.set()
        if waiter is not None:
            waiter.cancel()
        try:
            unfinished = [*running, *([scan] if scan else [])]
            if unfinished:
                await asyncio.shield(asyncio.gather(*unfinished, return_exceptions=True))
            for future, job in running.items():
                if not future.cancelled() and not future.exception():
                    finish(job, future.result())   # Stored while stopping: its files still change.
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            if health:
                health.data.setdefault("articles", {})["status"] = "stopped"


def cmd_fetch_articles(db, channel_ids, json_dir, retry=False, limit=None, delay=None, parallel=None):
    """Fetch the pending links of these channels (with `retry`, the failed ones) and rewrite the JSON.

    The links go through the collector's own article worker, so sites are
    fetched in parallel under the same rules as in `live`. Returns the worker's
    result with "selected", the number of links chosen.
    """
    from .article_fetcher import discover, ensure_schema, failed
    from .news_export import export_sources
    ensure_schema(db)
    links = failed(db, channel_ids, limit) if retry else discover(db, channel_ids)[1][:limit]
    path = next((p for _, name, p in db.execute("PRAGMA database_list") if name == "main"), "")
    if links and not path:
        raise ValueError("fetch-articles bir veritabanı dosyası ister")
    stats = {"left": 0, "paused": []}
    if links:
        stats = asyncio.run(article_worker(path, channel_ids, links=links, delay=delay, parallel=parallel))
    export_sources(db, channel_ids, json_dir)
    return {**stats, "selected": len(links)}


async def cmd_live(client, db, entities, since, recent_hours=48, health=None, json_dir=None, parallel=None):
    if not entities:
        log.warning("Dinlenecek geçerli yayın kanalı yok")
        return
    fatal = asyncio.get_running_loop().create_future()
    from .article_fetcher import ensure_schema
    ensure_schema(db)
    channel_ids = {e.id for e in entities}
    article_wakeup = asyncio.Event()
    article_wakeup.set()
    files = SourceFiles(db, channel_ids, json_dir) if json_dir else None

    def after_scan(cid=None):
        if files:
            files.touch_new_messages()
        article_wakeup.set()

    def report_failure(exc):
        db.rollback()
        log.exception("Canlı olay kaydedilemedi")
        if health:
            health.data["last_live_error"] = str(exc)
        if not fatal.done():
            fatal.set_exception(exc)

    async def handle(ev):
        msg = ev.message
        if isinstance(msg, MessageService) or not isinstance(msg.peer_id, PeerChannel):
            return
        source = "live_edit" if isinstance(ev, events.MessageEdited.Event) else "live_new"
        # state'e dokunmuyoruz: live kakalıyken kaçan mesajları bir sonraki backfill toplar
        try:
            row = to_row(msg.peer_id.channel_id, msg)
            save_observation(db, row, source)
            db.commit()
            if health:
                health.channel(msg.peer_id.channel_id, last_live_event_at=now_iso())
            log.info("Canlı kayıt: kanal=%s mesaj=%s tür=%s", msg.peer_id.channel_id, msg.id, source)
            after_scan(msg.peer_id.channel_id)
        except Exception as exc:
            report_failure(exc)

    async def handle_deleted(ev):
        if ev.chat_id is None:
            return
        cid = resolve_id(ev.chat_id)[0]
        if cid not in {e.id for e in entities}:
            return
        try:
            with db:
                db.executemany("INSERT OR IGNORE INTO message_deletions VALUES (?,?,?)",
                               [(cid, mid, now_iso()) for mid in ev.deleted_ids])
            if health:
                health.channel(cid, last_live_event_at=now_iso())
        except Exception as exc:
            report_failure(exc)

    client.add_event_handler(handle, events.NewMessage(chats=entities))
    client.add_event_handler(handle, events.MessageEdited(chats=entities))
    client.add_event_handler(handle_deleted, events.MessageDeleted(chats=entities))
    reconcile_wakeup = asyncio.Event()
    reconcile_wakeup.set()  # Startup: new channels and messages missed while stopped.
    if health:
        health.data["scan_mode"] = "on_reconnect"
    # Telethon 1.45.0 schedules this callback on every successful auto-reconnect.
    # Preserve its update recovery logic; polling is unnecessary and can miss brief outages.
    sender = getattr(client, "_sender", None)
    original_reconnect = getattr(sender, "_auto_reconnect_callback", None)

    async def on_reconnect():
        reconcile_wakeup.set()
        if health:
            health.data["last_reconnected_at"] = now_iso()
        log.info("Telegram bağlantısı geri geldi; eksikler ve son %s saat kontrol edilecek", recent_hours)
        if original_reconnect:
            await original_reconnect()

    if sender is not None:
        sender._auto_reconnect_callback = on_reconnect

    log.info("%d kanal dinleniyor; geçmiş kontrolü başlangıçta ve yeniden bağlantıda", len(entities))
    task = asyncio.create_task(reconciliation_loop(
        client, db, entities, since, reconcile_wakeup, recent_hours, health, after_scan))
    # Use the actual collector DB, not a module default.
    path = next((p for _, name, p in db.execute("PRAGMA database_list") if name == "main"), "")
    article_task = asyncio.create_task(article_worker(
        path, channel_ids, article_wakeup, files.touch if files else None, health, parallel))
    disconnected = asyncio.create_task(client.run_until_disconnected())
    tasks = [task, article_task, disconnected, fatal]
    if files:
        files.touch()   # Whatever changed while the collector was stopped.
        tasks.append(asyncio.create_task(files.run()))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            await finished
    finally:
        if sender is not None:
            sender._auto_reconnect_callback = original_reconnect
        if hasattr(client, "remove_event_handler"):
            client.remove_event_handler(handle)
            client.remove_event_handler(handle_deleted)
        for pending in tasks:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if files:
            files.flush()


# ---------------------------------------------------------------- giriş

def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Pozitif, sonlu bir sayı gerekli")
    return number


async def run_collector(args, db, health, api_id, api_hash):
    client = TelegramClient(SESSION, int(api_id), api_hash)
    client.flood_sleep_threshold = 0  # Rate-limit waits must be visible in health/logs.

    async def work():
        try:
            await client.connect()
            if args.cmd == "login":
                await client.start()
                log.info("Telegram oturumu hazır")
                return
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram oturumu yok/geçersiz. Önce etkileşimli 'login' komutunu çalıştır.")
            health.data["status"] = "running"
            await client.get_dialogs()
            if args.cmd == "channels":
                await cmd_channels(client)
                return
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
            entities = await resolve_channels(client, db, health)
            if not entities:
                raise RuntimeError("Toplanacak geçerli kanal bulunamadı")
            if args.cmd == "backfill":
                failures = await cmd_backfill(client, db, entities, since, args.recent_hours, health)
                if failures or health.data["resolution_errors"]:
                    raise RuntimeError("Bazı kanallar tamamlanamadı; sağlık kaydını kontrol et")
            else:
                await cmd_live(client, db, entities, since, args.recent_hours, health,
                               args.json_dir, getattr(args, "parallel_sites", None))
                raise ConnectionError("Canlı Telegram bağlantısı sonlandı; servis yeniden başlatmalı")
        finally:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(client.disconnect(), timeout=15)

    worker = asyncio.create_task(work())
    heartbeat = asyncio.create_task(health.loop(db, client))
    owner = asyncio.current_task()
    loop = asyncio.get_running_loop()
    previous = signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(owner.cancel))
    failed = False
    try:
        done, _ = await asyncio.wait([worker, heartbeat], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    except Exception as exc:
        failed = True
        health.data["last_error"] = str(exc)
        raise
    finally:
        for task in (worker, heartbeat):
            if not task.done():
                task.cancel()
        await asyncio.gather(worker, heartbeat, return_exceptions=True)
        signal.signal(signal.SIGTERM, previous)
        health.data["status"] = "failed" if failed else "stopped"
        health.publish(db, client)


def load_env(path):
    """Basit .env okuyucu: KEY=VALUE satırlarını os.environ'a yükler (mevcut değilse)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if key and val and key not in os.environ:
                os.environ[key] = val.strip("\"'")


def configure_paths():
    """Resolve paths after .env loading, while respecting parent process settings."""
    global DB_PATH, SESSION, CHANNELS_FILE
    DB_PATH = os.environ.get("TG_DB", "telegram_haber.db")
    SESSION = os.environ.get("TG_SESSION", "haber_toplayici")
    CHANNELS_FILE = os.environ.get("TG_CHANNELS", "channels.txt")


def main():
    # .env varsa yükle (local-pilot/.env veya kök .env)
    for env_path in ("local-pilot/.env", ".env"):
        load_env(env_path)
    configure_paths()

    p = argparse.ArgumentParser(description="Telegram haber kanalı arşivleyici")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels", help="katıldığın kanalları ve ilk mesaj tarihlerini listele")
    sub.add_parser("login", help="sunucuya almadan önce etkileşimli Telegram girişi yap")
    h = sub.add_parser("health", help="sağlık JSON'u; sorun varsa çıkış kodu 1")
    h.add_argument("--max-age-seconds", type=positive_float, default=120)
    h.add_argument("--scan-max-age-minutes", type=positive_float, default=60)
    fetchers = {}
    for name, help_ in [("backfill", "channels.txt'deki kanalların geçmişini çek"),
                        ("live", "önce eksikleri çek, sonra yeni mesajları canlı dinle")]:
        fetchers[name] = sp = sub.add_parser(name, help=help_)
        sp.add_argument("--since", default=None, help="YYYY-MM-DD (varsayılan: kanal dosyasındaki since ya da since-days)")
        sp.add_argument("--recent-hours", type=positive_float, default=None,
                        help="düzenlemeler için yeniden taranan yayın zamanı penceresi (48 saat)")
    fetchers["live"].add_argument("--catchup-minutes", type=positive_float, default=None,
                                  help=argparse.SUPPRESS)  # Legacy option accepted, no periodic scans.
    readable = sub.add_parser("export-json", help="toplanan metinleri okunabilir JSON olarak yaz")
    readable.add_argument("--out-dir", default="sources", help="kanal ve yıl başına JSON dosyalarının klasörü")
    fetchers["live"].add_argument("--json-dir", help="kanal ve yıl başına güncellenen JSON dosyalarının klasörü")
    fa = sub.add_parser("fetch-articles", help="mesajlardaki URL'lerden makale tam metnini çek")
    fa.add_argument("--delay", type=positive_float, default=2.0, help="aynı domain'e istekler arası saniye (varsayılan 2)")
    fa.add_argument("--limit", type=int, default=None, help="en fazla kaç URL çekilsin")
    fa.add_argument("--retry", action="store_true", help="daha önce başarısız olanları yeniden dene")
    fa.add_argument("--json-dir", default="sources", help="çekim sonrası güncellenecek kaynak JSON klasörü")
    args = p.parse_args()

    # channels.txt'deki ayarları CLI'ye varsayılan olarak uygula (CLI öncelikli)
    file_settings = read_channel_list()[1] if args.cmd in {"live", "backfill", "export-json", "fetch-articles"} else {}
    if hasattr(args, "since") and args.since is None:
        try:
            args.since = default_since(file_settings)
        except ValueError as exc:
            p.error(f"{CHANNELS_FILE}: {exc}")
    if hasattr(args, "recent_hours") and args.recent_hours is None:
        args.recent_hours = positive_float(file_settings.get("recent-hours", "48"))
    if getattr(args, "catchup_minutes", None) is not None or "catchup-minutes" in file_settings:
        log.warning("catchup-minutes artık kullanılmıyor; kontrol başlangıçta ve yeniden bağlantıda yapılır")
    try:
        args.parallel_sites = parallel_sites(file_settings)
    except ValueError as exc:
        p.error(f"{CHANNELS_FILE}: {exc}")
    if args.cmd == "fetch-articles" and args.limit is not None and args.limit <= 0:
        p.error("--limit pozitif olmalı")

    health_path = os.environ.get("TG_HEALTH_FILE", DB_PATH + ".health.json")
    if args.cmd == "health":
        data, ok = read_health(health_path, args.max_age_seconds, args.scan_max_age_minutes * 60)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)
    if hasattr(args, "since"):
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            p.error("--since YYYY-MM-DD biçiminde olmalı")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if args.cmd == "export-json":
        from .news_export import export_sources
        with contextlib.closing(open_db()) as db:
            counts = export_sources(db, active_channel_ids(db), args.out_dir)
            print(f"{sum(counts.values())} metin, {len(counts)} kaynak dosyası -> {args.out_dir}")
        return
    if args.cmd == "fetch-articles":
        with contextlib.closing(open_db()) as db:
            stats = cmd_fetch_articles(db, active_channel_ids(db), args.json_dir, args.retry, args.limit,
                                       args.delay, args.parallel_sites)
            print(f"Tamamlandı: {stats.get('ok', 0)} başarılı, {stats.get('empty', 0)} boş, "
                  f"{stats.get('error', 0)} hata / {stats['selected']} toplam")
            if stats["left"]:
                print(f"{stats['left']} bağlantı denenmedi; istekleri geri çeviren site: {', '.join(stats['paused'])}")
        return

    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("TG_API_ID ve TG_API_HASH ortam değişkenlerini ayarla (my.telegram.org).")

    session_path = SESSION if SESSION.endswith(".session") else SESSION + ".session"
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(process_lock(DB_PATH))
            stack.enter_context(process_lock(session_path))
            setup_logging(os.environ.get("TG_LOG_FILE", DB_PATH + ".log"))
            db = stack.enter_context(contextlib.closing(open_db()))
            health = Health(health_path, args.cmd)
            asyncio.run(run_collector(args, db, health, api_id, api_hash))
    except (KeyboardInterrupt, asyncio.CancelledError):
        from .article_fetcher import request_stop
        request_stop()
        log.info("Toplayıcı durduruldu")
    except Exception:
        log.exception("Toplayıcı durdu")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Offline regression tests for durable URL discovery and live article workers."""
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from lxml import etree

from news_archive import article_fetcher as af
from haber_extractor import news_extractor as extractor
from news_archive import tg_collector as tg
from news_archive import news_export as ne
from tests.helpers import channel, dt, message


def save_url(db, mid, url, cid=1, edited=False):
    msg = message(mid, f"Haber {url}", cid=cid, edit=dt() if edited else None)
    tg.save_observation(db, tg.to_row(cid, msg), "live_edit" if edited else "backfill")
    db.commit()


class ArticleTests(unittest.TestCase):
    def setUp(self):
        af.reset_stop()
        self.db = tg.open_db(":memory:")
        self.addCleanup(self.db.close)
        af.ensure_schema(self.db)

    def test_empty_selection_never_fetches_other_channels(self):
        save_url(self.db, 1, "https://example.test/news")
        self.assertEqual(af.pending_urls(self.db, set()), [])
        with patch.object(af, "fetch_article") as fetch:
            self.assertEqual(af.fetch_all(self.db, set())["total"], 0)
            self.assertEqual(af.retry_errors(self.db, set())["total"], 0)
            fetch.assert_not_called()

    def test_edited_url_survives_restart_discovery(self):
        save_url(self.db, 1, "https://example.test/first")
        save_url(self.db, 1, "https://example.test/edited", edited=True)
        self.assertEqual({u for u, _, _ in af.pending_urls(self.db, {1})},
                         {"https://example.test/first", "https://example.test/edited"})

    def test_http_network_and_extraction_errors_are_distinct(self):
        for error, expected in [("HTTP 503", "error"), ("bağlantı hatası: timeout", "error"),
                                (af.EmptyArticleError("metin çıkarılamadı"), "empty")]:
            with self.subTest(error=error), patch.object(af, "fetch_article", return_value=(None, None, error)):
                stats = af.fetch_urls(self.db, [("https://example.test/test", 1, 1)], delay=0)
                self.assertEqual(stats[expected], 1)
                self.assertEqual(self.db.execute("SELECT status FROM articles").fetchone()[0], expected)

    def test_retry_limit_targets_failed_url_without_deleting_record(self):
        save_url(self.db, 1, "https://example.test/new")
        save_url(self.db, 2, "https://example.test/failed")
        self.db.execute("INSERT INTO articles(url,domain,source_channel,source_message,status,error,fetched_at) "
                        "VALUES(?,?,?,?,?,?,?)", ("https://example.test/failed", "example.test", 1, 2,
                                                "error", "HTTP 503", tg.now_iso()))
        self.db.commit()
        def fetch(url, **kwargs):
            self.assertEqual(url, "https://example.test/failed")
            self.assertEqual(self.db.execute("SELECT status FROM articles WHERE url=?", (url,)).fetchone()[0], "error")
            return "Title", "Article", None
        with patch.object(af, "fetch_article", side_effect=fetch):
            self.assertEqual(af.retry_errors(self.db, {1}, delay=0, limit=1)["ok"], 1)
        self.assertEqual(self.db.execute("SELECT url,status FROM articles").fetchall(),
                         [("https://example.test/failed", "ok")])

    def test_stop_preserves_failed_record(self):
        self.db.execute("INSERT INTO articles(url,domain,status,fetched_at) VALUES(?,?,?,?)",
                        ("https://example.test/failed", "example.test", "error", tg.now_iso()))
        self.db.commit()
        af.request_stop()
        self.addCleanup(af.reset_stop)
        with patch.object(af, "fetch_article") as fetch:
            af.retry_errors(self.db, delay=0)
            fetch.assert_not_called()
        self.assertEqual(self.db.execute("SELECT status FROM articles").fetchone()[0], "error")

    def test_domain_spacing_is_shared_between_calls(self):
        limiter = extractor.DomainLimiter()
        stop = threading.Event()
        with patch.object(extractor.time, "monotonic", return_value=100), patch.object(stop, "wait", return_value=False) as wait:
            self.assertTrue(limiter.wait("site.test", 2, stop))
            self.assertTrue(limiter.wait("other.test", 2, stop))
            self.assertTrue(limiter.wait("site.test", 2, stop))
            wait.assert_called_once_with(2)

    def test_tables_are_included(self):
        response = SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"},
                                   iter_content=lambda **kwargs: iter([b"<html>article</html>"]), close=lambda: None)
        with patch.object(extractor, "_get_session", return_value=SimpleNamespace(get=lambda *a, **k: response)), \
                patch.object(extractor.trafilatura, "bare_extraction", return_value=SimpleNamespace(body=etree.fromstring("<body><p>table text</p></body>"))) as extract, \
                patch.object(extractor.trafilatura, "extract_metadata", return_value=None):
            self.assertEqual(af.fetch_article("https://example.test/table")[1], "table text")
        self.assertTrue(extract.call_args.kwargs["include_tables"])

    def test_paths_are_resolved_after_env_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('TG_DB="configured.db"\nTG_SESSION=configured\nTG_CHANNELS=selected.txt\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(tg, "DB_PATH"), \
                    patch.object(tg, "SESSION"), patch.object(tg, "CHANNELS_FILE"):
                tg.load_env(path)
                tg.configure_paths()
                self.assertEqual((tg.DB_PATH, tg.SESSION, tg.CHANNELS_FILE),
                                 ("configured.db", "configured", "selected.txt"))


class LiveArticleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_scan_continues_during_fetch_and_new_urls_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = tg.open_db(Path(tmp) / "archive.db")
            self.addCleanup(db.close)
            tg.upsert_channel(db, channel())
            save_url(db, 1, "https://first.test/news")
            started, release = threading.Event(), threading.Event()
            finished = asyncio.Event()
            scans = 0
            fetched = []

            def fetch(url, **kwargs):
                fetched.append(url)
                if url == "https://first.test/news":
                    started.set()
                    if not release.wait(2):
                        raise RuntimeError("reconnect scan blocked by article worker")
                return "Title", "Article text", None

            async def scan(*args, **kwargs):
                nonlocal scans
                scans += 1
                if scans >= 2 and started.is_set():
                    save_url(db, 2, "https://second.test/news")
                    release.set()
                if kwargs.get("on_channel"):
                    kwargs["on_channel"](1)
                return []

            async def disconnected():
                while not started.is_set():
                    await asyncio.sleep(0.01)
                await client._sender._auto_reconnect_callback()
                target = Path(tmp) / "sources/channel_1_2026.json"
                for _ in range(300):
                    if target.exists():
                        data = json.loads(target.read_text(encoding="utf-8"))["messages"]
                        if len(data) == 2 and all(r["articles"] for r in data):
                            finished.set()
                            return
                    await asyncio.sleep(0.01)
                raise TimeoutError("articles were not exported")

            client = SimpleNamespace(add_event_handler=lambda *a: None, run_until_disconnected=disconnected,
                                     _sender=SimpleNamespace(_auto_reconnect_callback=AsyncMock()))
            try:
                with patch.object(tg, "cmd_backfill", side_effect=scan), patch.object(af, "fetch_article", side_effect=fetch), \
                        patch.object(tg, "EXPORT_PAUSE", 0):
                    await asyncio.wait_for(tg.cmd_live(client, db, [channel()], dt(2022),
                                                     json_dir=Path(tmp) / "sources"), 4)
            finally:
                release.set()
                db.close()
            self.assertTrue(finished.is_set())
            self.assertGreaterEqual(scans, 2)
            self.assertEqual(fetched, ["https://first.test/news", "https://second.test/news"])

    async def test_url_free_live_message_is_exported_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = tg.open_db(":memory:")
            self.addCleanup(db.close)
            tg.upsert_channel(db, channel())
            handlers = []
            async def disconnected():
                await handlers[0](tg.events.NewMessage.Event(message(text="New text without a URL")))
                path = Path(tmp) / "channel_1_2026.json"
                for _ in range(200):   # written on the event loop's next turns, not after a pause
                    if path.exists():
                        break
                    await asyncio.sleep(0.01)
                rows = json.loads(path.read_text(encoding="utf-8"))["messages"]
                self.assertEqual(rows[0]["message_versions"][0]["text"], "New text without a URL")
            client = SimpleNamespace(add_event_handler=lambda cb, _: handlers.append(cb), run_until_disconnected=disconnected)
            with patch.object(tg, "cmd_backfill", AsyncMock()):
                await tg.cmd_live(client, db, [channel()], dt(2022), json_dir=tmp)


class ExportConcurrencyTests(unittest.TestCase):
    def test_two_exporters_cannot_write_snapshots_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp) / "archive.db", Path(tmp) / "news.json"
            db = tg.open_db(path)
            tg.save_observation(db, tg.to_row(1, message()), "backfill")
            db.commit()
            entered, release = threading.Event(), threading.Event()
            active = 0
            failures = []
            original = ne._export_json
            def exporting(connection, ids, target, year=None):
                nonlocal active
                active += 1
                try:
                    self.assertEqual(active, 1)
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("test did not release exporter")
                    return original(connection, ids, target, year)
                finally:
                    active -= 1
            def run():
                connection = sqlite3.connect(path)
                try:
                    ne.export_json(connection, {1}, out)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    connection.close()
            with patch.object(ne, "_export_json", side_effect=exporting):
                first = threading.Thread(target=run)
                second = threading.Thread(target=run)
                first.start()
                try:
                    self.assertTrue(entered.wait(1))
                    second.start()
                    tg.save_observation(db, tg.to_row(1, message(2)), "live_new")
                    db.commit()
                finally:
                    release.set()
                    first.join(3)
                    if second.ident is not None:
                        second.join(3)
                    db.close()
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))["messages"]), 2)

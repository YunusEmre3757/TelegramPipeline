"""Parallel article fetching: per-site turns, link discovery, the worker and the yearly JSON files."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from haber_extractor import news_extractor as extractor
from news_archive import article_fetcher as af
from news_archive import news_export as ne
from news_archive import tg_collector as tg
from tests.helpers import channel, dt, message


def save(db, mid, text, day=1, edit=None, cid=1, year=2026):
    msg = message(mid, text, date=datetime(year, 1, day, tzinfo=timezone.utc), cid=cid, edit=edit)
    tg.save_observation(db, tg.to_row(cid, msg), "live_edit" if edit else "backfill")
    db.commit()


def link(url, day=1, revisions=(), files=((1, 2026),)):
    return af.Link((url, 1, 1), dt(2026, day).isoformat(), list(revisions), set(files))


def article(url, **kwargs):
    return "Title", "Article text", None


class SpacingTests(unittest.TestCase):
    def test_waiting_for_one_site_does_not_hold_back_another(self):
        limiter, stop = extractor.DomainLimiter(), threading.Event()
        self.assertTrue(limiter.wait("slow.test", 5, stop))
        waiting = threading.Thread(target=limiter.wait, args=("slow.test", 5, stop))
        waiting.start()
        try:
            time.sleep(0.05)   # the second request to slow.test is now waiting for its turn
            started = time.monotonic()
            self.assertTrue(limiter.wait("other.test", 5, stop))
            self.assertLess(time.monotonic() - started, 1)
        finally:
            stop.set()
            waiting.join(2)
        self.assertFalse(waiting.is_alive())

    def test_simultaneous_requests_to_one_site_still_take_turns(self):
        limiter, stop, turns = extractor.DomainLimiter(), threading.Event(), []

        def request():
            if limiter.wait("site.test", 0.2, stop):
                turns.append(time.monotonic())

        threads = [threading.Thread(target=request) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        turns.sort()
        self.assertEqual(len(turns), 3)
        self.assertGreaterEqual(turns[1] - turns[0], 0.18)
        self.assertGreaterEqual(turns[2] - turns[1], 0.18)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        af.reset_stop()
        self.db = tg.open_db(":memory:")
        self.addCleanup(self.db.close)
        af.ensure_schema(self.db)

    def complete(self, url, revisions=None, result=("Title", "Article text", None)):
        with patch.object(af, "fetch_article", return_value=result):
            return af.fetch_urls(self.db, [(url, 1, 1)], delay=0, revisions=revisions)

    def test_later_looks_read_only_new_message_versions(self):
        save(self.db, 1, "Old https://a.test/old", day=1)
        mark, links = af.discover(self.db, {1})
        self.assertEqual(links, [(("https://a.test/old", 1, 1), dt(2026, 1).isoformat(), [], {(1, 2026)})])
        self.assertEqual(af.discover(self.db, {1}, mark), (mark, []))   # still unfetched, but already reported
        save(self.db, 2, "New https://a.test/new and https://t.me/skipped", day=2)
        later, links = af.discover(self.db, {1}, mark)
        self.assertGreater(later, mark)
        self.assertEqual(links, [(("https://a.test/new", 1, 2), dt(2026, 2).isoformat(), [], {(1, 2026)})])
        self.assertEqual(af.discover(self.db, {2}, mark)[1], [])        # another channel's selection
        self.assertEqual(af.pending_urls(self.db, {1}), [("https://a.test/old", 1, 1), ("https://a.test/new", 1, 2)])

    def test_link_knows_every_source_file_carrying_it(self):
        url = "https://a.test/shared"
        save(self.db, 1, f"Bu yıl {url}", cid=1)
        save(self.db, 5, f"Geçen yıl {url}", cid=2, year=2025)
        save(self.db, 6, "Başka https://a.test/other", cid=2, year=2025)
        files = {link.job[0]: link.files for link in af.discover(self.db, {1, 2})[1]}
        self.assertEqual(files, {url: {(1, 2026), (2, 2025)}, "https://a.test/other": {(2, 2025)}})

    def test_edit_of_a_fetched_link_is_due_again_with_its_revision(self):
        url = "https://a.test/story"
        save(self.db, 1, f"First {url}")
        self.complete(url)
        mark = af.discover(self.db, {1})[0]
        save(self.db, 1, f"Edited {url}", edit=dt(2026, 2))
        mark, links = af.discover(self.db, {1}, mark)
        (job, _, revisions, files), = links
        self.assertEqual((job, files), ((url, 1, 1), {(1, 2026)}))
        self.assertEqual([r[:3] for r in revisions], [(url, 1, 1)])
        # The fetch answers for exactly the revisions it was given, without searching the archive.
        with patch.object(af, "_edited_revisions", side_effect=AssertionError("archive searched")):
            self.complete(url, {url: revisions})
        self.assertEqual(af.discover(self.db, {1})[1], [])

    def test_fetch_given_no_revision_leaves_the_edit_due(self):
        url = "https://a.test/story"
        save(self.db, 1, f"Edited {url}", edit=dt(2026, 2))
        self.complete(url, {url: []})
        (job, _, revisions, _), = af.discover(self.db, {1})[1]
        self.assertEqual((job, len(revisions)), ((url, 1, 1), 1))

    def test_refusals_are_counted_apart_from_other_errors(self):
        for error, refused in (("HTTP 429", 1), ("HTTP 403", 1), ("HTTP 404", 0), ("indirme hatası: timeout", 0)):
            with self.subTest(error=error):
                stats = self.complete("https://a.test/x", result=(None, None, error))
                self.assertEqual((stats["error"], stats["refused"]), (1, refused))

    def test_failed_links_come_with_their_files_and_edits(self):
        url = "https://a.test/down"
        save(self.db, 1, f"Edited {url}", edit=dt(2026, 2))
        save(self.db, 2, "Fine https://a.test/fine")
        self.complete(url, result=(None, None, "HTTP 503"))
        self.complete("https://a.test/fine")
        (job, published, revisions, files), = af.failed(self.db, {1})
        self.assertEqual((job, published, files), ((url, 1, 1), None, {(1, 2026)}))
        self.assertEqual([r[:3] for r in revisions], [(url, 1, 1)])
        self.assertEqual(af.failed(self.db, {2}), [])
        self.assertEqual(af.failed(self.db, set()), [])
        with self.assertRaises(ValueError):
            af.failed(self.db, {1}, limit=0)
        with patch.object(af, "fetch_article", side_effect=article):
            self.assertEqual(af.retry_errors(self.db, {1}, delay=0)["ok"], 1)
        self.assertEqual(af.failed(self.db, {1}), [])


class QueueTests(unittest.TestCase):
    def test_one_fetch_per_site_and_the_newest_message_first(self):
        queue = af.LinkQueue(parallel=2)
        queue.add([link("https://a.test/1", day=1), link("https://a.test/3", day=3),
                   link("https://b.test/2", day=2), link("https://c.test/4", day=4)])
        self.assertEqual(len(queue), 4)
        first, second = queue.take()[0], queue.take()[0]
        self.assertEqual([first[0], second[0]], ["https://c.test/4", "https://a.test/3"])
        self.assertIsNone(queue.take())                 # two sites are busy: the limit
        queue.done(second)
        self.assertEqual(queue.take()[0][0], "https://b.test/2")   # newer than a.test's next message
        queue.done(first)
        self.assertEqual(queue.take()[0][0], "https://a.test/1")
        self.assertEqual(len(queue), 0)

    def test_message_arriving_later_overtakes_the_backlog_of_its_site(self):
        queue = af.LinkQueue()
        queue.add([link(f"https://a.test/{day}", day=day) for day in (1, 2, 3)])
        queue.add([link("https://a.test/live", day=9)])
        self.assertEqual(queue.take()[0][0], "https://a.test/live")

    def test_link_being_fetched_is_queued_again_only_for_an_edit(self):
        queue, url = af.LinkQueue(), "https://a.test/story"
        queue.add([link(url)])
        job, revisions = queue.take()
        self.assertEqual(revisions, [])
        queue.add([link(url, files=[(2, 2025)])])       # seen again, in another file, while its fetch runs
        self.assertEqual(len(queue), 0)
        edit = (url, 1, 1, "version", "observed")
        queue.add([link(url, revisions=[edit])])
        queue.add([link(url, revisions=[edit])])        # the same edit reported twice
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue.take())                 # not while the first fetch runs
        self.assertEqual(queue.done(job), {(1, 2026), (2, 2025)})
        self.assertEqual(queue.take(), (job, [edit]))
        self.assertEqual(queue.done(job), {(1, 2026), (2, 2025)})   # the second fetch changes the same files
        self.assertEqual(queue.files, {})

    def test_refusing_site_is_left_alone_while_others_continue(self):
        queue = af.LinkQueue()
        queue.add([link("https://a.test/1", day=3), link("https://a.test/2", day=2), link("https://b.test/1", day=1)])
        with patch.object(af.time, "monotonic", return_value=1000.0):
            refused = queue.take()[0]
            queue.done(refused, refused=True)
            self.assertEqual(queue.take()[0][0], "https://b.test/1")
            self.assertIsNone(queue.take())
            self.assertEqual(queue.paused(), {"a.test": round(af.REFUSAL_PAUSE)})
        with patch.object(af.time, "monotonic", return_value=1000.0 + af.REFUSAL_PAUSE):
            again = queue.take()[0]
            self.assertEqual(again[0], "https://a.test/2")
            queue.done(again, refused=True)             # still refusing: the pause doubles
            self.assertEqual(queue.resume["a.test"], 1000.0 + 3 * af.REFUSAL_PAUSE)
            queue.done(again)
            self.assertEqual(queue.paused(), {})


class ArchiveCase(unittest.IsolatedAsyncioTestCase):
    """A file archive with channel 1 and the given (text, day[, year]) messages."""

    def archive(self, texts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "archive.db"
        self.db = tg.open_db(self.path)
        self.addCleanup(self.db.close)
        af.ensure_schema(self.db)
        tg.upsert_channel(self.db, channel())
        for mid, (text, day, *year) in enumerate(texts, 1):
            save(self.db, mid, text, day=day, year=year[0] if year else 2026)
        return Path(tmp.name)

    def stored(self, count):
        return lambda health: self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == count


class WorkerTests(ArchiveCase):
    async def run_worker(self, fetch, finished, changed=None, **options):
        """Run the live worker until `finished(health)` holds; returns the article rows and its health record."""
        wakeup, health = asyncio.Event(), SimpleNamespace(data={})
        wakeup.set()
        with patch.object(af, "fetch_article", side_effect=fetch):
            task = asyncio.create_task(tg.article_worker(str(self.path), {1}, wakeup, changed, health, **options))
            try:
                async def poll():
                    while not finished(health):
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(poll(), 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self.db.execute("SELECT url,status FROM articles ORDER BY url").fetchall(), health.data["articles"]

    @staticmethod
    def counting():
        """A fetch that records how many fetches were in flight, in all and per site."""
        lock, flying, worst = threading.Lock(), {}, {}

        def fetch(url, **kwargs):
            with lock:
                for key in (af.domain_of(url), "all"):
                    flying[key] = flying.get(key, 0) + 1
                    worst[key] = max(worst.get(key, 0), flying[key])
            try:
                fetch.inside()
                return "Title", "Article text", None
            finally:
                with lock:
                    for key in (af.domain_of(url), "all"):
                        flying[key] -= 1

        fetch.inside, fetch.worst = lambda: time.sleep(0.05), worst
        return fetch

    async def test_sites_are_fetched_together_and_one_site_never_twice_at_once(self):
        self.archive([(f"Haber https://{site}.test/{n}", n) for n in (1, 2) for site in ("a", "b")])
        fetch, both = self.counting(), threading.Barrier(2, timeout=3)
        fetch.inside = both.wait   # passes only while two fetches are in flight together
        rows, health = await self.run_worker(fetch, self.stored(4))
        self.assertEqual([status for _, status in rows], ["ok"] * 4)
        self.assertEqual(fetch.worst, {"a.test": 1, "b.test": 1, "all": 2})
        self.assertEqual(health["status"], "stopped")

    async def test_parallel_setting_limits_the_sites_in_flight(self):
        self.archive([(f"Haber https://{site}.test/1", 1) for site in "abcdef"])
        for parallel, expected in ((1, 1), (3, 3)):
            with self.subTest(parallel=parallel):
                self.db.execute("DELETE FROM articles")
                self.db.commit()
                fetch = self.counting()
                await self.run_worker(fetch, self.stored(6), parallel=parallel)
                self.assertEqual(fetch.worst["all"], expected)

    async def test_newest_message_is_fetched_first(self):
        self.archive([("Haber https://a.test/old", 1), ("Haber https://a.test/newest", 3), ("Haber https://a.test/mid", 2)])
        order = []

        def fetch(url, **kwargs):
            order.append(url)
            return "Title", "Article text", None

        await self.run_worker(fetch, self.stored(3))
        self.assertEqual(order, ["https://a.test/newest", "https://a.test/mid", "https://a.test/old"])

    async def test_finished_fetch_names_the_source_files_of_its_link(self):
        self.archive([("Bu yıl https://a.test/now", 1), ("Geçen yıl https://a.test/then", 1, 2025)])
        touched = []
        await self.run_worker(article, self.stored(2), touched.append)
        self.assertCountEqual(touched, [{(1, 2026)}, {(1, 2025)}])

    async def test_refusal_stops_that_site_only(self):
        self.archive([("Haber https://a.test/1", 4), ("Haber https://a.test/2", 3),
                      ("Haber https://b.test/1", 2), ("Haber https://b.test/2", 1)])
        fetched = []

        def fetch(url, **kwargs):
            fetched.append(url)
            return (None, None, "HTTP 429") if "a.test" in url else ("Title", "Article text", None)

        rows, health = await self.run_worker(
            fetch, lambda health: self.stored(3)(health) and health.data["articles"].get("paused"))
        self.assertEqual(rows, [("https://a.test/1", "error"), ("https://b.test/1", "ok"), ("https://b.test/2", "ok")])
        self.assertNotIn("https://a.test/2", fetched)
        self.assertEqual(list(health["paused"]), ["a.test"])

    async def test_given_links_are_fetched_and_a_refusing_site_is_not_waited_for(self):
        self.archive([("Haber https://a.test/1", 4), ("Haber https://a.test/2", 3),
                      ("Haber https://b.test/1", 2), ("Haber https://b.test/2", 1)])
        links = af.discover(self.db, {1})[1]

        def fetch(url, **kwargs):
            return (None, None, "HTTP 429") if "a.test" in url else ("Title", "Article text", None)

        with patch.object(af, "fetch_article", side_effect=fetch):
            stats = await asyncio.wait_for(tg.article_worker(str(self.path), {1}, links=links), 5)
        self.assertEqual({k: stats[k] for k in ("ok", "error", "refused", "total", "left", "paused")},
                         {"ok": 2, "error": 1, "refused": 1, "total": 3, "left": 1, "paused": ["a.test"]})
        self.assertEqual(af.pending_urls(self.db, {1}), [("https://a.test/2", 1, 2)])   # untouched, for a later run


class SourceFileTests(ArchiveCase):
    async def test_changed_year_is_written_again_only_after_the_pause_and_other_years_not_at_all(self):
        folder = self.archive([("Geçen yıl", 1, 2025), ("Bu yıl", 1)])
        written, export = [], ne.export_sources

        def recording(db, ids, directory, only=None):
            written.append(only)
            return export(db, ids, directory, only)

        async def until(count):
            while len(written) < count:
                await asyncio.sleep(0.01)

        with patch.object(ne, "export_sources", side_effect=recording), patch.object(tg, "EXPORT_PAUSE", 0.4):
            files = tg.SourceFiles(self.db, {1}, folder / "sources")
            task = asyncio.create_task(files.run())
            try:
                files.touch()
                await asyncio.wait_for(until(2), 2)
                self.assertCountEqual(written, [{(1, 2025)}, {(1, 2026)}])   # one file per write
                save(self.db, 3, "Bu yıl, yeni", day=2)
                files.touch_new_messages()
                await asyncio.sleep(0.15)
                self.assertEqual(len(written), 2)                            # written too recently
                await asyncio.wait_for(until(3), 2)
                self.assertEqual(written[2], {(1, 2026)})
                files.touch_new_messages()                                   # nothing new since
                await asyncio.sleep(0.6)
                self.assertEqual(len(written), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        rows = json.loads((folder / "sources/channel_1_2026.json").read_text(encoding="utf-8"))["messages"]
        self.assertEqual([r["message_versions"][0]["text"] for r in rows], ["Bu yıl", "Bu yıl, yeni"])

    async def test_flush_writes_what_is_still_waiting(self):
        folder = self.archive([("Geçen yıl", 1, 2025), ("Bu yıl", 1)])
        files = tg.SourceFiles(self.db, {1}, folder / "sources")
        files.touch({(1, 2025), (9, 2026)})   # channel 9 is not this collector's
        files.flush()
        self.assertEqual([p.name for p in (folder / "sources").glob("*.json")], ["channel_1_2025.json"])
        self.assertEqual(files.waiting, set())


class FetchCommandTests(unittest.TestCase):
    def test_pending_then_failed_links_go_through_the_parallel_worker_and_reach_the_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = tg.open_db(Path(tmp) / "archive.db")
            try:
                tg.upsert_channel(db, channel())
                save(db, 1, "Haber https://a.test/1")
                save(db, 2, "Haber https://b.test/1")
                save(db, 3, "Haber https://b.test/2", year=2025)
                down = lambda url, **kwargs: (None, None, "HTTP 500") if "a.test" in url else article(url)
                with patch.object(af, "fetch_article", side_effect=down):
                    stats = tg.cmd_fetch_articles(db, {1}, Path(tmp) / "sources", limit=2)
                    self.assertEqual((stats["selected"], stats["left"]), (2, 0))
                    stats = tg.cmd_fetch_articles(db, {1}, Path(tmp) / "sources")
                self.assertEqual((stats["selected"], stats["ok"]), (1, 1))        # the one the limit left out
                self.assertEqual(db.execute("SELECT status,COUNT(*) FROM articles GROUP BY status").fetchall(),
                                 [("error", 1), ("ok", 2)])
                with patch.object(af, "fetch_article", side_effect=article):
                    stats = tg.cmd_fetch_articles(db, {1}, Path(tmp) / "sources", retry=True, parallel=1)
                self.assertEqual((stats["selected"], stats["ok"], stats["left"]), (1, 1, 0))
                self.assertEqual(tg.cmd_fetch_articles(db, {1}, Path(tmp) / "sources")["selected"], 0)
                linked = {name: [len(r["articles"]) for r in json.loads(
                    (Path(tmp) / "sources" / name).read_text(encoding="utf-8"))["messages"]]
                          for name in ("channel_1_2025.json", "channel_1_2026.json")}
                self.assertEqual(linked, {"channel_1_2025.json": [1], "channel_1_2026.json": [1, 1]})
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

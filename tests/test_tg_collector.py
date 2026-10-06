import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from news_archive import tg_collector as tg
from telethon.tl.types import Channel, ChatPhotoEmpty, PeerChannel
from tests.helpers import FakeClient, channel, dt, message


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.db = tg.open_db(":memory:")
        self.addCleanup(self.db.close)
        tg.upsert_channel(self.db, channel())

    def save(self, msg, observed="2026-01-02T00:00:00+00:00", source="backfill", cid=1):
        with patch.object(tg, "now_iso", return_value=observed):
            row = tg.to_row(cid, msg)
        tg.save_observation(self.db, row, source)
        return row

    def test_since_days_setting_is_read_and_counted_back_from_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.txt"
            path.write_text("since-days=3  # son üç gün\n@ekonomi\n", encoding="utf-8")
            with patch.object(tg, "CHANNELS_FILE", str(path)):
                self.assertEqual(tg.read_channel_list(), (["ekonomi"], {"since-days": "3"}))
        now = dt(2026, 10)
        self.assertEqual(tg.default_since({"since-days": "3"}, now), "2026-01-07")
        self.assertEqual(tg.default_since({"since": "2026-08-01"}, now), "2026-08-01")
        for bad in ({"since-days": "0"}, {"since-days": "3.5"}, {"since-days": "3", "since": "2026-08-01"}):
            with self.subTest(settings=bad), self.assertRaises(ValueError):
                tg.default_since(bad, now)

    def test_parallel_sites_setting_is_read_from_the_channel_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.txt"
            path.write_text("parallel-sites=6  # aynı anda altı site\n@ekonomi\n", encoding="utf-8")
            with patch.object(tg, "CHANNELS_FILE", str(path)):
                refs, settings = tg.read_channel_list()
        self.assertEqual((refs, tg.parallel_sites(settings)), (["ekonomi"], 6))
        self.assertEqual(tg.parallel_sites({}), 4)   # not written: the default
        for bad in ("0", "17", "2.5", "dört"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                tg.parallel_sites({"parallel-sites": bad})

    def test_invalid_and_duplicate_channels_do_not_break_resolution(self):
        async def get_entity(ref):
            if ref == "missing":
                raise ValueError("not found")
            if ref == "person":
                return SimpleNamespace(id=99)
            if ref == "group":
                return Channel(id=2, title="group", photo=ChatPhotoEmpty(), date=dt(), megagroup=True)
            return channel()
        client = SimpleNamespace(get_entity=get_entity)
        with patch.object(tg, "read_channel_list", return_value=["person", "group", "missing", "good", "alias"]):
            found = asyncio.run(tg.resolve_channels(client, self.db))
        self.assertEqual([e.id for e in found], [1])

    def test_empty_and_old_only_channels_resume(self):
        for old_messages in ([], [message(date=dt(2020))]):
            with self.subTest(old=bool(old_messages)):
                self.db.execute("DELETE FROM messages")
                self.db.execute("DELETE FROM state")
                self.db.execute("DELETE FROM message_versions")
                client = FakeClient(old_messages)
                asyncio.run(tg.backfill_channel(client, self.db, channel(), dt(2022)))
                client.messages += [message(i) for i in range(2, 12)]
                asyncio.run(tg.backfill_channel(client, self.db, channel(), dt(2022)))
                self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 10)

    def test_interruption_resumes_without_gaps_and_earlier_since_works(self):
        client = FakeClient([message(i, date=dt(2026, i)) for i in range(1, 7)])
        client.fail_after = 3
        with patch.object(tg, "BATCH", 2):
            with self.assertRaises(ConnectionError):
                asyncio.run(tg.backfill_channel(client, self.db, channel(), dt(2026, 3)))
        client.fail_after = None
        asyncio.run(tg.backfill_channel(client, self.db, channel(), dt(2026, 3)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 4)
        asyncio.run(tg.backfill_channel(client, self.db, channel(), dt(2026, 1)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 6)

    def test_buffered_original_written_after_live_edit_is_preserved(self):
        with patch.object(tg, "now_iso", return_value="2026-01-01T00:00:00+00:00"):
            buffered = tg.to_row(1, message(text="Original"))
        edited = self.save(message(text="Updated", edit=dt(2026, 2)), source="live_edit")
        tg.save_observation(self.db, buffered, "backfill")
        tg.save_observation(self.db, edited, "backfill")
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "Original")
        self.assertEqual(self.db.execute("SELECT count(*) FROM message_versions").fetchone()[0], 2)
        first = self.db.execute(
            "SELECT version_id,source FROM message_versions ORDER BY observed_at LIMIT 1"
        ).fetchone()
        self.assertEqual(first, (tg.version_id(buffered), "backfill"))

    def test_later_stale_backfill_cannot_replace_first_observation(self):
        first = self.save(message(text="Already edited", edit=dt()), source="live_edit")
        self.save(message(text="Older snapshot"), observed="2026-01-03T00:00:00+00:00")
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "Already edited")
        earliest = self.db.execute(
            "SELECT version_id,source FROM message_versions ORDER BY observed_at LIMIT 1"
        ).fetchone()
        self.assertEqual(earliest, (tg.version_id(first), "live_edit"))

    def test_same_version_reobserved_does_not_duplicate_or_change_first_capture(self):
        self.save(message(), source="live_new")
        msg = message()
        msg.views = 100
        self.save(msg, observed="2026-02-01T00:00:00+00:00")
        self.assertEqual(self.db.execute("SELECT count(*) FROM message_versions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT source FROM message_versions").fetchone()[0], "live_new")

    def test_live_handlers_preserve_new_message_and_capture_cleared_text(self):
        handlers = []

        async def run_until_disconnected():
            await handlers[0](tg.events.NewMessage.Event(message(text="Original news")))
            await handlers[1](tg.events.MessageEdited.Event(message(text="", edit=dt(2026, 2))))

        client = SimpleNamespace(
            add_event_handler=lambda handler, event: handlers.append(handler),
            run_until_disconnected=run_until_disconnected,
        )
        with patch.object(tg, "cmd_backfill", new=AsyncMock()):
            asyncio.run(tg.cmd_live(client, self.db, [channel()], dt(2022)))
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], "Original news")
        self.assertEqual(set(self.db.execute("SELECT source,text FROM message_versions")),
                         {("live_new", "Original news"), ("live_edit", "")})

    def test_empty_channel_list_does_not_register_unfiltered_live_handlers(self):
        # No methods: any attempt to register a handler or connect must fail.
        asyncio.run(tg.cmd_live(SimpleNamespace(), self.db, [], dt(2022)))

    def test_legacy_database_migrates_once_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            db = sqlite3.connect(path)
            db.executescript(tg.SCHEMA.split("CREATE TABLE IF NOT EXISTS message_versions")[0])
            row = tg.to_row(1, message(text="Legacy text", edit=dt()))
            db.execute("INSERT INTO messages VALUES (" + ",".join("?" * len(row)) + ")", row)
            db.commit()
            db.close()
            for _ in range(2):
                db = tg.open_db(path)
                try:
                    self.assertEqual(db.execute("SELECT text FROM messages").fetchone()[0], "Legacy text")
                    self.assertEqual(db.execute("SELECT source FROM message_versions").fetchall(), [("legacy",)])
                finally:
                    db.close()

    def test_short_news_and_opposite_meanings_are_preserved(self):
        for mid, text in enumerate([
            "TCMB politika faizini sabit tuttu", "TCMB politika faizini sabit tutmadı",
            "Spotify çöktü.", "Dolar 40 TL", "Hisse +%10", "Hisse -%10",
        ], 1):
            self.save(message(mid, text))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 6)


    def test_forward_time_does_not_change_first_channel_time(self):
        msg = message()
        msg.fwd_from = SimpleNamespace(from_id=PeerChannel(9), channel_post=10, date=dt(2020))
        self.save(msg)
        row = self.db.execute("SELECT date_utc,fwd_date_utc FROM messages").fetchone()
        self.assertEqual(row, (tg.iso(dt()), tg.iso(dt(2020))))

    def test_message_version_key_is_independent_of_other_channels(self):
        row = self.save(message(date=dt() + timedelta(hours=1)))
        tg.upsert_channel(self.db, channel(2))
        self.save(message(cid=2), cid=2)
        self.assertIn((1, 1, tg.version_id(row)), self.db.execute(
            "SELECT channel_id,message_id,version_id FROM message_versions"
        ).fetchall())


if __name__ == "__main__":
    unittest.main()

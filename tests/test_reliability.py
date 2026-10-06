import argparse
import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from news_archive import collector_runtime as runtime
from news_archive import tg_collector as tg
from tests.helpers import FakeClient, channel, dt, message
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageService, MessageActionChannelCreate, MessageMediaPhoto, PhotoEmpty, MessageEntityTextUrl


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = tg.open_db(":memory:")
        self.addCleanup(self.db.close)
        tg.upsert_channel(self.db, channel())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.health = runtime.Health(Path(self.tmp.name) / "health.json", "live")

    async def test_backfill_keeps_captions_and_links_but_skips_textless_and_service(self):
        photo = message(1, "")
        photo.media = MessageMediaPhoto(photo=PhotoEmpty(id=77))
        service = MessageService(id=2, peer_id=photo.peer_id, date=dt(),
                                 action=MessageActionChannelCreate(title="Created"))
        caption = message(3, "Haber https://example.com")
        caption.media = photo.media
        caption.entities = [MessageEntityTextUrl(offset=0, length=5, url="https://source.example")]
        await tg.backfill_channel(FakeClient([photo, service, caption]), self.db, channel(), dt(2022))
        rows = self.db.execute("SELECT message_id,text,urls FROM messages ORDER BY message_id").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (3, caption.message))
        self.assertEqual(set(json.loads(rows[0][2])), {"https://example.com", "https://source.example"})
        snapshots = [json.loads(row[0]) for row in self.db.execute("SELECT snapshot_json FROM message_versions")]
        self.assertTrue(all("telegram_media_or_action" not in row for row in snapshots))
        self.assertNotIn("media_type", {r[1] for r in self.db.execute("PRAGMA table_info(messages)")})
        self.assertEqual(self.db.execute("SELECT oldest_id,newest_id FROM state").fetchone(), (1, 3))

    async def test_recent_scan_catches_edit_but_does_not_advance_history_state(self):
        now = datetime.now(timezone.utc)
        original = message(date=now - timedelta(hours=2))
        tg.save_observation(self.db, tg.to_row(1, original), "live_new")
        tg.save_state(self.db, 1, 1, 1, 1, dt(2022).isoformat())
        state = self.db.execute("SELECT * FROM state").fetchall()
        edited = message(text="Changed", date=original.date, edit=now)
        old = message(0, "Too old", date=now - timedelta(hours=49))
        await tg.refresh_recent(FakeClient([old, edited]), self.db, channel(), dt(2022))
        self.assertEqual(self.db.execute("SELECT * FROM state").fetchall(), state)
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone()[0], original.message)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0], 2)
        await tg.refresh_recent(FakeClient([edited]), self.db, channel(), dt(2022))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM message_versions").fetchone()[0], 2)

    async def test_recent_scan_finds_cleared_caption_and_respects_since(self):
        now = datetime.now(timezone.utc)
        m = message(date=now - timedelta(hours=3))
        tg.save_observation(self.db, tg.to_row(1, m), "live_new")
        m.message = ""
        m.edit_date = now
        client = FakeClient([m])
        self.assertEqual(await tg.refresh_recent(client, self.db, channel(), now - timedelta(hours=1)), 0)
        self.assertEqual(await tg.refresh_recent(client, self.db, channel(), dt(2022)), 1)
        self.assertIn(("",), self.db.execute("SELECT text FROM message_versions").fetchall())

    async def test_reconciliation_is_idle_until_signalled_and_recovers_after_failure(self):
        wakeup = asyncio.Event()
        complete = asyncio.Event()
        calls = 0
        async def scan(*args, **kwargs):
            nonlocal calls
            calls += 1
            complete.set()
            if calls == 1:
                raise RuntimeError("temporary")
        with patch.object(tg, "cmd_backfill", scan):
            task = asyncio.create_task(tg.reconciliation_loop(
                None, self.db, [channel()], dt(2022), wakeup, health=self.health))
            try:
                await asyncio.sleep(0.02)
                self.assertEqual(calls, 0)
                wakeup.set()
                await asyncio.wait_for(complete.wait(), 1)
                await asyncio.sleep(0.02)
                self.assertEqual(calls, 1)
                self.assertEqual(self.health.data["channels"]["1"]["status"], "error")
                complete.clear()
                wakeup.set()
                await asyncio.wait_for(complete.wait(), 1)
                self.assertEqual(calls, 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_reconnect_recovers_missing_message_and_recent_edit_and_restores_callback(self):
        now = datetime.now(timezone.utc)
        original = message(1, "original", date=now - timedelta(hours=2))
        client = FakeClient([original])
        old_callback = AsyncMock()
        client._sender = SimpleNamespace(_auto_reconnect_callback=old_callback)
        client.add_event_handler = lambda *args: None
        async def disconnected():
            while self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] != 1:
                await asyncio.sleep(0)
            client.messages[0] = message(1, "edited", date=original.date, edit=now)
            client.messages.append(message(2, date=now))
            await client._sender._auto_reconnect_callback()
            for _ in range(100):
                if self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2:
                    return
                await asyncio.sleep(0.01)
            self.fail("Reconnect did not recover messages")
        client.run_until_disconnected = disconnected
        await asyncio.wait_for(tg.cmd_live(client, self.db, [channel()], dt(2022), health=self.health), 2)
        self.assertEqual(self.db.execute("SELECT message_id FROM messages ORDER BY message_id").fetchall(), [(1,), (2,)])
        self.assertIn(("edited",), self.db.execute("SELECT text FROM message_versions").fetchall())
        old_callback.assert_awaited_once()
        self.assertIs(client._sender._auto_reconnect_callback, old_callback)
        self.assertEqual(self.health.data["scan_mode"], "on_reconnect")

    async def test_reconnect_during_scan_is_queued_and_bursts_are_coalesced(self):
        wakeup, complete = asyncio.Event(), asyncio.Event()
        calls = 0
        async def scan(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                wakeup.set()
                wakeup.set()
            else:
                complete.set()
        wakeup.set()
        with patch.object(tg, "cmd_backfill", scan):
            task = asyncio.create_task(tg.reconciliation_loop(None, self.db, [channel()], dt(2022), wakeup))
            try:
                await asyncio.wait_for(complete.wait(), 1)
                await asyncio.sleep(0.02)
                self.assertEqual(calls, 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_live_writes_readable_json_after_initial_scan(self):
        directory = Path(self.tmp.name) / "sources"
        path = directory / "channel_1_2026.json"
        tg.save_observation(self.db, tg.to_row(1, message()), "backfill")
        async def disconnected():
            while not path.exists():
                await asyncio.sleep(0.01)
        client = SimpleNamespace(add_event_handler=lambda *args: None, run_until_disconnected=disconnected)
        with patch.object(tg, "cmd_backfill", AsyncMock()):
            await asyncio.wait_for(tg.cmd_live(client, self.db, [channel()], dt(2022), json_dir=directory), 1)
        rows = json.loads(path.read_text(encoding="utf-8"))["messages"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("labels", rows[0])

    async def test_flood_wait_is_visible_and_retried(self):
        error = FloodWaitError(request=None, capture=1)
        async def sleeping(seconds):
            self.assertEqual(seconds, 6)
            self.assertEqual(self.health.data["channels"]["1"]["status"], "waiting")
            self.assertIsNotNone(self.health.data["channels"]["1"]["waiting_until"])
        with patch.object(tg, "refresh_recent", AsyncMock(side_effect=[error, 0])), \
                patch.object(tg, "backfill_channel", AsyncMock()), \
                patch.object(tg.asyncio, "sleep", sleeping):
            self.assertEqual(await tg.cmd_backfill(None, self.db, [channel()], dt(2022), health=self.health), [])
        self.assertEqual(self.health.data["channels"]["1"]["status"], "ok")

    async def test_failed_channel_does_not_stop_other_channels(self):
        with patch.object(tg, "refresh_recent", AsyncMock(side_effect=[ConnectionError("offline"), 0])), \
                patch.object(tg, "backfill_channel", AsyncMock()):
            failures = await tg.cmd_backfill(None, self.db, [channel(), channel(2)], dt(2022), health=self.health)
        self.assertEqual(failures, [1])
        self.assertEqual(self.health.data["channels"]["1"]["status"], "error")
        self.assertEqual(self.health.data["channels"]["2"]["status"], "ok")

    async def test_live_deletion_preserves_message_and_records_event(self):
        tg.save_observation(self.db, tg.to_row(1, message()), "live_new")
        handlers = []
        async def disconnected():
            await handlers[2](SimpleNamespace(chat_id=-1000000000001, deleted_ids=[1, 2]))
        client = SimpleNamespace(add_event_handler=lambda cb, _: handlers.append(cb), run_until_disconnected=disconnected)
        with patch.object(tg, "cmd_backfill", AsyncMock()):
            await tg.cmd_live(client, self.db, [channel()], dt(2022))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM message_deletions").fetchone()[0], 2)
        self.assertIsNotNone(self.db.execute(
            "SELECT observed_at FROM message_deletions WHERE channel_id=1 AND message_id=1"
        ).fetchone()[0])

    async def test_live_skips_textless_photo_but_keeps_later_caption(self):
        handlers = []
        m = message(text="")
        m.media = MessageMediaPhoto(photo=PhotoEmpty(id=33))
        async def disconnected():
            await handlers[0](tg.events.NewMessage.Event(m))
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            m.message = "Fotoğraf altındaki haber"
            m.edit_date = datetime.now(timezone.utc)
            await handlers[1](tg.events.MessageEdited.Event(m))
        client = SimpleNamespace(add_event_handler=lambda cb, _: handlers.append(cb), run_until_disconnected=disconnected)
        with patch.object(tg, "cmd_backfill", AsyncMock()):
            await tg.cmd_live(client, self.db, [channel()], dt(2022))
        self.assertEqual(self.db.execute("SELECT text FROM messages").fetchone(), ("Fotoğraf altındaki haber",))

    async def test_live_task_cancellation_leaves_no_background_scanner(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def scan(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        client = SimpleNamespace(add_event_handler=lambda *args: None,
                                 run_until_disconnected=lambda: asyncio.Event().wait())
        with patch.object(tg, "cmd_backfill", AsyncMock()), patch.object(tg, "reconciliation_loop", scan):
            task = asyncio.create_task(tg.cmd_live(client, self.db, [channel()], dt(2022)))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())

    async def test_live_storage_error_propagates_to_supervisor(self):
        handlers = []
        async def disconnected():
            await handlers[0](tg.events.NewMessage.Event(message()))
            await asyncio.Event().wait()
        client = SimpleNamespace(add_event_handler=lambda cb, _: handlers.append(cb), run_until_disconnected=disconnected)
        with patch.object(tg, "cmd_backfill", AsyncMock()), \
                patch.object(tg, "save_observation", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                await asyncio.wait_for(tg.cmd_live(client, self.db, [channel()], dt(2022), health=self.health), 1)
        self.assertEqual(self.health.data["last_live_error"], "disk full")

    async def test_username_is_pinned_to_id_on_later_resolution(self):
        client = SimpleNamespace(get_entity=AsyncMock(return_value=channel()))
        with patch.object(tg, "read_channel_list", return_value=["original"]):
            await tg.resolve_channels(client, self.db)
            await tg.resolve_channels(client, self.db)
        self.assertEqual(client.get_entity.await_args.args[0].channel_id, 1)

    async def test_unauthorized_service_does_not_prompt_for_login(self):
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_connected=lambda: True,
                                 is_user_authorized=AsyncMock(return_value=False), start=AsyncMock())
        with patch.object(tg, "TelegramClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "login"):
                await tg.run_collector(SimpleNamespace(cmd="live"), self.db, self.health, "1", "test")
        client.start.assert_not_awaited()
        client.disconnect.assert_awaited()
        self.assertEqual(self.health.data["status"], "failed")

    async def test_sigterm_stops_worker_and_closes_connection(self):
        entered = asyncio.Event()
        async def waiting_dialogs():
            entered.set()
            await asyncio.Event().wait()
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_connected=lambda: True,
                                 is_user_authorized=AsyncMock(return_value=True), get_dialogs=waiting_dialogs)
        old_handler = signal.getsignal(signal.SIGTERM)
        with patch.object(tg, "TelegramClient", return_value=client):
            task = asyncio.create_task(tg.run_collector(SimpleNamespace(cmd="live"), self.db, self.health, "1", "test"))
            await asyncio.wait_for(entered.wait(), 1)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        client.disconnect.assert_awaited()
        self.assertEqual(self.health.data["status"], "stopped")
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_handler)

    async def test_heartbeat_failure_stops_worker(self):
        async def blocked_connect():
            await asyncio.Event().wait()
        client = SimpleNamespace(connect=blocked_connect, disconnect=AsyncMock(), is_connected=lambda: False)
        with patch.object(tg, "TelegramClient", return_value=client), \
                patch.object(self.health, "loop", AsyncMock(side_effect=OSError("health disk full"))):
            with self.assertRaisesRegex(OSError, "health disk full"):
                await asyncio.wait_for(tg.run_collector(SimpleNamespace(cmd="live"), self.db, self.health, "1", "test"), 1)
        client.disconnect.assert_awaited()


class RuntimeTests(unittest.TestCase):
    def test_text_only_migration_preserves_text_version_keys_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = sqlite3.connect(path)
            old_schema = tg.SCHEMA.replace("    urls            TEXT,", "    media_type TEXT,\n    grouped_id INTEGER,\n    urls            TEXT,")
            db.executescript(old_schema)
            db.execute("PRAGMA user_version=1")
            for mid, text in [(1, "Caption"), (2, ""), (3, "")]:
                snapshot = dict(zip(tg.MESSAGE_COLUMNS, tg.to_row(1, message(mid, text))))
                snapshot.update(media_type="photo", grouped_id=42)
                db.execute("INSERT INTO messages (" + ",".join(snapshot) + ") VALUES (" + ",".join("?" * len(snapshot)) + ")", list(snapshot.values()))
                snapshot["telegram_media_or_action"] = {"photo": "old metadata"}
                db.execute("INSERT INTO message_versions VALUES (?,?,?,?,?,?,?,?)",
                           (1, mid, f"old-{mid}", snapshot["collected_at"], "live_new", None, text, json.dumps(snapshot)))
                if mid == 3:
                    snapshot["text"] = "Caption added later"
                    snapshot["collected_at"] = "2099-01-01T00:00:00+00:00"
                    db.execute("INSERT INTO message_versions VALUES (?,?,?,?,?,?,?,?)",
                               (1, mid, "later-text", snapshot["collected_at"], "live_edit", None, snapshot["text"], json.dumps(snapshot)))
            tg.save_state(db, 1, 3, 1, 1, dt(2022).isoformat())
            db.commit()
            db.close()
            for _ in range(2):
                db = tg.open_db(path)
                try:
                    self.assertNotIn("media_type", {r[1] for r in db.execute("PRAGMA table_info(messages)")})
                    self.assertNotIn("grouped_id", {r[1] for r in db.execute("PRAGMA table_info(messages)")})
                    self.assertEqual(db.execute("SELECT message_id,text FROM messages ORDER BY message_id").fetchall(), [(1, "Caption"), (3, "Caption added later")])
                    self.assertEqual(db.execute("SELECT newest_id,oldest_id FROM state").fetchone(), (3, 1))
                    for payload, in db.execute("SELECT snapshot_json FROM message_versions"):
                        self.assertNotIn("media_type", json.loads(payload))
                        self.assertNotIn("telegram_media_or_action", json.loads(payload))
                    tg.save_observation(db, tg.to_row(1, message(1, "Caption")), "recent_scan")
                    self.assertEqual(db.execute("SELECT version_id FROM message_versions WHERE message_id=1").fetchall(), [("old-1",)])
                    db.commit()
                finally:
                    db.close()

    def test_cli_rejects_shared_session_even_with_different_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = str(Path(tmp) / "shared.session")
            env = dict(os.environ, TG_API_ID="1", TG_API_HASH="placeholder",
                       TG_SESSION=session, TG_DB=str(Path(tmp) / "other.db"))
            with runtime.process_lock(session):
                result = subprocess.run([sys.executable, "-B", "-m", "news_archive.tg_collector", "live"],
                                        env=env, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(Path(session).exists())  # Failed before TelegramClient creation.

    def test_crashed_process_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            resource = str(Path(tmp) / "archive.db")
            code = ("import sys\nfrom news_archive.collector_runtime import process_lock\n"
                    "with process_lock(sys.argv[1]):\n print('locked', flush=True)\n sys.stdin.read()")
            proc = subprocess.Popen([sys.executable, "-B", "-c", code, resource],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertEqual(proc.stdout.readline().strip(), b"locked")
                proc.terminate()
                proc.wait(timeout=5)
                # Windows may finish asynchronous handle cleanup just after wait().
                deadline = time.monotonic() + 2
                while True:
                    try:
                        with runtime.process_lock(resource):
                            pass
                        break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def test_process_lock_excludes_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            resource = str(Path(tmp) / "collector.session")
            code = "from news_archive.collector_runtime import process_lock; import sys\nwith process_lock(sys.argv[1]): pass"
            with runtime.process_lock(resource):
                proc = subprocess.run([sys.executable, "-B", "-c", code, resource], capture_output=True)
                self.assertNotEqual(proc.returncode, 0)
            proc = subprocess.run([sys.executable, "-B", "-c", code, resource], capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_health_distinguishes_quiet_channel_stale_heartbeat_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = runtime.Health(Path(tmp) / "h.json", "live")
            h.data.update(status="running", connected=True)
            h.channel(1, status="ok", last_success_at=datetime.now(timezone.utc).isoformat(), messages_24h=0)
            h.publish()
            self.assertTrue(runtime.read_health(h.path)[1])
            h.error(1, RuntimeError("failed"))
            h.publish()
            self.assertFalse(runtime.read_health(h.path)[1])
            h.channel(1, status="ok")
            h.publish()
            future = datetime.now(timezone.utc) + timedelta(minutes=3)
            with patch.object(runtime, "utc_now", return_value=future):
                self.assertFalse(runtime.read_health(h.path)[1])

    def test_idle_reconnect_mode_remains_healthy_but_stuck_scan_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = runtime.Health(Path(tmp) / "h.json", "live")
            old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            h.data.update(status="running", connected=True, scan_mode="on_reconnect")
            h.channel(1, status="ok", last_success_at=old, last_progress_at=old)
            h.publish()
            self.assertTrue(runtime.read_health(h.path)[1])
            h.channel(1, status="scanning", last_scan_started_at=old)
            h.publish()
            self.assertFalse(runtime.read_health(h.path)[1])
            h.channel(1, last_scan_started_at=datetime.now(timezone.utc).isoformat())
            h.publish()
            self.assertTrue(runtime.read_health(h.path)[1])

    def test_health_counts_only_archived_texts(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        tg.save_observation(db, tg.to_row(1, message(text="", date=datetime.now(timezone.utc))), "live_new")
        tg.save_observation(db, tg.to_row(1, message(2, text="Haber", date=datetime.now(timezone.utc))), "live_new")
        with tempfile.TemporaryDirectory() as tmp:
            h = runtime.Health(Path(tmp) / "h.json", "live")
            h.channel(1, status="ok")
            h.publish(db)
            self.assertEqual(h.data["channels"]["1"]["messages_24h"], 1)
            self.assertNotIn("textless_24h", h.data["channels"]["1"])

    def test_invalid_intervals_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                tg.positive_float(value)


if __name__ == "__main__":
    unittest.main()

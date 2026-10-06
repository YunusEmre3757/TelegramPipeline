import json
import tempfile
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from news_archive import article_fetcher as af
from news_archive import tg_collector as tg
from news_archive.news_export import export_json, export_sources, source_files
from tests.helpers import message, channel, dt


class NewsExportTests(unittest.TestCase):
    def test_source_files_are_isolated_and_keep_names_on_username_change(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        for cid in (1, 2, 3):
            tg.upsert_channel(db, channel(cid))
        for cid in (1, 2):
            tg.save_observation(db, tg.to_row(cid, message(cid, f"Source {cid}")), "backfill")
        with tempfile.TemporaryDirectory() as tmp:
            counts = export_sources(db, {1, 2, 3}, tmp)
            self.assertEqual(counts, {"channel_1_2026.json": 1, "channel_2_2026.json": 1})   # channel 3 has no texts
            for cid in (1, 2):
                rows = json.loads((Path(tmp) / f"channel_{cid}_2026.json").read_text(encoding="utf-8"))["messages"]
                self.assertEqual({r["source"]["channel_id"] for r in rows}, {cid})
            db.execute("UPDATE channels SET username='renamed' WHERE channel_id=1")
            export_sources(db, {1}, tmp)
            self.assertEqual(len(list(Path(tmp).glob('*.json'))), 2)

    def test_each_publication_year_has_its_own_file_with_its_own_articles(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        af.ensure_schema(db)
        tg.upsert_channel(db, channel())
        last_minute = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
        tg.save_observation(db, tg.to_row(1, message(1, "Eski https://a.test/old", date=last_minute)), "backfill")
        tg.save_observation(db, tg.to_row(1, message(2, "Yeni https://a.test/new", date=dt(2026))), "backfill")
        with patch.object(af, "fetch_article", side_effect=lambda url, **kw: ("Title", "Text of " + url, None)):
            af.fetch_all(db, {1}, delay=0)
        self.assertEqual(source_files(db, {1, 2}), {(1, 2025), (1, 2026)})
        with tempfile.TemporaryDirectory() as tmp:
            single = Path(tmp) / "channel_1.json"   # what the export wrote before the yearly split
            single.write_text("{}", encoding="utf-8")
            self.assertEqual(export_sources(db, {1}, tmp), {"channel_1_2025.json": 1, "channel_1_2026.json": 1})
            self.assertFalse(single.exists())
            for year, name in ((2025, "old"), (2026, "new")):
                record, = json.loads((Path(tmp) / f"channel_1_{year}.json").read_text(encoding="utf-8"))["messages"]
                self.assertEqual(record["published_at"][:4], str(year))
                self.assertEqual({a["text"] for a in record["articles"].values()}, {f"Text of https://a.test/{name}"})

    def test_only_the_asked_files_are_written(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        tg.upsert_channel(db, channel())
        tg.save_observation(db, tg.to_row(1, message(1, "Eski", date=dt(2025))), "backfill")
        tg.save_observation(db, tg.to_row(1, message(2, "Yeni", date=dt(2026))), "backfill")
        with tempfile.TemporaryDirectory() as tmp:
            export_sources(db, {1}, tmp)
            tg.save_observation(db, tg.to_row(1, message(3, "Daha eski", date=dt(2025, 2))), "backfill")
            tg.save_observation(db, tg.to_row(1, message(4, "Daha yeni", date=dt(2026, 2))), "backfill")
            self.assertEqual(export_sources(db, {1}, tmp, only={(1, 2026), (2, 2026)}), {"channel_1_2026.json": 2})
            count = lambda year: len(json.loads(
                (Path(tmp) / f"channel_1_{year}.json").read_text(encoding="utf-8"))["messages"])
            self.assertEqual((count(2025), count(2026)), (1, 2))   # 2025 was not asked for

    def test_raw_classification_inputs_and_stable_message_version_keys(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        tg.upsert_channel(db, channel())
        for mid, text in enumerate(["Haber metni", "Haber metni", "#reklam Tanıtım", "Dolar: 40 TL\nEuro: 45 TL"], 1):
            tg.save_observation(db, tg.to_row(1, message(mid, text)), "backfill")
        tg.save_observation(db, tg.to_row(2, message(9, "Inactive channel")), "backfill")
        tg.save_observation(db, tg.to_row(1, message(1, "Sonradan değişti", edit=dt())), "live_edit")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            self.assertEqual(export_json(db, {1}, path), 4)
            first = path.read_bytes()
            rows = json.loads(first.decode("utf-8"))["messages"]
            self.assertEqual(len({r["record_id"] for r in rows}), 4)
            first_version = next(v for v in rows[0]["message_versions"]
                                 if v["version_id"] == rows[0]["first_version_id"])
            self.assertEqual(first_version["text"], "Haber metni")
            self.assertTrue(all("labels" not in r for r in rows))
            self.assertTrue(all(r["first_version_id"] for r in rows))
            export_json(db, {1}, path)
            self.assertEqual(path.read_bytes(), first)

    def test_missing_version_does_not_replace_previous_export(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        db.execute(tg.INSERT_MSG, tg.to_row(1, message()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            path.write_text("previous", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Sürüm"):
                export_json(db, {1}, path)
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_cannot_overwrite_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.db"
            db = tg.open_db(path)
            try:
                with self.assertRaises(ValueError):
                    export_json(db, set(), path)
            finally:
                db.close()

    def test_article_query_errors_do_not_silently_drop_linked_texts(self):
        db = tg.open_db(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE articles(url TEXT)")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news.json"
            path.write_text("previous", encoding="utf-8")
            with self.assertRaises(sqlite3.OperationalError):
                export_json(db, set(), path)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous")


if __name__ == "__main__":
    unittest.main()

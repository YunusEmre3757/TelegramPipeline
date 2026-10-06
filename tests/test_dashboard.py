import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

from news_archive import dashboard
from news_archive import tg_collector as tg
from news_archive import article_fetcher as af
from unittest.mock import patch
from tests.helpers import message, channel, dt


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"
        db = tg.open_db(self.path)
        tg.upsert_channel(db, channel(1))
        tg.upsert_channel(db, channel(2))
        tg.save_observation(db, tg.to_row(1, message(1, "İHRACAT arttı", date=dt(2026, 1))), "backfill")
        tg.save_observation(db, tg.to_row(1, message(2, "Yeni haber", date=dt(2026, 2))), "live_new")
        tg.save_observation(db, tg.to_row(2, message(3, "Diğer kaynak", date=dt(2026, 3))), "backfill")
        db.commit()
        db.close()
        self.db = dashboard.connect(self.path)
        self.addCleanup(self.db.close)

    def test_filters_pagination_and_turkish_search(self):
        first = dashboard.feed(self.db, {"limit": "1"})
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(first["items"][0]["message_id"], 3)
        self.assertEqual(dashboard.feed(self.db, {"channel": "1"})["total"], 2)
        self.assertEqual(dashboard.feed(self.db, {"q": "ihracat"})["total"], 1)
        self.assertEqual(dashboard.feed(self.db, {"mode": "live"})["total"], 1)
        self.assertEqual(dashboard.feed(self.db, {"from": "2026-01-02", "to": "2026-01-02"})["total"], 1)
        self.assertEqual(dashboard.feed(self.db, {"q": "' OR 1=1 --"})["total"], 0)

    def test_read_only_connection_and_detail(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute("DELETE FROM messages")
        data = dashboard.detail(self.db, 1, 1)
        self.assertEqual(data["text"], "İHRACAT arttı")
        self.assertEqual(len(data["versions"]), 1)
        self.assertIsNone(dashboard.detail(self.db, 1, 9999))

    def test_http_surface_serves_only_ui_and_valid_api(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.handler_for(self.path, Path(self.tmp.name) / 'health.json'))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}'
            with urlopen(root + '/api/feed?limit=1') as response:
                self.assertEqual(len(json.load(response)['items']), 1)
            for endpoint, expected in [('/api/feed?channel=bad', 400), ('/api/message?channel=1&message=999', 404), ('/local-pilot/api.credentials.xml', 404)]:
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as caught:
                    urlopen(root + endpoint)
                self.assertEqual(caught.exception.code, expected)
            with urlopen(root) as response:
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_queue_and_detail_show_downloaded_article_and_edited_url(self):
        writer = tg.open_db(self.path)
        try:
            af.ensure_schema(writer)
            old, new = 'https://news.test/old', 'https://news.test/new'
            tg.save_observation(writer, tg.to_row(1, message(5, f'Initial {old}')), 'live_new')
            tg.save_observation(writer, tg.to_row(1, message(5, f'Edited {new}', edit=dt())), 'live_edit')
            writer.commit()
            with patch.object(af, 'fetch_article', return_value=('Full article', 'Extracted body 150 TL', None)):
                af.fetch_urls(writer, [(old, 1, 5)], delay=0)
        finally:
            writer.close()
        queue = dashboard.article_queue(self.db, {'channel': '1'})
        self.assertEqual(queue['pending_count'], 1)
        self.assertEqual(queue['pending'][0]['url'], new)
        self.assertEqual(queue['recent'][0]['characters'], len('Extracted body 150 TL'))
        self.assertEqual(dashboard.article_queue(self.db, {'channel': '2'})['pending_count'], 0)
        data = dashboard.detail(self.db, 1, 5)
        articles = {a['url']: a for a in data['articles']}
        self.assertEqual(articles[old]['text'], 'Extracted body 150 TL')
        self.assertEqual(articles[new]['status'], 'pending')
        self.assertEqual({tuple(v['urls']) for v in data['versions']}, {(old,), (new,)})


if __name__ == '__main__':
    unittest.main()

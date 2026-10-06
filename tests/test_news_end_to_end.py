"""Actual HTTP downloads through live events, edits, reconnect and JSON export."""
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from news_archive import tg_collector as tg
from tests.helpers import FakeClient, channel, dt, message


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_new_same_url_edit_new_url_edit_and_reconnect_with_real_http(self):
        document = {'profit': '100', 'requests': 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                document['requests'] += 1
                data = ('<html><head><meta charset="utf-8"><title>Company results</title></head>'
                        '<body><article><p>The company released its financial results. '
                        'Production increased and the board announced a new investment.</p>'
                        '<p>Türkiye net kâr: ' + document['profit'] + ' milyon TL.</p>'
                        '<table><tr><th>Period</th><th>Profit</th></tr><tr><td>2026</td><td>'
                        + document['profit'] + '</td></tr></table></article></body></html>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=ISO-8859-1')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = tg.open_db(Path(tmp) / 'archive.db')
                try:
                    tg.upsert_channel(db, channel())
                    current = datetime.now(timezone.utc)
                    client = FakeClient([message(1, 'Existing headline', date=current)])
                    handlers = []
                    original_callback = AsyncMock()
                    client._sender = SimpleNamespace(_auto_reconnect_callback=original_callback)
                    client.add_event_handler = lambda cb, _: handlers.append(cb)
                    url = f'http://127.0.0.1:{server.server_port}/story'
                    second = f'http://127.0.0.1:{server.server_port}/second'
                    output = Path(tmp) / f'sources/channel_1_{current.year}.json'

                    async def until(predicate):
                        async def poll():
                            while not predicate():
                                await asyncio.sleep(0.01)
                        await asyncio.wait_for(poll(), 8)

                    def article_contains(value):
                        row = db.execute('SELECT text FROM articles WHERE url=?', (url,)).fetchone()
                        return row and value in row[0]

                    async def disconnected():
                        await until(lambda: db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1)
                        live = message(2, f'Company results {url}', date=current)
                        client.messages.append(live)
                        await handlers[0](tg.events.NewMessage.Event(live))
                        await until(lambda: article_contains('100 milyon TL'))
                        document['profit'] = '150'
                        edited = message(2, f'Updated company results {url}', date=current, edit=current)
                        client.messages[1] = edited
                        await handlers[1](tg.events.MessageEdited.Event(edited))
                        await until(lambda: article_contains('150 milyon TL'))
                        edited_again = message(2, f'New report {second}', date=current,
                                               edit=datetime.now(timezone.utc))
                        client.messages[1] = edited_again
                        await handlers[1](tg.events.MessageEdited.Event(edited_again))
                        await until(lambda: db.execute('SELECT 1 FROM articles WHERE url=?', (second,)).fetchone())
                        client.messages.append(message(3, 'Message missed during outage', date=current))
                        await client._sender._auto_reconnect_callback()
                        await until(lambda: db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 3)
                        await until(lambda: output.exists() and len(json.loads(output.read_text(encoding='utf-8'))['messages']) == 3)

                    client.run_until_disconnected = disconnected
                    with patch.object(tg, 'EXPORT_PAUSE', 0):
                        await asyncio.wait_for(tg.cmd_live(client, db, [channel()], dt(2022), json_dir=output.parent), 20)
                    original_callback.assert_awaited_once()
                    self.assertIs(client._sender._auto_reconnect_callback, original_callback)
                    self.assertEqual(document['requests'], 3)
                    self.assertEqual(db.execute('SELECT text FROM messages WHERE message_id=2').fetchone()[0], f'Company results {url}')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM article_versions WHERE url=?', (url,)).fetchone()[0], 2)
                    records = json.loads(output.read_text(encoding='utf-8'))
                    row = next(r for r in records['messages'] if r['source']['message_id'] == 2)
                    self.assertEqual(len(row['message_versions']), 3)
                    last = next(v for v in row['message_versions'] if v['text'] == f'New report {second}')
                    self.assertEqual(last['urls'], [second])
                    article = row['articles'][second]
                    self.assertIn('Türkiye', article['text'])
                    self.assertIn('| 2026 | 150 |', article['text'])
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                finally:
                    db.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

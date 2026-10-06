"""Content loss regressions, with synthetic documents and a local HTTP server."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lxml import etree

from telethon.tl.types import MessageEntityTextUrl
from news_archive import article_fetcher as af
from haber_extractor import news_extractor as extractor
from news_archive import news_export as ne
from news_archive import tg_collector as tg
from tests.helpers import channel, dt, message


BODY = ('<p>The company reported annual results and approved a new factory investment. '
        'Production will begin in 2027. All amounts below are in million lira.</p>')
HTML = ('<html><head><meta charset="utf-8"><title>Results</title></head><body><article>'
        + BODY + '<p>Türkiye şirketi: net kâr 150 milyon TL.</p>'
        '<table><tr><th>Period</th><th>Profit</th></tr>'
        '<tr><td>2025</td><td>100</td></tr><tr><td>2026</td><td>150</td></tr></table>'
        '<p>Complete figures are in <a href="/report.pdf">the financial report</a>.</p>'
        '</article></body></html>').encode('utf-8')


class Response:
    def __init__(self, body=HTML, status=200, headers=None, chunks=None):
        self.body, self.status_code = body, status
        self.headers = headers if headers is not None else {'Content-Type': 'text/html; charset=ISO-8859-1'}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, **kwargs):
        return iter(self.chunks if self.chunks is not None else [self.body])

    def close(self):
        self.closed = True


class ContentTests(unittest.TestCase):
    def setUp(self):
        af.reset_stop()
        self.db = tg.open_db(':memory:')
        self.addCleanup(self.db.close)
        af.ensure_schema(self.db)
        tg.upsert_channel(self.db, channel())

    def fetch(self, response, url='https://news.test/story', **kwargs):
        session = SimpleNamespace(get=lambda *a, **kw: response)
        with patch.object(extractor, '_get_session', return_value=session):
            return af.fetch_article(url, delay=0, **kwargs)

    def save(self, text, edit=None):
        msg = message(1, text, edit=edit)
        tg.save_observation(self.db, tg.to_row(1, msg), 'live_edit' if edit else 'live_new')
        self.db.commit()

    def complete(self, text, error=None):
        result = af.ArticleResult('Results', text, error, final_url='https://news.test/story',
                                  content_type='text/html', raw_content=HTML)
        with patch.object(af, 'fetch_article', return_value=result):
            return af.fetch_urls(self.db, [('https://news.test/story', 1, 1)], delay=0)

    def test_prose_url_cleanup_preserves_balanced_parentheses_and_query(self):
        msg = message(text='See (https://news.test/story). Also https://news.test/a_(b)?id=12&lang=tr.')
        self.assertEqual(tg.extract_urls(msg), ['https://news.test/a_(b)?id=12&lang=tr',
                                                'https://news.test/story'])
        msg = message(text='Click')
        msg.entities = [MessageEntityTextUrl(offset=0, length=5, url='https://news.test/story?q=why?')]
        self.assertEqual(tg.extract_urls(msg), ['https://news.test/story?q=why?'])

    def test_repeated_documents_keep_figures_tables_links_and_turkish(self):
        for _ in range(6):
            response = Response()
            title, text, error = self.fetch(response)
            self.assertIsNone(error)
            self.assertIn('Türkiye', text)
            self.assertIn('150 milyon TL', text)
            self.assertIn('| 2025 | 100 |', text)
            self.assertIn('| 2026 | 150 |', text)
            self.assertIn('https://news.test/report.pdf', text)
            self.assertTrue(response.closed)

    def test_retained_html_table_keeps_row_and_column_boundaries(self):
        xml = ('<doc><main><p>Expected returns</p><table><tr><td><hi rend="#b">Broker</hi></td>'
               '<td><hi rend="#b">Target</hi></td><td><hi rend="#b">Return</hi></td></tr>'
               '<tr><td>Broker A</td><td>149,50 TL</td><td>+53,81%</td></tr>'
               '<tr><td>Broker B</td><td>119,00 TL</td><td>+22,43%</td></tr>'
               '</table><p>End of report</p></main></doc>')
        with patch.object(extractor.trafilatura, 'bare_extraction', return_value=SimpleNamespace(body=etree.fromstring(xml).find('main'))):
            text = af.extract_html(b'fixture', 'https://news.test/report')
        rows = [line for line in text.splitlines() if line.startswith('|')]
        self.assertEqual(len(rows), 4)  # Header, separator, two data rows.
        self.assertIn('| Broker A | 149,50 TL | +53,81% |', text)
        self.assertIn('| Broker B | 119,00 TL | +22,43% |', text)
        self.assertIn('Expected returns', text)
        self.assertIn('End of report', text)

    def test_real_dividend_article_keeps_formatted_payment_example(self):
        # Article subtree from Para Ajansi 22228800h, captured 2026-10-05.
        # XML export lost inline amounts in a nested fallback paragraph.
        raw = (Path(__file__).parent / 'fixtures/astor-dividend.html').read_bytes()
        text = af.extract_html(raw, 'https://news.test/dividend')
        for value in ('100 lot ASTOR hissesi', '186,25 TL', '200 lotu', '372,50 TL',
                      '1,8624750', '19.10.2026'):
            self.assertIn(value, text)

    def test_xml_whitespace_and_targetless_anchor_do_not_drop_news(self):
        xml = ('<doc><main>\n  <list>\n    <item><ref><hi rend="#b">ABC</hi></ref>'
               ' bought 57.707 shares at 17,69 TL.</item>\n  </list>\n'
               '  <table>\n    <row>\n      <cell role="head">Period</cell>\n'
               '      <cell role="head">Profit</cell>\n    </row>\n'
               '    <row>\n      <cell>2026</cell>\n      <cell>150</cell>\n    </row>\n'
               '  </table>\n</main></doc>')
        with patch.object(extractor.trafilatura, 'bare_extraction', return_value=SimpleNamespace(body=etree.fromstring(xml).find('main'))):
            text = af.extract_html(b'fixture', 'https://news.test/report')
        self.assertIn('bought 57.707 shares at 17,69 TL.', text)
        self.assertIn('| 2026 | 150 |', text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith('|')]), 3)

    def test_short_link_redirect_records_target_and_closes_every_response(self):
        first, second = Response(status=302, headers={'Location': 'https://news.test/story'}), Response()
        get_calls = []
        def get(url, **kwargs):
            get_calls.append(url)
            self.assertTrue(kwargs['stream'])
            self.assertFalse(kwargs['allow_redirects'])
            return first if len(get_calls) == 1 else second
        with patch.object(extractor, '_get_session', return_value=SimpleNamespace(get=get)):
            result = af.fetch_article('https://bit.ly/short', delay=0)
        self.assertIsNone(result[2])
        self.assertEqual(result.metadata['final_url'], 'https://news.test/story')
        self.assertEqual(get_calls, ['https://bit.ly/short', 'https://news.test/story'])
        self.assertTrue(first.closed and second.closed)

    def test_size_limits_apply_to_headers_and_decoded_chunks(self):
        for response in (Response(headers={'Content-Length': '9999', 'Content-Type': 'text/html'}),
                         Response(chunks=[b'a' * 60, b'b' * 60])):
            with self.subTest(headers=response.headers):
                self.assertIn('boyut', self.fetch(response, max_bytes=100)[2])
                self.assertTrue(response.closed)

    def test_redirect_loop_is_bounded_and_responses_are_closed(self):
        responses = []
        def get(*a, **kw):
            response = Response(status=302, headers={'Location': '/again'})
            responses.append(response)
            return response
        with patch.object(extractor, '_get_session', return_value=SimpleNamespace(get=get)):
            result = af.fetch_article('https://news.test/again', delay=0)
        self.assertIn('çok fazla yönlendirme', result[2])
        self.assertEqual(len(responses), extractor.MAX_REDIRECTS + 1)
        self.assertTrue(all(r.closed for r in responses))

    def test_stopping_during_fetch_leaves_the_revision_pending(self):
        url = 'https://news.test/story'
        self.save(f'Edited {url}', dt(2026, 2))
        stop = threading.Event()
        def fetch(*a, **kw):
            stop.set()
            return None, None, 'stopped'
        with patch.object(af, 'fetch_article', side_effect=fetch):
            af.fetch_urls(self.db, [(url, 1, 1)], stop_event=stop)
        self.assertEqual(af.pending_urls(self.db, {1}), [(url, 1, 1)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 0)

    def test_pdf_and_binary_types_are_not_sent_to_html_extractor(self):
        for response in (Response(b'%PDF-1.7', headers={}),
                         Response(headers={'Content-Type': 'application/pdf'}),
                         Response(headers={'Content-Type': 'image/png'})):
            with patch.object(extractor.trafilatura, 'extract') as extract:
                self.assertIn('HTML dışı', self.fetch(response)[2])
                extract.assert_not_called()

    def test_same_url_edit_checks_once_and_preserves_versions_on_failure(self):
        url = 'https://news.test/story'
        self.save(f'Original {url}')
        self.complete('Profit 100')
        self.assertEqual(af.pending_urls(self.db, {1}), [])
        self.save(f'Updated {url}', dt(2026, 2))
        self.assertEqual(af.pending_urls(self.db, {1}), [(url, 1, 1)])
        self.complete('Profit 150')
        self.assertEqual(af.pending_urls(self.db, {1}), [])
        self.complete('Profit 150')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM article_versions').fetchone()[0], 2)
        self.save(f'Another edit {url}', dt(2026, 3))
        self.complete(None, 'HTTP 503')
        self.assertEqual(self.db.execute('SELECT text,status FROM articles').fetchone(), ('Profit 150', 'error'))
        self.assertEqual(af.pending_urls(self.db, {1}), [])
        with tempfile.TemporaryDirectory() as tmp:
            ne.export_json(self.db, {1}, Path(tmp) / 'news.json')
            data = json.loads((Path(tmp) / 'news.json').read_text(encoding='utf-8'))
        record = data['messages'][0]
        article = record['articles'][url]
        self.assertEqual(article['text'], 'Profit 150')
        self.assertEqual([v['text'] for v in article['history']], ['Profit 100'])
        self.assertEqual(article['last_check']['status'], 'error')
        for version in [article, *article['history']]:
            self.assertNotIn('final_url', version)
            self.assertNotIn('content_type', version)
        self.assertEqual(len(record['message_versions']), 3)
        raw = self.db.execute('SELECT raw_content_zlib FROM article_versions LIMIT 1').fetchone()[0]
        self.assertEqual(zlib.decompress(raw), HTML)

    def test_edit_arriving_during_download_is_not_acknowledged(self):
        url = 'https://news.test/story'
        self.save(f'Edit one {url}', dt(2026, 2))
        def fetch(*a, **kw):
            self.save(f'Edit two {url}', dt(2026, 3))
            return 'Results', 'Profit 150', None
        with patch.object(af, 'fetch_article', side_effect=fetch):
            af.fetch_urls(self.db, [(url, 1, 1)], delay=0)
        self.assertEqual(af.pending_urls(self.db, {1}), [(url, 1, 1)])
        self.complete('Profit 150')
        self.assertEqual(af.pending_urls(self.db, {1}), [])

    def test_shared_article_is_exported_under_each_linking_message(self):
        url = 'https://news.test/story'
        self.save(f'First message {url}')
        tg.save_observation(self.db, tg.to_row(1, message(2, f'Second message {url}')), 'live_new')
        self.db.commit()
        self.complete('Unique article body 150 million TL')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'news.json'
            ne.export_json(self.db, {1}, target)
            serialized = target.read_text(encoding='utf-8')
            data = json.loads(serialized)
        self.assertEqual(data['schema_version'], '3.2')
        self.assertEqual(set(data), {'schema_version', 'messages'})
        self.assertEqual(len(data['messages']), 2)
        self.assertEqual(serialized.count('Unique article body 150 million TL'), 2)
        for record in data['messages']:
            self.assertNotIn('text', record)
            self.assertEqual(record['message_versions'][0]['urls'], [url])
            self.assertNotIn('article_ids', record['message_versions'][0])
            self.assertNotIn('edited_at', record['message_versions'][0])
            self.assertNotIn('first_observed_at', record)
            self.assertNotIn('observation_source', record)
            self.assertEqual(set(record['articles']), {url})
            self.assertNotIn('article_id', record['articles'][url])
            self.assertNotIn('history', record['articles'][url])
            self.assertNotIn('last_check', record['articles'][url])
            self.assertNotIn('final_url', record['articles'][url])
            self.assertNotIn('content_type', record['articles'][url])
            self.assertEqual(record['articles'][url]['text'], 'Unique article body 150 million TL')

    def test_edited_url_and_article_are_exported_under_correct_message_version(self):
        self.save('Original https://news.test/old')
        self.save('Edited https://news.test/new', dt(2026, 2))
        for url, _, _ in af.pending_urls(self.db, {1}):
            with patch.object(af, 'fetch_article', return_value=('Title', url, None)):
                af.fetch_urls(self.db, [(url, 1, 1)], delay=0)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'news.json'
            ne.export_json(self.db, {1}, target)
            data = json.loads(target.read_text(encoding='utf-8'))
        record = data['messages'][0]
        first = next(v for v in record['message_versions'] if v['version_id'] == record['first_version_id'])
        self.assertEqual(first['text'], 'Original https://news.test/old')
        versions = {v['text']: v for v in record['message_versions']}
        self.assertEqual(versions['Edited https://news.test/new']['urls'], ['https://news.test/new'])
        self.assertEqual(versions['Original https://news.test/old']['urls'], ['https://news.test/old'])
        self.assertEqual(set(record['articles']), {'https://news.test/new', 'https://news.test/old'})

    def test_legacy_migration_keeps_old_text_before_refetch(self):
        legacy = sqlite3.connect(':memory:')
        self.addCleanup(legacy.close)
        legacy.execute('CREATE TABLE articles(url TEXT PRIMARY KEY, domain TEXT NOT NULL, title TEXT, text TEXT, '
                       'source_channel INTEGER, source_message INTEGER, status TEXT, error TEXT, fetched_at TEXT NOT NULL)')
        legacy.execute('INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?)',
                       ('https://news.test/story', 'news.test', 'Legacy', 'Old profit', 1, 1, 'ok', None, dt().isoformat()))
        af.ensure_schema(legacy)
        af.ensure_schema(legacy)
        self.assertEqual(legacy.execute('SELECT text FROM article_versions').fetchall(), [('Old profit',)])
        with patch.object(af, 'fetch_article', return_value=('New', 'New profit', None)):
            af.fetch_urls(legacy, [('https://news.test/story', 1, 1)], delay=0)
        self.assertEqual(legacy.execute('SELECT COUNT(*) FROM article_versions').fetchone()[0], 2)


class DownloadDeadlineTests(unittest.TestCase):
    def test_slow_drip_body_is_interrupted_before_read_timeout(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', '300')
                self.end_headers()
                try:
                    for _ in range(300):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(0.03)
                except (OSError, ValueError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            start = time.monotonic()
            result = af.fetch_article(f'http://127.0.0.1:{server.server_port}/slow',
                                      timeout=2, total_timeout=0.2, delay=0)
            self.assertLess(time.monotonic() - start, 1)
            self.assertIn('toplam indirme', result[2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

"""Reading the article a page declares for itself; synthetic pages shaped like real failures."""
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from lxml import etree

from haber_extractor import news_extractor as extractor
from haber_extractor.structured_data import page_facts
from news_archive import article_fetcher as af
from news_archive import news_export as ne
from news_archive import tg_collector as tg
from tests.helpers import channel, dt, message

LEAD = ('Altın piyasasında eylül başındaki yükselişin ardından kâr satışları öne çıktı '
        've İstanbul Kapalıçarşı fiyatları yatay seyretti.')
PRICES = 'Serbest piyasada oluşan güncel altın fiyatları şöyle:'
ITEMS = ['Alış: 6.489,62 TL', 'Satış: 6.545,25 TL']
NOTE = 'Uzmanlar, yıl sonuna kadar dalgalı seyrin sürebileceğini ve alımlarda temkinli olunmasını öneriyor.'
CREDIT = 'Fotoğraflar Anadolu Ajansı muhabirleri tarafından Kapalıçarşı esnafının izniyle çekilmiştir.'
OTHER = ('Belediye, hafta sonu yapılacak maraton nedeniyle bazı yolların trafiğe kapatılacağını '
         've sürücülerin başka güzergahları kullanması gerektiğini duyurdu.')


def article(body, **fields):
    return {'@context': 'https://schema.org', '@type': 'NewsArticle', 'headline': 'Altında son rakamlar',
            'datePublished': '2026-10-04T09:44:13+03:00', 'dateModified': '2026-10-04T09:47:23+03:00',
            'articleBody': body, **fields}


def page(declared, body, og_type='article', also=None, **fields):
    """A news page: site chrome around `body`, with `declared` as its JSON-LD article body."""
    nodes = [article(declared, **fields)] if declared is not None else []
    script = ''.join(f'<script type="application/ld+json">{json.dumps(node)}</script>'
                     for node in nodes + ([also] if also else []))
    return ('<html><head><meta charset="utf-8"><title>Altın | Site</title>'
            f'<meta property="og:type" content="{og_type}">'
            f'<link rel="canonical" href="/altin">{script}</head><body>'
            '<nav><ul><li><a href="/ekonomi">Ekonomi</a></li><li><a href="/istanbul">İstanbul</a></li></ul></nav>'
            f'<h1>Altında son rakamlar</h1>{body}'
            '<div class="share">Haberi Paylaş</div>'
            '<footer><p>Kategoriler</p><p>İstanbul</p></footer></body></html>').encode('utf-8')


def layout(*paragraphs):
    """What the layout heuristic would have chosen, without running it."""
    body = etree.Element('body')
    for text in paragraphs:
        etree.SubElement(body, 'p').text = text
    return patch.object(extractor.trafilatura, 'bare_extraction', return_value=SimpleNamespace(body=body))


class DeclaredBodyTests(unittest.TestCase):
    def test_subheadings_and_short_list_items_are_kept_and_page_chrome_is_not(self):
        # TGRT Haber 3363164: the price list after "şöyle:" and every <h3> were missing.
        body = (f'<div><p>{LEAD}</p><h3><strong>GÜNCEL ALTIN FİYATLARI</strong></h3><p>{PRICES}</p>'
                f'<ul><li>{ITEMS[0]}</li><li>{ITEMS[1]}</li></ul></div>')
        declared = ' '.join([LEAD, 'GÜNCEL ALTIN FİYATLARI', PRICES, *ITEMS])
        result = extractor.extract_page(page(declared, body), 'https://news.test/altin')
        self.assertEqual(result.text, '\n\n'.join([
            LEAD, '### **GÜNCEL ALTIN FİYATLARI**', PRICES, f'- {ITEMS[0]}\n- {ITEMS[1]}']))
        self.assertEqual(result.title, 'Altında son rakamlar')
        self.assertEqual(result.details, {
            'method': 'declared', 'source': 'json-ld', 'coverage': 1.0, 'undeclared': 0.0, 'is_article': True,
            'published_at': '2026-10-04T06:44:13+00:00', 'modified_at': '2026-10-04T06:47:23+00:00',
            'canonical_url': 'https://news.test/altin'})

    def test_paragraph_outranks_breadcrumb_and_summary_repeating_its_opening(self):
        # Para Ajansi 22230375h: a breadcrumb and the summary line took the first paragraph's place.
        opening = LEAD.split(' ve ')[0] + '.'
        body = (f'<ul><li><a href="/altin">{opening}</a></li></ul><h2>{opening}</h2>'
                f'<div><p>{opening} {NOTE}</p><p>{PRICES}</p></div>')
        result = extractor.extract_page(page(f'{opening} {NOTE} {PRICES}', body), 'https://news.test/altin')
        self.assertEqual(result.text, f'{opening} {NOTE}\n\n{PRICES}')
        self.assertEqual(result.details['coverage'], 1.0)

    def test_declaration_of_another_story_on_the_same_page_is_not_joined(self):
        # Related stories and the next article of an endless scroll carry declarations too.
        body = f'<div><p>{LEAD}</p><p>{NOTE}</p></div><aside><h3>Maraton</h3><p>{OTHER}</p></aside>'
        other = article(OTHER, headline='Maraton', url='https://news.test/maraton')
        cases = {
            'told apart by address': page(f'{LEAD} {NOTE}', body, also=other,
                                          mainEntityOfPage={'@type': 'WebPage', '@id': 'https://www.news.test/altin/'}),
            'told apart by title': page(f'{LEAD} {NOTE}', body, also=article(OTHER, headline='Maraton')),
            'cited inside the article': page(f'{LEAD} {NOTE}', body, citation=other),
        }
        for name, html in cases.items():
            # The layout reading takes the neighbour for content; that alone must not bring it in.
            with self.subTest(name), layout(LEAD, NOTE, OTHER):
                result = extractor.extract_page(html, 'https://news.test/altin?utm_source=telegram')
                self.assertEqual(result.text, f'{LEAD}\n\n{NOTE}')
                self.assertEqual(result.title, 'Altında son rakamlar')
                self.assertEqual((result.details['coverage'], result.details['undeclared']), (1.0, 0.0))
        # Two stories, no address and no title to tell them apart: trust neither.
        html = page(f'{LEAD} {NOTE}', body, also=article(OTHER, headline='Maraton'), headline='Piyasalar')
        with layout(LEAD, NOTE):
            self.assertEqual(extractor.extract_page(html, 'https://news.test/altin').details['method'], 'layout')

    def test_marked_body_of_a_sidebar_story_does_not_stand_in_for_the_article(self):
        # Microdata: neither the first nor the longest marked body is necessarily this page's.
        mine, theirs = f'<div itemprop="articleBody"><p>{LEAD}</p><p>{NOTE}</p></div>', f'<div itemprop="articleBody"><p>{OTHER} {OTHER}</p></div>'
        item = '<article itemscope itemtype="https://schema.org/NewsArticle">{}</article>'
        cases = {
            'told apart by title': item.format('<h3 itemprop="headline">Maraton</h3>' + theirs)
                                   + item.format('<h2 itemprop="headline">Altında son rakamlar</h2>' + mine),
            'told apart by address': item.format('<link itemprop="mainEntityOfPage" href="/maraton">' + theirs)
                                     + item.format('<link itemprop="url" href="https://www.news.test/altin/">' + mine),
            'no item, told apart by the heading before': f'<h3>Maraton</h3>{theirs}<h2>Altında son rakamlar</h2>{mine}',
        }
        for name, body in cases.items():
            with self.subTest(name), layout(LEAD, NOTE, f'{OTHER} {OTHER}'):
                result = extractor.extract_page(page(None, body), 'https://news.test/altin')
                self.assertEqual(result.text, f'{LEAD}\n\n{NOTE}')
                self.assertEqual({k: result.details[k] for k in ('source', 'coverage', 'undeclared')},
                                 {'source': 'microdata', 'coverage': 1.0, 'undeclared': 0.0})
        with layout(LEAD, NOTE):   # nothing tells the two apart: trust neither
            result = extractor.extract_page(page(None, f'<h3>Maraton</h3>{theirs}<h3>Piyasalar</h3>{mine}'),
                                            'https://news.test/altin')
        self.assertEqual(result.details['method'], 'layout')
        # One item may mark its body in pieces, e.g. around an advertisement.
        pieces = item.format(f'<div itemprop="articleBody"><p>{LEAD}</p></div><div>Reklam</div>'
                             f'<div itemprop="articleBody"><p>{NOTE}</p></div>')
        result = extractor.extract_page(page(None, pieces), 'https://news.test/altin')
        self.assertEqual((result.text, result.details['coverage']), (f'{LEAD}\n\n{NOTE}', 1.0))

    def test_layout_reading_drops_what_another_story_on_the_page_declares(self):
        # A declared body too short to lead leaves the layout reading, which takes neighbours for content.
        body = f'<div><p>{PRICES}</p><p>{NOTE}</p></div><aside><h3>Maraton</h3><p>{OTHER}</p></aside>'
        other = article(OTHER, headline='Maraton', url='https://news.test/maraton')
        with layout('Maraton', PRICES, NOTE, OTHER):
            result = extractor.extract_page(page(PRICES, body, also=other, url='/altin'), 'https://news.test/altin')
        self.assertEqual((result.text, result.details['method']), (f'{PRICES}\n\n{NOTE}', 'layout'))
        # Without knowing which declaration is the page's own, nothing may be dropped.
        with layout(PRICES, NOTE, OTHER):
            result = extractor.extract_page(page(PRICES, body, also=article(OTHER, headline='Maraton'),
                                                 headline='Piyasalar'), 'https://news.test/altin')
        self.assertEqual(result.text, f'{PRICES}\n\n{NOTE}\n\n{OTHER}')

    def test_live_blog_updates_in_other_sections_and_undeclared_lead_are_read(self):
        # A Haber live stories: only the first of several <article> sections was read.
        updates = [{'@type': 'BlogPosting', 'headline': 'ARTÇILAR SÜRÜYOR', 'articleBody': NOTE},
                   {'@type': 'BlogPosting', 'headline': 'VALİ AÇIKLADI', 'articleBody': LEAD}]
        body = (f'<article><p><em>Giriş Tarihi:</em></p><p>{PRICES} {PRICES}</p></article>'
                f'<article><h2>VALİ AÇIKLADI</h2><p>{LEAD}</p></article>'
                f'<article><h2>ARTÇILAR SÜRÜYOR</h2><p>{NOTE}</p></article>'
                f'<aside><p>Başka bir haberin uzun tanıtım cümlesi burada yer alıyor ve okuru oraya çağırıyor.</p></aside>')
        html = page('Kısa giriş.', body, **{'@type': 'LiveBlogPosting', 'liveBlogUpdate': updates})
        with layout('Giriş Tarihi:', f'{PRICES} {PRICES}'):
            result = extractor.extract_page(html, 'https://news.test/altin')
        self.assertEqual(result.text, '\n\n'.join([
            f'{PRICES} {PRICES}', '## VALİ AÇIKLADI', LEAD, '## ARTÇILAR SÜRÜYOR', NOTE]))
        # The page does not print the declared intro; the opening is not in the declaration.
        self.assertEqual({k: result.details[k] for k in ('method', 'coverage', 'undeclared')},
                         {'method': 'declared', 'coverage': 0.96, 'undeclared': 0.29})

    def test_undeclared_text_must_be_prose_the_layout_reading_also_chose(self):
        # GDH and TGRT Haber: long breadcrumbs repeating the title came along with the lead.
        crumb = ('Haberler › Ekonomi › Altın › Altında son rakamlar: gram, çeyrek ve bilezikte '
                 'güncel durum ile uzman yorumları')
        body = (f'<div>{crumb}</div><p><a href="/abone">{CREDIT}</a></p><p>{PRICES} {PRICES}</p>'
                f'<div><p>{LEAD}</p><p>{NOTE}</p></div><p>{OTHER}</p>')
        with layout(crumb, CREDIT, f'{PRICES} {PRICES}', OTHER):
            result = extractor.extract_page(page(f'{LEAD} {NOTE}', body), 'https://news.test/altin')
        self.assertEqual(result.text, f'{PRICES} {PRICES}\n\n{LEAD}\n\n{NOTE}')

    def test_paragraphs_beside_the_declared_ones_are_kept_when_the_declaration_stops_early(self):
        body = f'<div><p>{LEAD}</p><p>{NOTE}</p><p>{CREDIT}</p></div><aside><p>{OTHER}</p></aside>'
        with layout(LEAD, NOTE, CREDIT, OTHER):
            result = extractor.extract_page(page(f'{LEAD} {NOTE}', body), 'https://news.test/altin')
        self.assertEqual(result.text, f'{LEAD}\n\n{NOTE}\n\n{CREDIT}')
        self.assertEqual((result.details['coverage'], result.details['undeclared']), (1.0, 0.3))

    def test_data_table_is_kept_whole_and_layout_table_is_read_as_text(self):
        table = ('<table><tr><th>Hisse</th><th>Ağırlık</th></tr>'
                 '<tr><td>GARAN</td><td>%13</td></tr><tr><td>TAVHL</td><td>%10</td></tr></table>')
        body = f'<table><tr><td><p>{LEAD}</p>{table}<p>{NOTE}</p></td></tr></table>'
        result = extractor.extract_page(page(f'{LEAD} Hisse Ağırlık GARAN %13 TAVHL %10 {NOTE}', body),
                                        'https://news.test/altin')
        self.assertEqual(result.text, '\n\n'.join([
            LEAD, '| Hisse | Ağırlık | \n|---|---|\n| GARAN | %13 | \n| TAVHL | %10 | ', NOTE]))

    def test_summary_repeated_as_body_and_unprinted_body_fall_back_to_layout(self):
        # Anadolu Ajansi declares its summary as articleBody; that is no article.
        body = f'<div><p>{LEAD}</p><p>{NOTE}</p></div>'
        cases = {'summary': page(f'{LEAD} {NOTE}'[:170], body, description=f'{LEAD} {NOTE}'[:170]),
                 'script-rendered': page(f'{PRICES} {PRICES} {PRICES} {PRICES}', body)}
        for name, html in cases.items():
            with self.subTest(name), layout('Altında son rakamlar', LEAD, NOTE):
                result = extractor.extract_page(html, 'https://news.test/altin')
                self.assertEqual(result.text, f'{LEAD}\n\n{NOTE}')   # without the leading title
                self.assertEqual(result.details['method'], 'layout')
                self.assertNotIn('coverage', result.details)

    def test_page_that_is_not_an_article_is_marked_and_zoneless_time_is_not_guessed(self):
        html = page(None, f'<p>{LEAD}</p>', og_type='website')
        with layout(LEAD):
            self.assertIs(extractor.extract_page(html, 'https://news.test/altin').details['is_article'], False)
        html = page(f'{LEAD} {NOTE}', f'<p>{LEAD}</p><p>{NOTE}</p>', datePublished='2026-10-04T09:44:13',
                    dateModified=None)
        facts = page_facts(extractor.load_html(html), 'https://news.test/altin')
        self.assertEqual((facts['is_article'], facts['published_at'], facts['modified_at']), (True, None, None))
        self.assertIsNone(page_facts(extractor.load_html(b'<html><body><p>x</p></body></html>'),
                                     'https://news.test/a')['is_article'])

    def test_strict_layout_reading_vetoes_menus_and_labels_but_not_the_article(self):
        # TRT Haber printed its whole site menu; the strict reading alone lost paragraphs on Anadolu Ajansi.
        strict = etree.fromstring(f'<body><p>{LEAD}</p><p>{NOTE}</p><p>Kaynak: AA</p></body>')
        loose = etree.fromstring(
            f'<body><list><item><ref target="/a">{PRICES} {PRICES}</ref></item><item>İstanbul</item></list>'
            f'<p>{LEAD}</p><p>{ITEMS[0]}</p><p>{NOTE}</p><p>Haberi Paylaş</p><p>Kaynak: AA</p><p>{CREDIT}</p></body>')
        with patch.object(extractor.trafilatura, 'bare_extraction',
                          side_effect=lambda raw, **kw: SimpleNamespace(body=strict if kw['favor_precision'] else loose)):
            text = extractor.extract_page(b'<html><body><p>x</p></body></html>', 'https://news.test/a').text
        # Kept: the short price line between agreed paragraphs, and a paragraph only the loose reading has.
        self.assertEqual(text, '\n\n'.join([LEAD, ITEMS[0], NOTE, 'Kaynak: AA', CREDIT]))


class StoredExtractionTests(unittest.TestCase):
    def setUp(self):
        af.reset_stop()
        self.db = tg.open_db(':memory:')
        self.addCleanup(self.db.close)
        af.ensure_schema(self.db)
        tg.upsert_channel(self.db, channel())
        self.url = 'https://news.test/altin'
        tg.save_observation(self.db, tg.to_row(1, message(1, f'Altın {self.url}')), 'live_new')
        self.db.commit()

    def export(self):
        with tempfile.TemporaryDirectory() as tmp:
            ne.export_json(self.db, {1}, Path(tmp) / 'news.json')
            data = json.loads((Path(tmp) / 'news.json').read_text(encoding='utf-8'))
        return data['messages'][0]['articles'][self.url]

    def fetch(self, html, **extraction):
        result = af.ArticleResult('Old title', 'Old text', None, final_url='https://www.news.test/altin',
                                  content_type='text/html', raw_content=html, extraction=extraction)
        with patch.object(af, 'fetch_article', return_value=result):
            af.fetch_urls(self.db, [(self.url, 1, 1)], delay=0)

    def test_export_tells_how_the_text_was_read_and_what_the_page_declares(self):
        self.fetch(b'', method='declared', source='json-ld', coverage=0.97, is_article=False,
                   published_at=dt(2025).isoformat(), modified_at=dt(2026, 2).isoformat(),
                   canonical_url='https://www.news.test/altin')
        self.assertEqual({k: v for k, v in self.export().items() if k not in ('version_id', 'fetched_at')}, {
            'title': 'Old title', 'text': 'Old text', 'extraction': {'method': 'declared', 'coverage': 0.97},
            'not_article': True, 'page_published_at': dt(2025).isoformat(),
            'page_modified_at': dt(2026, 2).isoformat(), 'canonical_url': 'https://www.news.test/altin',
            'modified_after_message': True})   # the message is dated 2026-01-01

    def test_export_omits_what_is_unknown_or_unremarkable(self):
        self.fetch(b'', method='layout', is_article=True, modified_at=dt(2025).isoformat(),
                   canonical_url=self.url)
        self.assertEqual(set(self.export()), {'version_id', 'fetched_at', 'title', 'text', 'extraction',
                                              'page_modified_at'})
        self.assertEqual(self.export()['extraction'], {'method': 'layout'})

    def test_stored_page_is_read_again_in_place_without_a_new_version(self):
        html = page(f'{LEAD} {NOTE}', f'<div><p>{LEAD}</p><p>{NOTE}</p></div>')
        self.fetch(html)
        before = self.db.execute('SELECT version_id,fetched_at FROM article_versions').fetchall()
        self.assertEqual([p.text for *_, p in af.reextract(self.db)], [f'{LEAD}\n\n{NOTE}'])
        self.assertEqual(self.export()['text'], 'Old text')   # a dry run writes nothing
        list(af.reextract(self.db, apply=True))
        self.assertEqual(self.db.execute('SELECT version_id,fetched_at FROM article_versions').fetchall(), before)
        self.assertEqual(self.db.execute('SELECT title,text FROM articles').fetchone(),
                         ('Altında son rakamlar', f'{LEAD}\n\n{NOTE}'))
        self.assertEqual(zlib.decompress(self.db.execute(
            'SELECT raw_content_zlib FROM article_versions').fetchone()[0]), html)
        exported = self.export()
        self.assertEqual((exported['text'], exported['extraction']), (
            f'{LEAD}\n\n{NOTE}', {'method': 'declared', 'coverage': 1.0, 'undeclared': 0.0}))
        self.assertNotIn('history', exported)
        # A later download of the unchanged page is still the same version.
        result = af.ArticleResult('Altında son rakamlar', f'{LEAD}\n\n{NOTE}', None,
                                  final_url='https://www.news.test/altin', content_type='text/html', raw_content=html)
        with patch.object(af, 'fetch_article', return_value=result):
            af.fetch_urls(self.db, [(self.url, 1, 1)], delay=0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM article_versions').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()

"""Download and extract HTML news without Telegram or a database."""
import socket
import threading
import time
from collections import namedtuple
from urllib.parse import urlsplit, urljoin

import requests
import trafilatura
from lxml import etree
from trafilatura.utils import load_html
from trafilatura.xml import xmltotxt
if __package__:
    from .structured_data import ANCHOR, PROSE, Tape, compact, declared_body, page_facts
    from .url_utils import normalize_url
else:
    from structured_data import ANCHOR, PROSE, Tape, compact, declared_body, page_facts
    from url_utils import normalize_url

# t.me linkleri, resim/video hostları, sosyal medya — makale değil
SKIP_DOMAINS = frozenset({
    "t.me", "telegram.me", "telegram.org",
    "instagram.com", "www.instagram.com",
    "twitter.com", "x.com",
    "facebook.com", "www.facebook.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "tiktok.com", "www.tiktok.com",
    "linkedin.com", "www.linkedin.com",
    "play.google.com", "apps.apple.com",
    "imgur.com", "i.imgur.com",
})

DEFAULT_DELAY = 2.0  # aynı domain'e istekler arası minimum saniye
MAX_BYTES = 10 * 1024 * 1024
TOTAL_TIMEOUT = 45.0
MAX_REDIRECTS = 8

_stop = threading.Event()


def request_stop():
    """Devam eden çekimi durdurur."""
    _stop.set()


def reset_stop():
    _stop.clear()


def domain_of(url):
    return urlsplit(url).hostname or ""


def should_skip(url):
    d = domain_of(url)
    if not d or urlsplit(url).scheme not in {"http", "https"}:
        return True
    # Ana domain'i kontrol et (sub.example.com → example.com)
    parts = d.split(".")
    base = ".".join(parts[-2:]) if len(parts) >= 2 else d
    return base in SKIP_DOMAINS or d in SKIP_DOMAINS


_sessions = threading.local()

def _get_session():
    if not hasattr(_sessions, "session"):
        _sessions.session = requests.Session()
        _sessions.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.3",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    return _sessions.session


class EmptyArticleError(str):
    """Extraction failure, distinct from HTTP/network errors; string compatible."""


class DomainLimiter:
    """Shared spacing across live, pending and retry requests in this process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.turns = {}
        self.last_request = {}

    def wait(self, domain, delay, stop):
        with self.lock:
            turn = self.turns.setdefault(domain, threading.Lock())
        # Requests to one site queue on that site's lock; a wait for one site
        # does not hold back a request to another.
        with turn:
            remaining = delay - (time.monotonic() - self.last_request.get(domain, 0))
            if remaining > 0 and stop.wait(remaining):
                return False
            if stop.is_set():
                return False
            self.last_request[domain] = time.monotonic()
            return True


_limiter = DomainLimiter()


class ArticleResult(tuple):
    """Retain the three-value API while carrying download provenance."""
    def __new__(cls, title, text, error, **metadata):
        result = super().__new__(cls, (title, text, error))
        result.metadata = metadata
        return result

    @property
    def title(self):
        return self[0]

    @property
    def text(self):
        return self[1]

    @property
    def error(self):
        return self[2]

    def to_dict(self):
        """JSON-ready result; raw HTML remains available in metadata only."""
        return {"title": self.title, "text": self.text, "error": self.error,
                "status": "empty" if isinstance(self.error, EmptyArticleError)
                          else "error" if self.error else "ok",
                "final_url": self.metadata.get("final_url"),
                "content_type": self.metadata.get("content_type"),
                "extraction": self.metadata.get("extraction")}


def _abort_response(resp):
    # Interrupt a slow-drip body even when iter_content hasn't yielded a chunk.
    # Requests has no public whole-download deadline. Socket shutdown is kept
    # here and covered by a real local HTTP test; never wait on a buffered lock.
    raw = getattr(resp, "raw", None)
    connection = getattr(raw, "_connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        fp = getattr(getattr(raw, "_fp", None), "fp", None)
        sock = getattr(getattr(fp, "raw", None), "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def fetch_article(url, timeout=15, total_timeout=TOTAL_TIMEOUT, max_bytes=MAX_BYTES,
                  delay=DEFAULT_DELAY, stop_event=None):
    """Bound HTML downloads; decode from bytes, preserving links and tables."""
    current = normalize_url(url)
    if not current or should_skip(current):
        return ArticleResult(None, None, "desteklenmeyen URL")
    deadline = time.monotonic() + total_timeout
    stop = stop_event if stop_event is not None else _stop
    resp = timer = None
    metadata = {}
    try:
        for redirect in range(MAX_REDIRECTS + 1):
            if not _limiter.wait(domain_of(current), delay, stop):
                return ArticleResult(None, None, "indirme durduruldu", **metadata)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("toplam indirme süresi aşıldı")
            resp = _get_session().get(current, timeout=min(timeout, remaining),
                                      allow_redirects=False, stream=True)
            metadata["final_url"] = current
            if resp.status_code in {301, 302, 303, 307, 308}:
                target = normalize_url(urljoin(current, resp.headers.get("Location", "")))
                resp.close()
                resp = None
                if not target or should_skip(target):
                    raise ValueError("yönlendirme hedefi desteklenmiyor")
                if redirect == MAX_REDIRECTS:
                    raise ValueError("çok fazla yönlendirme")
                current = target
                continue
            break
        if resp.status_code >= 400:
            return ArticleResult(None, None, f"HTTP {resp.status_code}", **metadata)
        kind = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        metadata["content_type"] = kind or "text/html"
        if kind and kind not in {"text/html", "application/xhtml+xml"}:
            return ArticleResult(None, None, f"HTML dışı içerik: {kind}", **metadata)
        size = resp.headers.get("Content-Length", "")
        if size.isdigit() and int(size) > max_bytes:
            raise ValueError("indirme boyutu sınırı aşıldı")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("toplam indirme süresi aşıldı")
        timer = threading.Timer(remaining, _abort_response, (resp,))
        timer.daemon = True
        timer.start()
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=16384):
            if time.monotonic() >= deadline:
                raise TimeoutError("toplam indirme süresi aşıldı")
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("indirme boyutu sınırı aşıldı")
            chunks.append(chunk)
        if time.monotonic() >= deadline:
            raise TimeoutError("toplam indirme süresi aşıldı")
        raw = b"".join(chunks)
        if raw.lstrip().startswith(b"%PDF"):
            return ArticleResult(None, None, "HTML dışı içerik: application/pdf",
                                 final_url=current, content_type="application/pdf")
        metadata["raw_content"] = raw
    except (requests.RequestException, OSError, ValueError) as exc:
        if time.monotonic() >= deadline:
            exc = TimeoutError("toplam indirme süresi aşıldı")
        return ArticleResult(None, None, f"indirme hatası: {exc}", **metadata)
    finally:
        if timer is not None:
            timer.cancel()
        if resp is not None:
            resp.close()

    # Passing bytes lets the extractor detect HTML charset/BOM rather than
    # inheriting a possibly incorrect HTTP-header decoding from resp.text.
    page = extract_page(raw, current)
    metadata["extraction"] = page.details
    title = page.title
    if not title:
        document_metadata = trafilatura.extract_metadata(raw)
        title = document_metadata.title if document_metadata else None
    if not page.text:
        return ArticleResult(title, None, EmptyArticleError("metin çıkarılamadı"), **metadata)
    return ArticleResult(title, page.text, None, **metadata)


Page = namedtuple("Page", "title text details")


def extract_page(raw, url):
    """Article title and text, with how the text was established.

    `details["method"]` is "declared" when the page names its own article body
    (schema.org) and the text was read from the blocks printing it; `coverage`
    is the share of that declaration found and `undeclared` the share of the
    text it does not contain. Otherwise it is "layout": the layout heuristic
    alone, which nothing on the page confirms.
    """
    tree = load_html(raw)
    facts = page_facts(tree, url) if tree is not None else {}
    main, details = _layout_body(raw, url), {"method": "layout"}
    if facts.get("body"):
        layout_text = "".join(main.itertext()) if main is not None else ""
        declared, coverage, undeclared = declared_body(tree, facts["body"], url, layout_text,
                                                       facts["headline"], facts["foreign"])
        # A declaration the page barely prints (script-rendered or paywalled
        # text) cannot vouch for anything; keep the layout result then. Above
        # that the label only names the method: read `coverage` and `undeclared`.
        if coverage >= 0.5:
            main, details = declared, {"method": "declared", "source": facts["body_source"],
                                       "coverage": round(coverage, 2), "undeclared": round(undeclared, 2)}
    title = compact(facts.get("headline"))
    if details["method"] == "layout" and main is not None:
        _drop_foreign(main, facts.get("foreign") or [])
        if title and len(main) and compact("".join(main[0].itertext())) == title:
            main.remove(main[0])   # the title is returned on its own, never inside the text
    for key in ("is_article", "published_at", "modified_at", "canonical_url"):
        if facts.get(key) is not None:
            details[key] = facts[key]
    text = _render(main) if main is not None else None
    return Page(facts.get("headline"), text or None, details)


def extract_html(raw, url):
    """Article text of an already downloaded page; no network access."""
    return extract_page(raw, url).text


def _drop_foreign(main, foreign):
    """Remove what other stories on the page declare as their own.

    The layout reading cannot tell a neighbouring story from the article; the
    neighbour's own declaration can. Used only where no declared body leads.
    """
    theirs, labels = Tape(" ".join(foreign)), {compact(text) for text in foreign}
    for element in list(main):
        for block in element.findall("item") if element.tag == "list" else [element]:
            text = compact("".join(block.itertext()))
            if text in labels or len(text) >= ANCHOR and theirs.claim(text):
                block.getparent().remove(block)


def _layout_body(raw, url):
    """Trafilatura's layout-based choice of the main content, as its XML tree.

    Its strict reading drops menus but at times real paragraphs as well; the
    loose one keeps both. So the loose reading decides what the text is, and
    the strict one may only veto what is not prose: a run of links anywhere,
    and a block too short to be a sentence when it lies outside the stretch of
    paragraphs both readings agree on. Inside it, short blocks are subheadings,
    list items and figures; a short list closing the article can still be lost.
    """
    strict, loose = (trafilatura.bare_extraction(raw, url=url, include_comments=False,
                                                 include_tables=True, include_links=True,
                                                 include_formatting=True, deduplicate=False,
                                                 favor_precision=precise) for precise in (True, False))
    strict, loose = (None if document is None else document.body for document in (strict, loose))
    if strict is None or loose is None:
        return strict if loose is None else loose
    kept, blocks = Tape("".join(strict.itertext())), []
    for element in loose:
        for block in element.findall("item") if element.tag == "list" else [element]:
            text = compact("".join(block.itertext()))
            links = sum(len(compact("".join(ref.itertext()))) for ref in block.iter("ref"))
            prose = len(text) >= PROSE and links * 2 < len(text)
            blocks.append((block, text, links, prose and bool(kept.claim(text))))
    agreed = [index for index, block in enumerate(blocks) if block[3]]
    for index, (block, text, links, _) in enumerate(blocks):
        inside = bool(agreed) and agreed[0] < index < agreed[-1]
        if (links * 2 >= len(text) or len(text) < PROSE and not inside) and not kept.claim(text):
            block.getparent().remove(block)
    for element in loose.findall("list"):
        if not len(element):
            loose.remove(element)
    return loose


def _render(main):
    """Normalize retained HTML table tags before Markdown serialization.

    Trafilatura's fallback may retain tr/td inside an extracted table instead
    of its row/cell vocabulary. Text serialization otherwise merges columns.
    Work on the selected article tree, never append all tables from the page.
    """
    # The XML export sanitizes fallback trees and can discard inline content
    # inside nested paragraphs. Render the extracted tree directly instead.
    for table in list(main.iter("table")):
        retained_html_rows = bool(table.findall("tr"))
        for element in table.iter("tr", "td", "th"):
            if element.tag == "tr":
                element.tag = "row"
            else:
                if element.tag == "th":
                    element.set("role", "head")
                element.tag = "cell"
        first = table.find("row")
        if first is not None and not any(c.get("role") == "head" for c in first.findall("cell")):
            # Markdown displays its first retained row as the table header;
            # no source row or cell is removed or added.
            for cell in first.findall("cell"):
                cell.set("role", "head")
        if retained_html_rows:
            parent = table.getparent()
            if parent is not None:
                parent.insert(parent.index(table), etree.Element("lb"))
    # XML pretty-print whitespace is not article text. Keep inline spacing.
    for element in main.iter():
        if element.tag in {"main", "p", "head", "list", "item", "table", "row", "cell", "lb"}:
            if element.text and not element.text.strip():
                element.text = None
            if element.tail and not element.tail.strip():
                element.tail = None
    # Unwrap targetless anchors, preserving both their children and following
    # text; an unknown span tag can cause the renderer to omit its tail.
    for ref in main.iter("ref"):
        if not ref.get("target"):
            ref.tag = "untargetedref"
    etree.strip_tags(main, "untargetedref")
    return xmltotxt(main, include_formatting=True).strip()


def main():
    """CLI: python news_extractor.py URL --out article.json"""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="URL'den haber başlığı ve metni çıkarır.")
    parser.add_argument("url", help="Haberin http/https adresi")
    parser.add_argument("--out", type=Path, help="UTF-8 JSON çıktı dosyası")
    args = parser.parse_args()
    result = fetch_article(args.url)
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())



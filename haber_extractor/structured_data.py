"""What a page declares about its own article, and the DOM blocks that print it.

News sites describe their articles for search engines (schema.org JSON-LD,
microdata, Open Graph). The vocabulary is the same on every site, so reading it
needs no per-site rule, and the declared body tells which blocks are the article.
"""
import html
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from lxml import etree

ARTICLE_TYPES = frozenset({
    "Article", "NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle",
    "BackgroundNewsArticle", "OpinionNewsArticle", "ReviewNewsArticle",
    "AskPublicNewsArticle", "BlogPosting", "LiveBlogPosting", "TechArticle",
    "ScholarlyArticle", "Report",
})
PART_KEYS = frozenset({"liveBlogUpdate", "hasPart"})   # where an article lists its own parts
INLINE = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "big", "br", "cite", "code", "data", "del", "dfn",
    "em", "font", "i", "img", "ins", "kbd", "label", "mark", "q", "s", "samp", "small",
    "span", "strike", "strong", "sub", "sup", "time", "tt", "u", "var", "wbr",
})
NOT_CONTENT = ("head", "script", "style", "noscript", "template", "svg", "iframe",
               "select", "textarea", "button")
MARKUP = {"b": "#b", "strong": "#b", "i": "#i", "em": "#i"}
MIN_BODY = 150   # compact characters; a shorter declaration cannot identify a body
ANCHOR = 40      # a block this long identifies its place in the body on its own
NEAR = 80        # how far a short block may sit from the matched text beside it
PROSE = 80       # an undeclared block this long reads as a sentence, not a label
PIECE = 20


def compact(text):
    """Letters and digits only, so spacing, punctuation and entities cannot differ."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text or "").lower() if ch.isalnum())


def _is_block(element):
    return isinstance(element.tag, str) and element.tag not in INLINE


def _text(element):
    return "".join(element.itertext())


def _visible(tree):
    """A private copy without scripts, form controls and comments."""
    tree = deepcopy(tree)
    etree.strip_elements(tree, *NOT_CONTENT, etree.Comment, with_tail=False)
    return tree


def _types(node):
    kind = node.get("@type")
    return set(kind if isinstance(kind, list) else [kind])


def _articles(tree):
    """Declared article nodes as (node, owner).

    `owner` is None for a declaration standing on its own, the outermost
    article for one of its parts (a live blog update), and False for an article
    another one merely cites: that describes some other page.
    """
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            pending = [(json.loads(script.text or "", strict=False), None)]
        except ValueError:
            continue
        while pending:
            node, owner = pending.pop(0)
            if isinstance(node, dict):
                found = bool(_types(node) & ARTICLE_TYPES)
                if found:
                    yield node, owner
                for key, value in node.items():
                    inner = owner
                    if found:
                        inner = False if owner is False or key not in PART_KEYS else owner or node
                    pending.append((value, inner))
            elif isinstance(node, list):
                pending.extend((value, owner) for value in node)


def _address(url):
    """A page's identity: scheme, www, a trailing slash and the fragment do not change it."""
    parts = urlsplit(url)
    return (parts.hostname or "").removeprefix("www."), parts.path.rstrip("/"), parts.query


def _own(nodes, url, pages, titles):
    """The declarations describing this page, not another story it shows or links to.

    A page may carry declarations for neighbouring articles (related stories,
    the next article of an endless scroll); taking them all would join
    different news items into one text.
    """
    def named(node):
        for key in ("mainEntityOfPage", "url", "@id"):
            value = node.get(key)
            if isinstance(value, dict):
                value = value.get("@id") or value.get("id") or value.get("url")
            if isinstance(value, str) and value.strip():
                yield _address(urljoin(url, value.strip()))

    def headline(node):
        return compact(_plain(node.get("headline")))

    free = [n for n in nodes if not set(named(n))]
    own = [n for n in nodes if set(named(n)) & pages] or _titled(free, list(map(headline, free)), titles)
    # The same story is often declared twice, e.g. as NewsArticle and LiveBlogPosting.
    heads = {headline(n) for n in own} - {""}
    return own + [n for n in free if headline(n) in heads and not any(n is o for o in own)]


def _titled(candidates, headlines, titles):
    """The candidate carrying the page's title, for declarations that name no address.

    `titles` lists the page's own titles by trust: its metadata, then its first
    heading. A lone candidate is taken on trust only when there is nothing to
    compare: one whose headline is not this page's title describes another
    story, however completely the page prints it.
    """
    for group in titles:
        for candidate, headline in zip(candidates, headlines):
            if headline and any(headline in title for title in group):
                return [candidate]
    return candidates if len(candidates) == 1 and not (headlines[0] and any(titles)) else []


def _marked(tree, url, pages, titles):
    """Microdata article bodies as (this page's text, texts of other stories).

    A body is marked inside an item (`itemscope`, failing that an <article>),
    which may mark it in several pieces. Items of other stories on the page are
    told apart as JSON-LD declarations are: by address, else by title. Taking
    the first or the longest would let a sidebar story stand in for the article.
    """
    items = {}
    for node in tree.xpath('//*[@itemprop="articleBody"]'):
        holder = next((a for a in node.iterancestors() if a.get("itemscope") is not None or a.tag == "article"), node)
        items.setdefault(holder, []).append(node)

    def named(holder):
        values = holder.xpath('@itemid | .//*[@itemprop="url" or @itemprop="mainEntityOfPage"]'
                              '/@*[name()="href" or name()="content" or name()="itemid"]')
        return {_address(urljoin(url, value.strip())) for value in values if value.strip()}

    def headline(holder):
        marked = holder.xpath('.//*[@itemprop="headline"]')
        if marked:
            return compact(marked[0].get("content") or _text(marked[0]))
        # A body marked with no item around it is introduced by the heading before it.
        inside = [] if holder in items[holder] else holder.xpath(".//h1 | .//h2 | .//h3")
        return "".join(compact(_text(h)) for h in (
            inside or holder.xpath("preceding::*[self::h1 or self::h2 or self::h3][1]"))[:1])

    own = [h for h in items if named(h) & pages] or _titled(list(items), list(map(headline, items)), titles)

    def text(holders):
        return [_text(_visible(node)) for holder in holders for node in items[holder]]

    return " ".join(text(own)), text([h for h in items if h not in own]) if own else []


def _plain(value):
    """Declared strings may carry escaped markup; keep only their text."""
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(value)))).strip() or None


def _instant(value):
    """UTC ISO time, or None when the site gives no usable zone."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None
    return moment.astimezone(timezone.utc).isoformat() if moment.tzinfo else None


def page_facts(tree, url):
    """What the page declares about itself."""
    def meta(*names):
        for name in names:
            found = tree.xpath('//meta[@property=$n or @name=$n]/@content', n=name)
            if found and found[0].strip():
                return found[0].strip()
        return None

    canonical = (tree.xpath('//link[@rel="canonical"]/@href') or [meta("og:url") or ""])[0].strip()
    canonical = urljoin(url, canonical) if canonical else None
    pages = {_address(page) for page in (url, canonical) if page}
    # The page's own titles, most trusted first: its metadata, then its first
    # <h1> (later ones head other stories, and so may the first).
    titles = [[compact(t) for t in found if compact(t)] for found in (
        [meta("og:title"), *tree.xpath("//title/text()")], map(_text, tree.xpath("(//h1)[1]")))]
    nodes = list(_articles(tree))
    own = _own([n for n, owner in nodes if owner is None], url, pages, titles)
    article = own[0] if own else {}
    declared = meta("og:type")
    summary = compact(_plain(article.get("description")) or meta("og:description", "description"))
    parts, foreign = [], []
    for node, owner in nodes:
        part = owner not in (None, False)
        body = _plain(node.get("articleBody"))
        if not any((owner if part else node) is o for o in own):
            foreign += [_plain(node.get("headline")), body]
        # Some sites repeat their summary as the body; that is not the article.
        elif body and body not in parts and not (summary and compact(body) in summary):
            parts += [_plain(node.get("headline")) or "", body] if part else [body]
    marked, others = _marked(tree, url, pages, titles)
    bodies = {"json-ld": " ".join(parts), "microdata": marked}
    source = next((k for k, v in bodies.items() if len(compact(v)) >= MIN_BODY), None)
    mine = compact(" ".join(bodies.values()))
    return {
        # What other stories on this page declare as theirs. Known only once the
        # page's own article is; a text this article declares too is not theirs.
        "foreign": [t for t in foreign + others if mine and t and compact(t) not in mine],
        # None: the page says nothing either way. Old or small sites often do not.
        "is_article": True if own or declared == "article" else False if declared else None,
        "headline": _plain(article.get("headline")) or meta("og:title"),
        "published_at": _instant(article.get("datePublished") or meta("article:published_time")),
        "modified_at": _instant(article.get("dateModified") or meta("article:modified_time")),
        "canonical_url": canonical,
        "body": bodies[source] if source else None,
        "body_source": source,
    }


def _wrap_loose_text(tree):
    """Give inline runs beside block children a paragraph, as a browser lays them out."""
    for parent in {e.getparent() for e in tree.iter() if _is_block(e)} - {None}:
        children = list(parent)
        runs = [etree.Element("p")]
        runs[0].text, parent.text = parent.text, None
        parent.insert(0, runs[0])
        for child in children:
            if _is_block(child):
                runs.append(etree.Element("p"))
                runs[-1].text, child.tail = child.tail, None
                child.addnext(runs[-1])
            else:
                runs[-1].append(child)
        for run in runs:
            if not _text(run).strip():
                parent.remove(run)


class Tape:
    """Body text as a tape: each block claims the part it prints."""

    def __init__(self, text):
        self.text = compact(text)
        self.used = bytearray(len(self.text))

    def _find(self, piece, start=0):
        at = self.text.find(piece, start)
        while at >= 0 and sum(self.used[at:at + len(piece)]) * 2 > len(piece):
            at = self.text.find(piece, at + 1)   # a repeated DOM block must not print twice
        return at

    def _mark(self, at, size):
        self.used[at:at + size] = b"\x01" * size
        return at, at + size

    def claim(self, block, start=0, reach=None):
        """Take the block's place on the tape; (start, end), or None when it has none.

        With `reach`, the text must begin within that distance after `start`.
        """
        at = self._find(block, start) if block else -1
        if at >= 0 and (reach is None or at - start <= reach):
            return self._mark(at, len(block))
        if reach is not None or len(block) < 2 * ANCHOR:
            return None
        # The block carries a little text the tape lacks (a photo credit):
        # accept it when most of its consecutive pieces are on the tape.
        pieces = [block[i:i + PIECE] for i in range(0, len(block) - PIECE + 1, PIECE)]
        found = [at for at in map(self._find, pieces) if at >= 0]
        if len(found) * 5 < len(pieces) * 3:
            return None
        for at in found:
            self._mark(at, PIECE)
        return min(found), max(found) + PIECE

    @property
    def coverage(self):
        return sum(self.used) / len(self.text) if self.text else 0.0


def _inline(source, tag, url):
    """Copy a block's inline content into trafilatura's XML vocabulary."""
    node = deepcopy(source)
    node.tail = None
    for element in list(node.iter()):
        # Source line breaks and indentation are not text, as in a browser.
        element.text = re.sub(r"\s+", " ", element.text) if element.text else None
        element.tail = re.sub(r"\s+", " ", element.tail) if element.tail else None
        if element is node:
            continue
        href = element.get("href", "").strip()
        rend = MARKUP.get(element.tag)
        block = _is_block(element)
        element.attrib.clear()
        if rend:
            element.tag = "hi"
            element.set("rend", rend)
        elif element.tag == "a" and href and not href.startswith(("#", "javascript:")):
            element.tag = "ref"
            element.set("target", urljoin(url, href))
        elif element.tag == "br":
            element.tag = "lb"
        else:
            if block:   # only inside table cells: keep nested blocks apart
                element.text, element.tail = " " + (element.text or ""), " " + (element.tail or "")
            element.tag = "unwrap"
    etree.strip_tags(node, "unwrap")
    node.attrib.clear()
    node.tag = tag
    return node


def _data_rows(table):
    """Rows of a data table; None for layout tables, whose cells are ordinary blocks."""
    if next(table.iterdescendants("table"), None) is not None:
        return None
    rows = [[c for c in row if c.tag in ("td", "th")] for row in table.iter("tr")]
    rows = [r for r in rows if r]
    if len(rows) < 2 or max(map(len, rows)) < 2:
        return None
    if any(len(compact(_text(c))) > 400 for r in rows for c in r):
        return None
    return rows


def _table(rows, url):
    table = etree.Element("table")
    width = max(map(len, rows))
    for cells in rows:
        row = etree.SubElement(table, "row")
        for cell in cells:
            copy = _inline(cell, "cell", url)
            if cell.tag == "th":
                copy.set("role", "head")
            row.append(copy)
        for _ in range(width - len(cells)):
            etree.SubElement(row, "cell")
    return table


def _units(tree):
    """Innermost blocks in page order as (element, table rows, text); a data table is one unit."""
    parents = {e.getparent() for e in tree.iter() if _is_block(e)}
    tables = {}
    for leaf in tree.iter():
        if not _is_block(leaf) or leaf in parents:
            continue
        table = next(leaf.iterancestors("table"), None)
        if table is not None:
            if table not in tables:
                tables[table] = _data_rows(table)
                if tables[table]:
                    yield table, tables[table], ""
            if tables[table]:
                continue
        text = compact(_text(leaf))
        if text:
            yield leaf, None, text


def _linked(element):
    """Characters of the block that sit inside links."""
    return len(compact("".join(_text(a) for a in element.iter("a"))))


def declared_body(tree, body, url, layout_text="", headline=None, foreign=()):
    """Render the DOM blocks that print the declared body, in page order.

    Returns (trafilatura XML body, coverage, undeclared). `coverage` is the
    share of the declaration found on the page: it says how complete the text
    is, not that nothing else is in it. `undeclared` is the share of the text
    the declaration does not contain. Headings, lists and tables keep their
    structure. `layout_text` is the layout-based extraction, a second witness:
    prose it selected that the declaration lacks (a lead, a live blog's
    opening) is kept, so a long sentence of page furniture can come with it.
    `foreign` is what other stories on the page declare; that is never kept.
    """
    tree = _visible(tree)
    _wrap_loose_text(tree)
    tape, witness, theirs = Tape(body), Tape(layout_text), Tape(" ".join(foreign))
    units = list(_units(tree))
    # Longest first: the paragraph outranks a teaser, breadcrumb or summary line
    # that merely repeats its opening words. Among equals, text outranks a link.
    spans = {}
    for index in sorted(range(len(units)), key=lambda i: (-len(units[i][2]), _linked(units[i][0]) == len(units[i][2]))):
        if len(units[index][2]) >= ANCHOR:
            span = tape.claim(units[index][2])
            if span:
                spans[index] = span
    flow = {units[i][0].getparent() for i in spans}
    following, ahead = {}, None
    for index in reversed(range(len(units))):
        following[index] = ahead
        ahead = spans[index][0] if index in spans else ahead
    title = compact(headline)
    # Where undeclared text may sit: between the page title and the end of the
    # declared body. A story placed after or beside the article is another one.
    opening = next((i for i, unit in enumerate(units) if title and unit[2] == title), -1)
    closing = max(spans, default=-1)
    main = etree.Element("body")
    cursor, open_list = 0, (None, None)
    printed = {True: 0, False: 0}   # characters by whether the declaration contains them
    for index, (element, rows, text) in enumerate(units):
        if rows:
            # Short cells cannot be placed one by one; judge the table as a whole.
            cells = [compact(_text(c)) for r in rows for c in r]
            size = sum(map(len, cells)) * 3
            holder = element.getparent()
            known = sum(len(c) for c in cells if c in tape.text) * 5 >= size
            if (known or sum(len(c) for c in cells if c in witness.text) * 5 >= size
                    or holder in flow or holder.getparent() in flow):
                for cell in cells:
                    span = tape.claim(cell, cursor, None if len(cell) >= 12 else NEAR)
                    cursor = span[1] if span else cursor
                main.append(_table(rows, url))
                printed[known] += size // 3
                open_list = (None, None)
            continue
        span = spans.get(index)
        if span is None and len(text) < ANCHOR:
            # A short block has no identity of its own: it belongs only where the
            # page order puts it, beside the text matched before or after it.
            before = following[index]
            span = tape.claim(text, cursor, NEAR) or (
                before is not None and tape.claim(text, max(0, before - NEAR - len(text)), NEAR))
        if span:
            cursor = span[1]
        elif (len(text) < PROSE or element.tag == "h1" or (title and text.find(title) > 0)
              or _linked(element) * 2 >= len(text)
              or next(element.iterancestors("aside", "nav", "footer"), None) is not None
              or not (opening < index < closing or element.getparent() in flow)
              or theirs.claim(text) or not witness.claim(text)):
            # Undeclared text needs the second witness, must read as prose (not
            # a label, a run of links or a breadcrumb ending in the title) and
            # must belong to the article: inside its stretch of the page, or a
            # sibling of declared paragraphs when the declaration stops early.
            # Text another story declares is that story's, wherever it sits.
            continue
        if text == title:
            continue   # the title is returned on its own, never inside the text
        printed[bool(span)] += len(text)
        if element.tag == "li":
            holder = element.getparent()
            if open_list[0] is not holder:
                open_list = (holder, etree.SubElement(main, "list", rend="ol" if holder.tag == "ol" else "ul"))
            open_list[1].append(_inline(element, "item", url))
            continue
        open_list = (None, None)
        if re.fullmatch(r"h[1-6]", element.tag):
            main.append(_inline(element, "head", url))
            main[-1].set("rend", element.tag)
        else:
            main.append(_inline(element, "quote" if element.tag == "blockquote" else "p", url))
    return main, tape.coverage, printed[False] / (sum(printed.values()) or 1)

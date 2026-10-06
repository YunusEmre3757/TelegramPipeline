"""Readable JSON snapshots of collected source texts."""
import json
import os
import re
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from .collector_runtime import process_lock
from haber_extractor.url_utils import normalize_url, parse_urls


@contextmanager
def export_lock(resource):
    """Serialize full read/write snapshots, including independent CLI processes."""
    deadline = time.monotonic() + 30
    while True:
        lock = process_lock(resource)
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _url_groups(urls, size=500):
    """SQL conditions selecting `urls` in bounded lists; a single empty one for every URL."""
    if urls is None:
        return [("", [])]
    urls = sorted(urls)
    return [(" AND url IN (" + ",".join("?" * len(part)) + ")", part)
            for part in (urls[i:i + size] for i in range(0, max(len(urls), 1), size))]


def _article_lookup(db, urls=None):
    """articles tablosu varsa URL→{title, text} sözlüğü döndürür; `urls` verilirse yalnızca onlar okunur."""
    def table(name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()

    if not table("articles"):
        return {}
    versions, details = table("article_versions"), table("article_extractions")
    result, extractions = {}, {}
    for scope, values in _url_groups(urls):
        for url, title, text in db.execute(
            "SELECT url, title, text FROM articles WHERE text IS NOT NULL" + scope, values
        ):
            result[normalize_url(url)] = {"title": title, "text": text}
        if versions:
            for url, vid, title, text, final, kind, fetched in db.execute(
                "SELECT url,version_id,title,text,final_url,content_type,fetched_at "
                "FROM article_versions WHERE url IS NOT NULL" + scope + " ORDER BY fetched_at,rowid", values
            ):
                article = result.get(normalize_url(url))
                if article is not None:
                    article.setdefault("versions", []).append({"version_id": vid, "title": title,
                        "text": text, "final_url": final, "content_type": kind, "fetched_at": fetched})
            for url, final, checked, status, error in db.execute(
                "SELECT url,final_url,last_checked_at,status,error FROM articles WHERE text IS NOT NULL" + scope, values
            ):
                result[normalize_url(url)].update(final_url=final,
                    last_checked_at=checked, last_check_status=status, last_check_error=error)
        if details:
            extractions.update(((normalize_url(url), vid), json.loads(payload)) for url, vid, payload in db.execute(
                "SELECT url,version_id,details FROM article_extractions WHERE url IS NOT NULL" + scope, values))
    for key, article in result.items():
        stored_versions = article.get("versions", [])
        if not stored_versions:
            # Legacy databases may not have article_versions yet.
            import hashlib
            vid = hashlib.sha256(json.dumps([article["title"], article["text"]],
                                          ensure_ascii=False).encode()).hexdigest()
            stored_versions = [{"version_id": vid, "title": article["title"],
                                "text": article["text"]}]
        matching = [v for v in stored_versions
                    if v["text"] == article["text"] and v["title"] == article["title"]]
        current = (matching or stored_versions)[-1]
        technical = {'final_url', 'content_type'}
        compact = {k: v for k, v in current.items() if v is not None and k not in technical}
        history = [{k: value for k, value in v.items() if value is not None and k not in technical}
                   for v in stored_versions if v['version_id'] != current['version_id']]
        if history:
            compact['history'] = history
        # A successful unchanged check has no new content version, so its time
        # still matters. Omit the check only when it duplicates the fetch.
        checked = article.get('last_checked_at')
        status = article.get('last_check_status')
        if status and (status != 'ok' or checked != current.get('fetched_at')):
            compact['last_check'] = {'status': status, 'at': checked}
            if article.get('last_check_error'):
                compact['last_check']['error'] = article['last_check_error']
        compact.update(_extraction_fields(extractions.get((key, current['version_id']), {}), key))
        result[key] = compact
    return result


def _extraction_fields(details, url):
    """How the text was established and what its page declares; absent facts are omitted."""
    fields = {}
    if details.get('method'):
        # "declared": read from the body the page names for itself; `coverage` of
        # it was found and `undeclared` of the text is not in it. "layout": the
        # heuristic alone; nothing on the page confirms it.
        fields['extraction'] = {k: details[k] for k in ('method', 'coverage', 'undeclared') if k in details}
    if details.get('is_article') is False:
        fields['not_article'] = True
    for name in ('published_at', 'modified_at'):
        if details.get(name):
            fields['page_' + name] = details[name]
    canonical = details.get('canonical_url')
    if canonical and normalize_url(canonical) != url:
        fields['canonical_url'] = canonical
    return fields


def _for_message(article, published):
    """Flag a page its site changed after the Telegram post: the text may differ from what the post linked to."""
    try:
        late = datetime.fromisoformat(article['page_modified_at']) > datetime.fromisoformat(published)
    except (KeyError, TypeError, ValueError):
        return article
    return {**article, 'modified_after_message': True} if late else article


def export_json(db, channel_ids, out, year=None):
    """One first-observed text version per source message, without rule filtering.

    With `year`, only the messages published in that year (UTC).
    """
    target = Path(out).resolve()
    with export_lock(str(target) + ".export"):
        # All SELECTs see the same database snapshot. Preserve caller transactions.
        db.execute("SAVEPOINT news_export")
        try:
            return _export_json(db, channel_ids, target, year)
        finally:
            db.execute("RELEASE news_export")


def _export_json(db, channel_ids, out, year=None):
    chosen, selected = "m.channel_id IN (" + ','.join('?' for _ in channel_ids) + ")", sorted(channel_ids)
    if year is not None:
        chosen += " AND m.date_utc >= ? AND m.date_utc < ?"
        selected += [f"{year:04d}", f"{year + 1:04d}"]
    # Only what these messages need is read: a yearly file must not cost the whole archive.
    versions, linked_urls = {}, set()
    for cid, mid, vid, observed, source, edited, text, payload in db.execute(
        "SELECT v.channel_id,v.message_id,v.version_id,v.observed_at,v.source,v.edit_date_utc,v.text,v.snapshot_json "
        "FROM message_versions v JOIN messages m ON m.channel_id=v.channel_id AND m.message_id=v.message_id "
        "WHERE " + chosen + " ORDER BY v.observed_at,v.version_id", selected
    ):
        urls = parse_urls(json.loads(payload).get("urls"))
        linked_urls.update(filter(None, map(normalize_url, urls)))
        version = {"version_id": vid, "text": text, "urls": urls,
                   "observed_at": observed, "observation_source": source}
        if edited:
            version['edited_at'] = edited
        versions.setdefault((cid, mid), []).append(version)
    articles = _article_lookup(db, linked_urls)
    query = """SELECT m.channel_id,m.message_id,c.title,m.text,m.date_utc,
                      m.edit_date_utc,m.collected_at,m.urls,v.version_id,v.source
               FROM messages m LEFT JOIN channels c USING(channel_id)
               LEFT JOIN message_versions v ON v.channel_id=m.channel_id
                 AND v.message_id=m.message_id AND v.version_id=(
                   SELECT v2.version_id FROM message_versions v2
                   WHERE v2.channel_id=m.channel_id AND v2.message_id=m.message_id
                     AND v2.text IS m.text AND v2.edit_date_utc IS m.edit_date_utc
                   ORDER BY v2.observed_at,v2.version_id LIMIT 1)
               WHERE trim(m.text)!='' AND """ + chosen + " ORDER BY m.date_utc,m.channel_id,m.message_id"
    target = Path(out).resolve()
    for _, _, db_file in db.execute("PRAGMA database_list"):
        if db_file and target == Path(db_file).resolve():
            raise ValueError("JSON çıktısı veritabanının üstüne yazılamaz")
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=target.name + ".", suffix=".tmp", delete=False) as f:
            name = f.name
            f.write('{"schema_version": "3.2", "messages": [\n')
            for cid, mid, title, text, published, edited, observed, urls, vid, source in db.execute(query, selected):
                if not vid:
                    raise ValueError(f"Sürüm kimliği eksik: {cid}:{mid}")
                url_list = json.loads(urls or "[]")
                message_versions = versions.get((cid, mid), [])
                # Each record carries its own articles; a URL shared by several
                # messages is repeated so a record can be read on its own.
                linked = sorted({key for version in message_versions
                                 for key in _article_ids(version['urls'], articles)})
                record = {
                    "record_id": f"{cid}:{mid}",
                    "source": {"channel_id": cid, "message_id": mid,
                               "channel_title": title},
                    "first_version_id": vid,
                    "message_versions": message_versions,
                    "published_at": published,
                    "publication_text_uncertain": source != "live_new" or bool(edited),
                    "articles": {key: _for_message(articles[key], published) for key in linked},
                }
                if count:
                    f.write(',\n')
                f.write('\n'.join('  ' + line for line in json.dumps(record, ensure_ascii=False, indent=2).splitlines()))
                count += 1
            f.write('\n]}\n')
        os.replace(name, target)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return count


def _article_ids(urls, articles):
    return sorted({normalize_url(url) for url in urls if normalize_url(url) in articles})


def source_files(db, channel_ids):
    """(channel_id, year) of every source file: each publication year (UTC) in which a channel has texts."""
    marks = ",".join("?" for _ in channel_ids)
    return {(cid, int(year)) for cid, year in db.execute(
        "SELECT DISTINCT channel_id, substr(date_utc,1,4) FROM messages "
        f"WHERE trim(text)!='' AND channel_id IN ({marks})", sorted(channel_ids))}


def export_sources(db, channel_ids, directory, only=None):
    """One independent, atomic JSON file per channel and publication year (UTC).

    `only` limits the work to those (channel_id, year) files and leaves the
    others as they are. A channel without texts has no file.
    """
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    counts = {}
    files = source_files(db, channel_ids) if only is None else {f for f in only if f[0] in channel_ids}
    for cid, year in sorted(files):
        with export_lock(root / f".channel_{cid}.export"):
            row = db.execute("SELECT username FROM channels WHERE channel_id=?", (cid,)).fetchone()
            slug = re.sub(r'[^a-z0-9_-]+', '_', (row[0] or 'channel').lower()) if row else 'channel'
            # Keep the filename stable when a channel changes its username.
            existing = sorted(root.glob(f'*_{cid}_{year}.json'))
            path = existing[0] if existing else root / f'{slug}_{cid}_{year}.json'
            counts[path.name] = export_json(db, {cid}, path, year)
            # The one file per channel written before the yearly split would repeat these records.
            for old in root.glob(f'*_{cid}.json'):
                for leftover in (old, Path(str(old) + '.export.lock')):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
    return counts

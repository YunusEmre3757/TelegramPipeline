"""URL normalization shared by Telegram discovery, fetching and export."""
import re
import json
from urllib.parse import urlsplit, urlunsplit


def parse_urls(value):
    """Legacy snapshots store the URL array as an embedded JSON string."""
    return json.loads(value or "[]") if isinstance(value, str) else (value or [])


def normalize_url(value, prose=False):
    url = value.strip()
    if prose:
        url = url.rstrip(".,;:!?\"'»”’")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1].rstrip(".,;:!?\"'»”’")
    if url.startswith("t.me/"):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username or parts.password or re.search(r"[\s\x00-\x1f]", url):
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parts.port
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        # Query parameters may select different articles: preserve them.
        return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ""))
    except (ValueError, UnicodeError):
        return None

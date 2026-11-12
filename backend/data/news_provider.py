"""Live news from free RSS feeds (no API key).

* Google News RSS search – headlines from many publishers, one query per gold driver
* Federal Reserve press releases – official RSS

Only headline, source, time, a short summary and the link are kept. Links open
the original article (Google News links redirect to the publisher).
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote_plus

import requests

from backend.data.base import DataUnavailableError

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
FED_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"

# one search per gold driver from the project brief
QUERIES: dict[str, str] = {
    "Gold market": "gold price OR XAUUSD OR \"spot gold\" when:2d",
    "Federal Reserve": "\"Federal Reserve\" OR FOMC OR Powell rates when:2d",
    "US inflation": "US CPI OR PCE inflation when:3d",
    "US jobs": "nonfarm payrolls OR \"jobless claims\" OR \"unemployment rate\" US when:3d",
    "Dollar & yields": "\"dollar index\" OR DXY OR \"Treasury yields\" when:2d",
    "Central banks & ETFs": "\"central bank\" gold buying OR \"gold ETF\" when:7d",
    "Geopolitics": "geopolitical tensions safe haven gold when:2d",
    "China": "China economy gold OR PBOC when:3d",
}
FAST_FEEDS = {"Gold market", "Federal Reserve", "Federal Reserve (official)"}
FAST_TTL = 30             # seconds – the feeds that move gold fastest
SLOW_TTL = 120            # seconds – the other topics (keeps total requests polite: ~9 per minute)
SUMMARY_MAX = 260

HttpText = Callable[[str], "str | bytes"]


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published_utc: datetime
    link: str
    summary: str
    feed: str           # which query / feed found it


def _http_text_factory(timeout: float = 15.0) -> HttpText:
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (GoldPulseAI personal dashboard)"})

    def get(url: str) -> str:
        try:
            r = sess.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise DataUnavailableError(f"News feed unreachable ({type(exc).__name__}).") from None
        if r.status_code != 200:
            raise DataUnavailableError(f"News feed returned HTTP {r.status_code}.")
        return r.content

    return get


_TAG = re.compile(r"<[^>]+>")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    t = html.unescape(_TAG.sub(" ", html.unescape(text)))
    return re.sub(r"\s+", " ", t).strip()


def parse_rss(xml_text: "str | bytes", feed: str, default_source: str | None = None) -> list[NewsItem]:
    raw = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
    raw = raw.lstrip(b"\xef\xbb\xbf").lstrip()            # byte-order mark / leading blank lines
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        head = raw[:200].lower()
        if b"<html" in head or b"<!doctype html" in head:
            raise DataUnavailableError(f"{feed}: the site returned a web page instead of a feed (possibly blocking automated requests).") from None
        raise DataUnavailableError(f"{feed}: feed is not valid RSS/XML ({exc}).") from None
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag == "html":
        raise DataUnavailableError(f"{feed}: the site returned a web page instead of a feed (possibly blocking automated requests).")
    if tag not in ("rss", "rdf", "feed"):
        raise DataUnavailableError(f"{feed}: response is XML but not an RSS feed (<{tag}>).")
    items = []
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if not title or not link or not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        src_el = it.find("source")
        source = _clean(src_el.text) if src_el is not None and src_el.text else (default_source or "")
        # Google News titles end with " - Publisher"; keep the headline clean
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        summary = _clean(it.findtext("description"))
        if summary.startswith(title):                # Google News descriptions repeat the title
            summary = ""
        if source and summary.endswith(source):
            summary = summary[: -len(source)].strip()
        items.append(NewsItem(title, source or "Unknown", dt.astimezone(timezone.utc), link,
                              summary[:SUMMARY_MAX], feed))
    return items


class NewsProvider:
    """Reads each feed on its own schedule and keeps the last good copy if a feed fails."""

    def __init__(self, http_text: HttpText | None = None, clock: Callable[[], float] = time.time):
        self._get = http_text or _http_text_factory()
        self._clock = clock
        self._lock = threading.Lock()
        self._feeds: dict[str, dict] = {}      # name -> {"fetched": t, "items": [...], "error": str|None}

    def _feed_list(self) -> list[tuple[str, str, str | None]]:
        out = [(name, GOOGLE_NEWS.format(q=quote_plus(q)), None) for name, q in QUERIES.items()]
        out.append(("Federal Reserve (official)", FED_RSS, "Federal Reserve"))
        return out

    def fetch(self) -> tuple[list[NewsItem], dict[str, str]]:
        """Return (items, problems-by-feed). Raises only if no feed has ever worked."""
        with self._lock:
            now = self._clock()
            for name, url, default_src in self._feed_list():
                ttl = FAST_TTL if name in FAST_FEEDS else SLOW_TTL
                st = self._feeds.get(name)
                if st and now - st["fetched"] < ttl:
                    continue
                try:
                    items = parse_rss(self._get(url), name, default_src)
                    self._feeds[name] = {"fetched": now, "items": items, "error": None, "ok_at": now}
                except DataUnavailableError as exc:
                    prev = st or {"items": [], "ok_at": None}
                    self._feeds[name] = {"fetched": now, "items": prev["items"], "error": str(exc), "ok_at": prev.get("ok_at")}
            items, errors = [], {}
            for name, st in self._feeds.items():
                items += st["items"]
                if st["error"]:
                    errors[name] = st["error"] + (" – showing last good copy" if st["items"] else "")
            if not items:
                raise DataUnavailableError("No news feed could be reached: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
            return items, errors

    def schedule(self) -> dict:
        return {"fast_feeds": sorted(FAST_FEEDS), "fast_seconds": FAST_TTL, "other_seconds": SLOW_TTL}

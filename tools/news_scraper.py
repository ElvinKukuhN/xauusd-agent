"""Tool berita makro: mengambil headline terbaru dari RSS finansial (emas, USD, komoditas)."""

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

import config

logger = logging.getLogger(__name__)

MAX_NEWS = 8
SUMMARY_MAX_CHARS = 280

RSS_FEEDS = [
    ("Investing.com Commodities", "https://www.investing.com/rss/news_11.rss"),
    ("Investing.com Forex", "https://www.investing.com/rss/news_1.rss"),
    ("ForexLive", "https://www.forexlive.com/feed/news"),
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _clean(text) -> str:
    """Buang tag HTML, decode entity, dan rapikan spasi."""
    if not text:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", text))
    return _SPACE_RE.sub(" ", text).strip()


def _parse_date(raw) -> datetime:
    """Parse tanggal RSS (RFC 822 atau 'YYYY-MM-DD HH:MM:SS') menjadi datetime UTC."""
    if not raw:
        return _EPOCH
    raw = raw.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return _EPOCH
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_feed(source: str, url: str) -> list:
    """Ambil dan parse satu RSS feed. Mengembalikan list kosong jika gagal."""
    try:
        response = requests.get(url, headers=_HEADERS, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except requests.exceptions.RequestException as exc:
        logger.warning("RSS %s gagal diambil: %s", source, exc)
        return []
    except ET.ParseError as exc:
        logger.warning("RSS %s gagal di-parse: %s", source, exc)
        return []

    items = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        summary = _clean(item.findtext("description"))
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "..."
        items.append({
            "source": source,
            "title": title,
            "summary": summary,
            "link": (item.findtext("link") or "").strip(),
            "_dt": _parse_date(item.findtext("pubDate")),
        })
    logger.info("RSS %s: %d item", source, len(items))
    return items


def get_macro_news() -> list:
    """Mengambil 8 berita makroekonomi/finansial terbaru yang relevan untuk emas dan USD.

    Gunakan tool ini untuk menilai sentimen fundamental XAU_USD: kebijakan The
    Fed, inflasi, data tenaga kerja AS, yield obligasi, pergerakan dolar AS,
    geopolitik, dan komoditas. Sumber: RSS Investing.com (commodities & forex)
    dan ForexLive.

    Returns:
        List berisi maksimal 8 dictionary, diurutkan dari yang paling baru.
        Setiap item berisi: source, title, summary, link, published_utc.
        Jika semua sumber gagal, list berisi satu dictionary dengan field error.
    """
    per_feed = []
    for source, url in RSS_FEEDS:
        items = _fetch_feed(source, url)
        if items:
            per_feed.append(sorted(items, key=lambda x: x["_dt"], reverse=True))

    if not per_feed:
        logger.error("Semua RSS feed gagal diambil.")
        return [{"error": "Semua sumber berita RSS gagal diambil. Sentimen berita tidak tersedia."}]

    # Ambil bergiliran dari tiap sumber (terbaru dulu) supaya satu feed yang
    # sangat aktif tidak memenuhi seluruh slot berita.
    seen = set()
    selected = []
    for rank in range(max(len(items) for items in per_feed)):
        for items in per_feed:
            if rank >= len(items) or len(selected) >= MAX_NEWS:
                continue
            key = items[rank]["title"].lower()
            if key not in seen:
                seen.add(key)
                selected.append(items[rank])
        if len(selected) >= MAX_NEWS:
            break
    selected.sort(key=lambda x: x["_dt"], reverse=True)

    news = []
    for item in selected:
        dt = item.pop("_dt")
        item["published_utc"] = dt.strftime("%Y-%m-%d %H:%M UTC") if dt != _EPOCH else "unknown"
        news.append(item)
    return news

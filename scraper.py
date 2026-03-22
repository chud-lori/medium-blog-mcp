"""
scraper.py — Fetches Lori's Medium blog via sitemap + RSS feed.

Data sources:
  • Sitemap  (https://chud-lori.medium.com/sitemap/sitemap.xml)
      → Complete list of ALL published articles (57+), no bot detection.
  • RSS feed (https://chud-lori.medium.com/feed)
      → Full article HTML for the 10 most recent posts, no bot detection.
      Medium caps the RSS feed at 10 items regardless of how many articles exist.

Why not GraphQL / article pages?
  Cloudflare blocks all POST requests and individual article-page GET requests
  from headless browsers. The sitemap and RSS endpoints are plain HTTP/XML and
  are not gated by Cloudflare.

Cache:  data/scrape_cache.json
  post_list  → 6-hour TTL
  rss_feed   → 6-hour TTL
  post:<url> → 7-day TTL
"""

import json
import sys
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

MEDIUM_PROFILE = "https://chud-lori.medium.com/"
MEDIUM_FEED    = "https://chud-lori.medium.com/feed"
MEDIUM_SITEMAP = "https://chud-lori.medium.com/sitemap/sitemap.xml"

CACHE_DIR  = Path(__file__).parent / "data"
CACHE_FILE = CACHE_DIR / "scrape_cache.json"
CACHE_TTL_LIST = 3600 * 6        # 6 hours
CACHE_TTL_POST = 3600 * 24 * 7   # 7 days

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Non-article pages that appear in the sitemap but should be excluded
_SITEMAP_SKIP = {"", "/", "/about"}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_cache(cache: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ── HTML → plain text ─────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    """Convert article HTML to clean plain text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    parts = []
    for el in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6",
         "p", "li", "blockquote", "pre", "figcaption"]
    ):
        text = el.get_text(strip=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


# ── Sitemap fetching ──────────────────────────────────────────────────────────

def _fetch_sitemap() -> list[dict]:
    """
    Fetch the sitemap and return a list of article entries.

    Each entry: {url, lastmod}

    The sitemap contains ALL published articles, unlike the RSS which is
    capped at 10 items. Non-article pages (homepage, /about) are filtered out.
    """
    with httpx.Client(headers=_HTTP_HEADERS, verify=False, timeout=20) as client:
        resp = client.get(MEDIUM_SITEMAP)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns   = _SITEMAP_NS
    entries = []
    for el in root.findall(f"{{{ns}}}url"):
        loc_el  = el.find(f"{{{ns}}}loc")
        mod_el  = el.find(f"{{{ns}}}lastmod")
        if loc_el is None:
            continue
        url = loc_el.text.strip()
        # Extract path relative to profile root
        path = url.replace(MEDIUM_PROFILE.rstrip("/"), "").rstrip("/")
        if path in _SITEMAP_SKIP:
            continue
        # Keep only paths that look like article slugs (contain a hex hash at end)
        import re
        if not re.search(r"[a-f0-9]{8,12}$", path):
            continue
        lastmod = mod_el.text.strip() if mod_el is not None else ""
        entries.append({"url": url, "lastmod": lastmod})

    return entries


# ── RSS fetching ──────────────────────────────────────────────────────────────

def _fetch_rss() -> list[dict]:
    """
    Fetch the RSS feed and return up to 10 recent articles with full content.

    Each entry: {title, url, content, pub_date}
    """
    with httpx.Client(headers=_HTTP_HEADERS, verify=False, timeout=20) as client:
        resp = client.get(MEDIUM_FEED)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns   = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "dc":      "http://purl.org/dc/elements/1.1/",
    }
    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        enc_el   = item.find("content:encoded", ns)
        pub_el   = item.find("pubDate")

        title    = title_el.text.strip() if title_el is not None else ""
        raw_url  = link_el.text.strip()  if link_el  is not None else ""
        url      = raw_url.split("?")[0]
        html     = (enc_el.text or "") if enc_el is not None else ""
        pub_date = pub_el.text.strip()   if pub_el   is not None else ""

        content = _html_to_text(html)
        if title and url and len(content) > 50:
            items.append({
                "title":    title,
                "url":      url,
                "content":  content,
                "pub_date": pub_date,
            })
    return items


# ── MediumScraper ─────────────────────────────────────────────────────────────

class MediumScraper:
    """
    Synchronous scraper that merges sitemap (all URLs) with RSS (recent content).

    Article list  →  sitemap (complete) enriched with RSS titles/dates.
    Article content  →  RSS cache (recent 10) or "unavailable" for older posts.
    """

    def __init__(self):
        self._cache = _load_cache()

    def _get_rss(self, force: bool = False) -> list[dict]:
        """Return cached RSS feed, refreshing if stale or forced."""
        key = "rss_feed"
        cached = self._cache.get(key)
        if not force and cached and time.time() - cached["ts"] < CACHE_TTL_LIST:
            return cached["data"]
        sys.stderr.write("🔄 Fetching RSS feed…\n")
        data = _fetch_rss()
        self._cache[key] = {"ts": time.time(), "data": data}
        _save_cache(self._cache)
        sys.stderr.write(f"   RSS: {len(data)} articles\n")
        return data

    def _get_sitemap(self, force: bool = False) -> list[dict]:
        """Return cached sitemap entries, refreshing if stale or forced."""
        key = "sitemap"
        cached = self._cache.get(key)
        if not force and cached and time.time() - cached["ts"] < CACHE_TTL_LIST:
            return cached["data"]
        sys.stderr.write("🔄 Fetching sitemap…\n")
        data = _fetch_sitemap()
        self._cache[key] = {"ts": time.time(), "data": data}
        _save_cache(self._cache)
        sys.stderr.write(f"   Sitemap: {len(data)} articles\n")
        return data

    # ── Public API ────────────────────────────────────────────────────────────

    def get_post_list(self) -> list[dict]:
        """
        Return the complete list of articles, sorted newest-first.

        Each entry:
          title     (str)  — from RSS if available, else derived from URL slug
          url       (str)  — canonical URL without tracking params
          pub_date  (str)  — ISO date from sitemap lastmod, or pub date from RSS
          in_rss    (bool) — whether full content is available via RSS
          lastmod   (str)  — last modification date from sitemap

        Sitemap provides the authoritative URL list (all articles).
        RSS enriches the 10 most recent entries with proper titles and dates.
        """
        key = "post_list"
        cached = self._cache.get(key)
        if cached and time.time() - cached["ts"] < CACHE_TTL_LIST:
            sys.stderr.write("📦 Using cached post list\n")
            return cached["data"]

        rss_posts   = self._get_rss()
        sitemap_urls = self._get_sitemap()

        # Build lookup: URL → RSS entry
        rss_by_url = {p["url"]: p for p in rss_posts}

        merged: list[dict] = []
        for entry in sitemap_urls:
            url     = entry["url"]
            lastmod = entry.get("lastmod", "")
            rss     = rss_by_url.get(url)
            if rss:
                merged.append({
                    "title":    rss["title"],
                    "url":      url,
                    "pub_date": rss["pub_date"],
                    "lastmod":  lastmod,
                    "in_rss":   True,
                })
            else:
                # Derive a human-readable title from the URL slug
                slug  = url.rstrip("/").rsplit("/", 1)[-1]
                # Remove trailing hex hash and dashes → readable title
                import re
                slug_clean = re.sub(r"-[a-f0-9]{8,12}$", "", slug)
                title = slug_clean.replace("-", " ").title()
                merged.append({
                    "title":    title,
                    "url":      url,
                    "pub_date": lastmod,
                    "lastmod":  lastmod,
                    "in_rss":   False,
                })

        # Sort newest-first by lastmod date
        merged.sort(key=lambda x: x.get("lastmod", ""), reverse=True)

        self._cache[key] = {"ts": time.time(), "data": merged}
        _save_cache(self._cache)
        sys.stderr.write(f"✅ Post list: {len(merged)} total ({len(rss_by_url)} with content)\n")
        return merged

    def get_post(self, url: str) -> Optional[dict]:
        """
        Return {title, url, content} for *url*.

        Priority:
          1. Per-post cache (7-day TTL)
          2. RSS feed (for the 10 most recent articles)
          3. Unavailable — older articles are blocked by Cloudflare and cannot
             be retrieved without a real logged-in browser session.
        """
        clean_url = url.split("?")[0]
        key       = f"post:{clean_url}"

        # 1. Per-post cache
        cached = self._cache.get(key)
        if cached and time.time() - cached["ts"] < CACHE_TTL_POST:
            sys.stderr.write(f"📦 Using cached post: {clean_url}\n")
            return cached["data"]

        # 2. RSS content
        rss_posts = self._get_rss()
        for p in rss_posts:
            if p["url"] == clean_url:
                result = {"title": p["title"], "url": p["url"], "content": p["content"]}
                self._cache[key] = {"ts": time.time(), "data": result}
                _save_cache(self._cache)
                sys.stderr.write(f"✅ Served from RSS: {p['title']}\n")
                return result

        # 3. Not available
        sys.stderr.write(
            f"❌ Content unavailable: {clean_url}\n"
            "   Reason: article is older than RSS window (10 most recent) and\n"
            "   direct article page access is blocked by Medium/Cloudflare.\n"
        )
        return None

    def invalidate_cache(self):
        """Force next call to re-fetch from Medium."""
        for k in ("post_list", "rss_feed", "sitemap"):
            self._cache.pop(k, None)
        _save_cache(self._cache)


# ── Module-level singleton ────────────────────────────────────────────────────

_scraper: Optional[MediumScraper] = None


def get_scraper() -> MediumScraper:
    global _scraper
    if _scraper is None:
        _scraper = MediumScraper()
    return _scraper

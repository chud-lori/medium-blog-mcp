"""
scraper.py — Fetches Lori's Medium blog via RSS + Playwright profile scrape.

Data sources (in priority order):
  1. RSS feed (https://chud-lori.medium.com/feed)  — full article HTML, no bot detection.
  2. Profile page via Playwright — catches any articles absent from RSS (member-only, etc.).
  3. Playwright article fetch — fallback for non-RSS articles (often blocked by Cloudflare).

Results are cached in data/scrape_cache.json to avoid hammering Medium.
  - Post list: 6-hour TTL
  - Individual posts: 7-day TTL
"""

import json
import random
import sys
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

MEDIUM_PROFILE = "https://chud-lori.medium.com/"
MEDIUM_FEED = "https://chud-lori.medium.com/feed"
CACHE_DIR = Path(__file__).parent / "data"
CACHE_FILE = CACHE_DIR / "scrape_cache.json"
CACHE_TTL_LIST = 3600 * 6        # 6 hours for post list
CACHE_TTL_POST = 3600 * 24 * 7   # 7 days for individual posts

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
    ]
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
"""

# JS to extract post links + titles from the Medium profile page
_PROFILE_EXTRACT_JS = """
    () => {
        const seen = new Set();
        const out  = [];
        document.querySelectorAll('a[href]').forEach(a => {
            const clean = a.href.split('?')[0];
            if (seen.has(clean)) return;
            if (!clean.includes('medium.com')) return;
            if (!/[a-f0-9]{8,12}$/.test(clean)) return;
            if (/\\/(tag|membership|about|plans|me|settings|followers|activity|signin|bookmark)/.test(clean)) return;
            let title = '';
            const container = a.closest('article') || a.parentElement;
            if (container) {
                const h = container.querySelector('h2, h3, h4, [data-testid="post-preview-title"]');
                if (h) title = h.textContent.trim();
            }
            if (!title) {
                const h = a.querySelector('h2, h3, h4');
                if (h) title = h.textContent.trim();
            }
            if (title) {
                seen.add(clean);
                out.push({ title, url: clean });
            }
        });
        return out;
    }
"""


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


# ── RSS fetching ──────────────────────────────────────────────────────────────

def _fetch_rss() -> list[dict]:
    """Fetch RSS feed and return list of {title, url, content, pub_date}."""
    with httpx.Client(headers=_HTTP_HEADERS, verify=False, timeout=20) as client:
        resp = client.get(MEDIUM_FEED)
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "dc": "http://purl.org/dc/elements/1.1/",
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
                "in_rss":   True,
            })
    return items


# ── Profile-page scraping (Playwright sync API) ───────────────────────────────

def _scrape_profile_page() -> list[dict]:
    """Scrape the profile page and return [{title, url}] for every visible article."""
    from playwright.sync_api import sync_playwright

    posts: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": random.randint(1200, 1440), "height": random.randint(768, 900)},
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()

        try:
            page.goto(MEDIUM_PROFILE, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(random.randint(2000, 3500))

            # Dismiss popups
            for sel in ['button[data-testid="close-button"]',
                        'button:text("Got it")', 'button:text("Accept")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=400):
                        btn.click()
                        page.wait_for_timeout(400)
                except Exception:
                    pass

            # Evaluate posts before any scroll (scroll causes Medium 500 errors)
            posts = page.evaluate(_PROFILE_EXTRACT_JS)
        finally:
            browser.close()

    return posts


# ── Playwright article fetch (fallback for non-RSS content) ───────────────────

def _playwright_get_post(url: str) -> Optional[dict]:
    """Attempt to scrape a single article via Playwright. Returns None if blocked."""
    from playwright.sync_api import sync_playwright

    result = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            timezone_id="America/New_York",
        )
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(random.randint(2500, 4000))
            result = page.evaluate("""
                () => {
                    const titleEl = document.querySelector('h1');
                    const title   = titleEl ? titleEl.textContent.trim() : document.title;
                    const article = document.querySelector('article');
                    if (!article) return null;
                    const blocks = [];
                    article.querySelectorAll(
                        'h1,h2,h3,h4,h5,h6,p,li,blockquote,pre,figcaption'
                    ).forEach(el => {
                        const t = el.textContent.trim();
                        if (t) blocks.push(t);
                    });
                    const content = blocks.join('\\n\\n');
                    if (content.length < 100) return null;
                    return { title, url: window.location.href, content };
                }
            """)
        except Exception as exc:
            sys.stderr.write(f"❌ Playwright fetch failed for {url}: {exc}\n")
        finally:
            browser.close()

    return result


# ── MediumScraper ─────────────────────────────────────────────────────────────

class MediumScraper:
    """
    Provides get_post_list() and get_post() as synchronous methods.

    List strategy: merge RSS (9 posts) + profile page (may have extras) so that
    member-only articles absent from the feed are still listed.
    Read strategy: RSS cache → scrape_cache.json → Playwright fallback.
    """

    def __init__(self):
        self._cache = _load_cache()

    # ── Post list ─────────────────────────────────────────────────────────────

    def get_post_list(self) -> list[dict]:
        """
        Return merged list of all articles.

        Each entry: {title, url, pub_date, in_rss}
          in_rss=False means the article exists on the profile page
          but was absent from the RSS feed (likely member-only/paywalled).
        """
        key = "post_list"
        cached = self._cache.get(key)
        if cached and time.time() - cached["ts"] < CACHE_TTL_LIST:
            sys.stderr.write("📦 Returning cached post list\n")
            return cached["data"]

        # 1. Fetch RSS articles
        sys.stderr.write("🔄 Fetching RSS feed…\n")
        rss_posts = _fetch_rss()
        rss_urls = {p["url"] for p in rss_posts}
        sys.stderr.write(f"   RSS: {len(rss_posts)} articles\n")

        # 2. Scrape profile page to catch any extras
        sys.stderr.write("🌐 Scraping profile page…\n")
        try:
            profile_posts = _scrape_profile_page()
            sys.stderr.write(f"   Profile: {len(profile_posts)} articles\n")
        except Exception as exc:
            sys.stderr.write(f"⚠️  Profile scrape failed: {exc}\n")
            profile_posts = []

        # 3. Merge: RSS entries + profile-only extras
        merged: list[dict] = [
            {"title": p["title"], "url": p["url"],
             "pub_date": p["pub_date"], "in_rss": True}
            for p in rss_posts
        ]
        for pp in profile_posts:
            if pp["url"] not in rss_urls:
                sys.stderr.write(f"   ➕ Extra (not in RSS): {pp['title']}\n")
                merged.append({
                    "title":    pp["title"],
                    "url":      pp["url"],
                    "pub_date": "",
                    "in_rss":   False,
                })

        self._cache[key] = {"ts": time.time(), "data": merged}
        _save_cache(self._cache)
        sys.stderr.write(f"✅ Post list ready: {len(merged)} total\n")
        return merged

    # ── Single post ───────────────────────────────────────────────────────────

    def get_post(self, url: str) -> Optional[dict]:
        """
        Return {title, url, content} for the given URL.

        Priority:
          1. Per-post cache (scrape_cache.json, 7-day TTL)
          2. RSS feed content (always loaded as part of get_post_list)
          3. Playwright fallback (for non-RSS articles; may fail if Cloudflare-blocked)
        """
        clean_url = url.split("?")[0]
        key = f"post:{clean_url}"

        # 1. Per-post cache
        cached = self._cache.get(key)
        if cached and time.time() - cached["ts"] < CACHE_TTL_POST:
            sys.stderr.write(f"📦 Returning cached post: {clean_url}\n")
            return cached["data"]

        # 2. RSS feed
        rss_key = "rss_feed"
        rss_cached = self._cache.get(rss_key)
        rss_posts = rss_cached["data"] if rss_cached else _fetch_rss()
        if not rss_cached:
            self._cache[rss_key] = {"ts": time.time(), "data": rss_posts}

        for p in rss_posts:
            if p["url"] == clean_url:
                result = {"title": p["title"], "url": p["url"], "content": p["content"]}
                self._cache[key] = {"ts": time.time(), "data": result}
                _save_cache(self._cache)
                sys.stderr.write(f"✅ Found in RSS: {p['title']}\n")
                return result

        # 3. Playwright fallback
        sys.stderr.write(f"⚠️  Not in RSS, trying Playwright: {clean_url}\n")
        result = _playwright_get_post(clean_url)
        if result:
            self._cache[key] = {"ts": time.time(), "data": result}
            _save_cache(self._cache)
            sys.stderr.write(f"✅ Playwright scraped: {result['title']}\n")
        else:
            sys.stderr.write(f"❌ Could not fetch content for: {clean_url}\n")
        return result

    def invalidate_list_cache(self):
        """Force next get_post_list() call to re-fetch from Medium."""
        self._cache.pop("post_list", None)
        self._cache.pop("rss_feed", None)
        _save_cache(self._cache)


# ── Module-level singleton ────────────────────────────────────────────────────

_scraper: Optional[MediumScraper] = None


def get_scraper() -> MediumScraper:
    global _scraper
    if _scraper is None:
        _scraper = MediumScraper()
    return _scraper

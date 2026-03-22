"""
parser.py — Extracts title, body text, URL, and pub_date from Medium export HTML files.
"""

from bs4 import BeautifulSoup
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger(__name__)

CONTENT_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
MIN_CONTENT_LENGTH = 50

POSTS_DIR = Path(__file__).parent / "data" / "posts"


def parse(filepath: Path) -> Optional[dict]:
    """Parse a Medium export HTML file.

    Returns a dict with keys: title, content, url, pub_date.
    Returns None if the file is too short or unreadable.
    """
    try:
        html = filepath.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("Failed to read %s: %s", filepath, exc)
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Title
    tag = soup.find("h1", class_="p-name") or soup.find("title")
    title = tag.get_text(strip=True) if tag else filepath.stem

    # Body
    body = soup.find("section", attrs={"data-field": "body"})
    if body is None:
        body = soup.find("article") or soup

    paragraphs = [t.get_text(strip=True) for t in body.find_all(CONTENT_TAGS) if t.get_text(strip=True)]
    content = "\n\n".join(paragraphs)

    if len(content) < MIN_CONTENT_LENGTH:
        log.warning("Skipping %s — too short (%d chars)", filepath.name, len(content))
        return None

    # Canonical URL from footer: <a class="p-canonical" href="...">
    canonical_el = soup.find("a", class_="p-canonical")
    url = canonical_el["href"] if canonical_el else ""

    # Publication date from footer: <time class="dt-published" datetime="...">
    time_el = soup.find("time", class_="dt-published")
    pub_date = time_el["datetime"][:10] if time_el else ""  # ISO date YYYY-MM-DD

    return {"title": title, "content": content, "url": url, "pub_date": pub_date}


def get_local_posts(posts_dir: Path = POSTS_DIR) -> list[dict]:
    """Return metadata for all non-draft local HTML post files.

    Each entry: {title, url, pub_date, filepath}
    Sorted newest-first.
    """
    if not posts_dir.exists():
        return []

    results = []
    for html_file in sorted(posts_dir.glob("*.html")):
        if html_file.name.startswith("draft_"):
            continue
        post = parse(html_file)
        if post:
            results.append({
                "title":    post["title"],
                "url":      post["url"],
                "pub_date": post["pub_date"],
                "filepath": html_file,
                "content":  post["content"],
            })

    results.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python parser.py <file.html>")
        raise SystemExit(1)

    result = parse(Path(sys.argv[1]))
    if result:
        print(f"Title   : {result['title']}")
        print(f"URL     : {result['url']}")
        print(f"Date    : {result['pub_date']}")
        print(f"Length  : {len(result['content'])} chars")
        print(f"---\n{result['content'][:300]}…")
    else:
        print("No content extracted.")

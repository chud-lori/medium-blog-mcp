"""
parser.py — Extracts title and body text from Medium export HTML files.
"""

from bs4 import BeautifulSoup
from pathlib import Path
from typing import Optional
import logging

log = logging.getLogger(__name__)

CONTENT_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
MIN_CONTENT_LENGTH = 50


def parse(filepath: Path) -> Optional[dict]:
    """Parse a Medium export HTML file and return {'title': str, 'content': str}."""
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

    return {"title": title, "content": content}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python parser.py <file.html>")
        raise SystemExit(1)

    result = parse(Path(sys.argv[1]))
    if result:
        print(f"Title : {result['title']}")
        print(f"Length: {len(result['content'])} chars")
        print(f"---\n{result['content'][:300]}…")
    else:
        print("No content extracted.")

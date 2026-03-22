"""
build_index.py — Build the ChromaDB vector index from local HTML exports or by scraping Medium live.

Two sources are supported:
  local   — Read from downloaded Medium export files in data/posts/
             (complete: all articles, works offline, no Cloudflare issues)
  scrape  — Fetch live from Medium via sitemap + RSS feed
             (only retrieves the 10 most recent articles with content)

If a Medium export ZIP is found in data/, it will be offered for extraction
before indexing — this replaces data/posts/ with the fresh export content.

Usage:
    python build_index.py                  # auto-detect zip, prompt for source
    python build_index.py --source local   # use local HTML files
    python build_index.py --source scrape  # use live scraping
    python build_index.py --force          # wipe and rebuild from scratch
    python build_index.py --audit          # print index stats, no changes
    python build_index.py --extract-zip    # extract zip only, no indexing
"""

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Optional

import chromadb
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

from parser import get_local_posts, POSTS_DIR
from scraper import get_scraper

# ── Config ────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent
DB_DIR     = ROOT / "vector_db"
SYNC_STATE = ROOT / ".sync_state.json"
COLLECTION = "blog_posts"
CHUNK_SIZE = 800


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sync_state() -> dict:
    if SYNC_STATE.exists():
        return json.loads(SYNC_STATE.read_text())
    return {}


def save_sync_state(state: dict):
    SYNC_STATE.write_text(json.dumps(state, indent=2))


def chunk_text(text: str, max_size: int = CHUNK_SIZE) -> list[str]:
    """Split *text* into paragraph-aware chunks of at most *max_size* chars."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) < max_size:
            buf += p + "\n\n"
        else:
            if buf:
                chunks.append(buf.strip())
            buf = p + "\n\n"
    if buf:
        chunks.append(buf.strip())
    return chunks or [text]


def url_id(url: str) -> str:
    """Stable short ID derived from a URL (used as ChromaDB doc-id prefix)."""
    return hashlib.md5(url.encode()).hexdigest()[:16]


def find_zip() -> Optional[Path]:
    """Return the newest Medium export ZIP in data/, or None."""
    zips = sorted((ROOT / "data").glob("medium-export-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def extract_zip(zip_path: Path) -> int:
    """Extract posts/ from the Medium export ZIP into data/posts/.

    Deletes the existing data/posts/ directory first, then extracts only
    the posts/*.html files from the ZIP.  Returns the number of files extracted.
    """
    print(f"\n📦 ZIP: {zip_path.name}")

    # Wipe existing posts dir
    if POSTS_DIR.exists():
        print(f"🗑️  Removing existing {POSTS_DIR} …")
        shutil.rmtree(POSTS_DIR)
    POSTS_DIR.mkdir(parents=True)

    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        post_entries = [n for n in zf.namelist() if n.startswith("posts/") and n.endswith(".html")]
        print(f"📄 Extracting {len(post_entries)} HTML files…")
        for entry in post_entries:
            filename = Path(entry).name          # strip "posts/" prefix
            target   = POSTS_DIR / filename
            target.write_bytes(zf.read(entry))
            count += 1

    print(f"✅ Extracted {count} files → {POSTS_DIR}\n")
    return count


def maybe_extract_zip() -> bool:
    """If a ZIP exists in data/, offer to extract it. Returns True if extracted."""
    zip_path = find_zip()
    if not zip_path:
        return False

    print(f"\n📦 Found Medium export ZIP: {zip_path.name}")
    choice = input("Extract and replace data/posts/ with this ZIP? [y/N]: ").strip().lower()
    if choice in ("y", "yes"):
        extract_zip(zip_path)
        return True
    return False


def choose_source(source_arg: Optional[str]) -> str:
    """Return 'local' or 'scrape', prompting the user if not specified."""
    if source_arg in ("local", "scrape"):
        return source_arg

    print("Select indexing source:")
    print("  [1] local  — Read from downloaded HTML files in data/posts/")
    print("               (complete: all articles, works offline)")
    print("  [2] scrape — Fetch live from Medium via sitemap + RSS")
    print("               (only the 10 most recent articles have content)")
    print()
    while True:
        choice = input("Choice [1/2] or [local/scrape]: ").strip().lower()
        if choice in ("1", "local"):
            return "local"
        if choice in ("2", "scrape"):
            return "scrape"
        print("Please enter 1, 2, 'local', or 'scrape'.")


# ── Build ─────────────────────────────────────────────────────────────────────

def build_index(force: bool = False, source: Optional[str] = None):
    """Fetch all articles and store embeddings in ChromaDB."""
    maybe_extract_zip()
    source = choose_source(source)
    print(f"\n📂 Source: {source}\n")

    print("🚀 Loading embedding model (paraphrase-multilingual-MiniLM-L12-v2)…")
    # Multilingual model chosen because content is Indonesian + English.
    # ~470 MB; supports 50+ languages with strong semantic similarity.
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    client = chromadb.PersistentClient(path=str(DB_DIR))

    if force:
        print("🗑️  Clearing existing index…")
        SYNC_STATE.unlink(missing_ok=True)
        try:
            client.delete_collection(name=COLLECTION)
        except Exception:
            pass

    col  = client.get_or_create_collection(name=COLLECTION)
    sync = {} if force else load_sync_state()
    total_chunks = 0
    skipped = 0
    failed: list[str] = []

    # ── Gather posts from chosen source ───────────────────────────────────────
    if source == "local":
        posts_meta = _gather_local(col, sync, model, total_chunks, skipped, failed)
        return  # _gather_local handles everything including save

    # source == "scrape"
    print("📋 Fetching article list from Medium…")
    scraper_posts = get_scraper().get_post_list()
    print(f"   Found {len(scraper_posts)} articles\n")

    for meta in tqdm(scraper_posts, desc="Indexing"):
        url   = meta["url"]
        uid   = url_id(url)
        title = meta["title"]

        if not force and uid in sync:
            skipped += 1
            continue

        post: Optional[dict] = get_scraper().get_post(url)
        if not post or not post.get("content"):
            tqdm.write(f"⚠️  Skipping (no content): {title}")
            failed.append(url)
            continue

        _upsert(col, model, uid, url, title, post["content"],
                pub_date=meta.get("pub_date", ""), in_rss=meta.get("in_rss", True))
        total_chunks += len(chunk_text(post["content"]))
        sync[uid] = {"url": url, "title": title, "source": "scrape"}
        tqdm.write(f"✅ {title[:55]}")

    save_sync_state(sync)
    _print_summary(scraper_posts, skipped, failed, total_chunks, col)


def _gather_local(col, sync, model, total_chunks, skipped, failed):
    """Index all non-draft local HTML files."""
    if not POSTS_DIR.exists():
        print(f"❌ posts directory not found: {POSTS_DIR}")
        print("   Download your Medium export and place HTML files in data/posts/")
        return

    posts = get_local_posts()
    print(f"📋 Found {len(posts)} local HTML articles\n")

    sync = load_sync_state()
    total_chunks = 0
    skipped = 0
    failed_list: list[str] = []

    for p in tqdm(posts, desc="Indexing"):
        url   = p["url"]
        title = p["title"]
        uid   = url_id(url)

        if uid in sync:
            skipped += 1
            continue

        content = p.get("content", "")
        if not content:
            tqdm.write(f"⚠️  Skipping (no content): {title}")
            failed_list.append(url or p["filepath"].name)
            continue

        chunks = chunk_text(content)
        _upsert(col, model, uid, url, title, content,
                pub_date=p.get("pub_date", ""), in_rss=False)
        total_chunks += len(chunks)
        sync[uid] = {"url": url, "title": title, "source": "local"}
        tqdm.write(f"✅ {title[:55]}  ({len(chunks)} chunks)")

    save_sync_state(sync)

    indexed = len(posts) - skipped - len(failed_list)
    print(f"\n{'─'*55}")
    print(f"  Indexed  : {indexed} articles  ({total_chunks} chunks)")
    print(f"  Skipped  : {skipped} (already up to date)")
    print(f"  Failed   : {len(failed_list)} (no content)")
    print(f"  Total DB : {col.count()} chunks")
    if failed_list:
        print("\n  Failed:")
        for f in failed_list:
            print(f"    {f}")


def _upsert(col, model, uid: str, url: str, title: str, content: str,
            pub_date: str = "", in_rss: bool = False):
    """Chunk, embed, and store a post in ChromaDB (replacing stale chunks)."""
    chunks = chunk_text(content)

    # Remove any stale chunks for this URL before re-adding
    old_ids = [f"{uid}:chunk{i}" for i in range(200)]
    try:
        existing = col.get(ids=old_ids)
        stale = [eid for eid in existing["ids"] if eid]
        if stale:
            col.delete(ids=stale)
    except Exception:
        pass

    for i, chunk in enumerate(chunks):
        col.add(
            ids=[f"{uid}:chunk{i}"],
            embeddings=[model.encode(chunk).tolist()],
            documents=[chunk],
            metadatas=[{
                "title":    title,
                "url":      url,
                "chunk":    i,
                "pub_date": pub_date,
                "in_rss":   in_rss,
            }],
        )


def _print_summary(posts_meta, skipped, failed, total_chunks, col):
    indexed = len(posts_meta) - skipped - len(failed)
    print(f"\n{'─'*55}")
    print(f"  Indexed  : {indexed} articles  ({total_chunks} chunks)")
    print(f"  Skipped  : {skipped} (already up to date)")
    print(f"  Failed   : {len(failed)} (no content available)")
    print(f"  Total DB : {col.count()} chunks")
    if failed:
        print("\n  Failed URLs:")
        for f in failed:
            print(f"    {f}")


# ── Audit ─────────────────────────────────────────────────────────────────────

def audit():
    """Print a summary of what is currently stored in the vector index."""
    print("📋 Auditing index…\n")
    client = chromadb.PersistentClient(path=str(DB_DIR))

    try:
        col = client.get_collection(name=COLLECTION)
    except Exception:
        print("❌ Collection not found. Run `python build_index.py` first.")
        return

    data  = col.get(include=["metadatas", "documents"])
    metas = data["metadatas"]
    docs  = data["documents"]

    stats: dict[str, dict] = {}
    for i, meta in enumerate(metas):
        url = meta["url"]
        if url not in stats:
            stats[url] = {
                "title":  meta["title"],
                "chunks": 0,
                "chars":  0,
                "in_rss": meta.get("in_rss", True),
            }
        stats[url]["chunks"] += 1
        stats[url]["chars"]  += len(docs[i])

    ranked = sorted(stats.items(), key=lambda x: x[1]["chars"], reverse=True)
    rss_marker = lambda v: "  " if v["in_rss"] else "📄"

    print(f"Posts : {len(stats)}")
    print(f"Chunks: {len(docs)}\n")
    print(f"{'':2} {'Title':<44} {'Chunks':>6} {'Chars':>7}")
    print("─" * 62)
    for _, info in ranked:
        title = (info["title"][:41] + "…") if len(info["title"]) > 42 else info["title"]
        flag  = rss_marker(info)
        print(f"{flag} {title:<44} {info['chunks']:>6} {info['chars']:>7}")

    thin = [s for s, d in stats.items() if d["chunks"] < 2 and d["chars"] < 500]
    if thin:
        print(f"\n⚠️  {len(thin)} posts with very thin content")
    print("\n📄 = indexed from local HTML export")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Medium blog index builder")
    ap.add_argument("--source", choices=["local", "scrape"],
                    help="'local' = read from data/posts/ HTML files; "
                         "'scrape' = fetch live from Medium (sitemap+RSS). "
                         "Prompted interactively if omitted.")
    ap.add_argument("--force",       action="store_true",
                    help="Wipe and rebuild entire index from scratch")
    ap.add_argument("--audit",       action="store_true",
                    help="Print index stats without making changes")
    ap.add_argument("--extract-zip", action="store_true",
                    help="Extract the Medium export ZIP into data/posts/ and exit (no indexing)")
    ap.add_argument("--incremental", action="store_true",
                    help="Alias for default behaviour (skip already-indexed articles)")
    args = ap.parse_args()

    if args.audit:
        audit()
    elif args.extract_zip:
        z = find_zip()
        if z:
            extract_zip(z)
        else:
            print("❌ No medium-export-*.zip found in data/")
    else:
        build_index(force=args.force, source=args.source)

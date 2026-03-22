"""
build_index.py — Build the ChromaDB vector index by scraping Medium live.

Replaces the old file-based indexer. Articles are fetched via the RSS feed
(or Playwright fallback) and their text is chunked and embedded.

Usage:
    python build_index.py                # index all articles (skip already-indexed)
    python build_index.py --force        # wipe and rebuild from scratch
    python build_index.py --audit        # print index stats, no changes
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import chromadb
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

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


# ── Build ─────────────────────────────────────────────────────────────────────

def build_index(force: bool = False):
    """Fetch all articles and store embeddings in ChromaDB."""
    scraper = get_scraper()

    print("📋 Fetching article list…")
    posts_meta = scraper.get_post_list()
    print(f"   Found {len(posts_meta)} articles\n")

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

    col   = client.get_or_create_collection(name=COLLECTION)
    sync  = {} if force else load_sync_state()
    total_chunks = 0
    skipped = 0
    failed: list[str] = []

    for meta in tqdm(posts_meta, desc="Indexing"):
        url   = meta["url"]
        uid   = url_id(url)
        title = meta["title"]

        # Skip if already indexed (keyed by URL hash)
        if not force and uid in sync:
            skipped += 1
            continue

        # Fetch full content
        post: Optional[dict] = scraper.get_post(url)
        if not post or not post.get("content"):
            tqdm.write(f"⚠️  Skipping (no content): {title}")
            failed.append(url)
            continue

        # Remove any stale chunks for this URL before re-adding
        old_ids = [f"{uid}:chunk{i}" for i in range(200)]
        try:
            existing = col.get(ids=old_ids)
            stale = [eid for eid in existing["ids"] if eid]
            if stale:
                col.delete(ids=stale)
        except Exception:
            pass

        # Chunk, embed, store
        chunks = chunk_text(post["content"])
        for i, chunk in enumerate(chunks):
            col.add(
                ids=[f"{uid}:chunk{i}"],
                embeddings=[model.encode(chunk).tolist()],
                documents=[chunk],
                metadatas=[{
                    "title":    title,
                    "url":      url,
                    "chunk":    i,
                    "pub_date": meta.get("pub_date", ""),
                    "in_rss":   meta.get("in_rss", True),
                }],
            )
        total_chunks += len(chunks)
        sync[uid] = {"url": url, "title": title, "chunks": len(chunks)}
        tqdm.write(f"✅ {title[:55]}  ({len(chunks)} chunks)")

    save_sync_state(sync)

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
    rss_marker = lambda v: "  " if v["in_rss"] else "🔒"

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
    print("\n🔒 = not in RSS feed (member-only or unlisted)")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Medium blog index builder")
    ap.add_argument("--force",       action="store_true",
                    help="Wipe and rebuild entire index from scratch")
    ap.add_argument("--audit",       action="store_true",
                    help="Print index stats without making changes")
    ap.add_argument("--incremental", action="store_true",
                    help="Alias for default behaviour (skip already-indexed articles)")
    args = ap.parse_args()

    if args.audit:
        audit()
    else:
        build_index(force=args.force)

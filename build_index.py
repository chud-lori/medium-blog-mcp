"""
indexer.py — Build and audit the ChromaDB vector index from local Medium export HTML.

Usage:
    python indexer.py              # full re-index
    python indexer.py --audit      # print index stats without modifying anything
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List

import chromadb
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

from parser import parse

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
POSTS_DIR = ROOT / "data" / "posts"
DB_DIR = ROOT / "vector_db"
SYNC_STATE = ROOT / ".sync_state.json"
COLLECTION = "blog_posts"
CHUNK_SIZE = 800


# ── Helpers ──────────────────────────────────────────────────────────────────
def load_sync_state() -> Dict[str, str]:
    if SYNC_STATE.exists():
        return json.loads(SYNC_STATE.read_text())
    return {}


def save_sync_state(state: Dict[str, str]):
    SYNC_STATE.write_text(json.dumps(state, indent=2))


def chunk_text(text: str, max_size: int = CHUNK_SIZE) -> List[str]:
    """Split text into paragraph-aware chunks."""
    paragraphs = text.split("\n\n")
    chunks, buf = [], ""
    for p in paragraphs:
        if len(buf) + len(p) < max_size:
            buf += p + "\n\n"
        else:
            if buf:
                chunks.append(buf.strip())
            buf = p + "\n\n"
    if buf:
        chunks.append(buf.strip())
    return chunks


# ── Index ────────────────────────────────────────────────────────────────────
def build_index(force: bool = True):
    """Parse every HTML file in POSTS_DIR and store embeddings in ChromaDB."""
    if not POSTS_DIR.exists():
        print(f"❌ Posts directory not found: {POSTS_DIR}")
        return

    print("🚀 Initializing…")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=str(DB_DIR))

    if force:
        print("🗑️  Clearing existing index…")
        SYNC_STATE.unlink(missing_ok=True)
        try:
            client.delete_collection(name=COLLECTION)
        except Exception:
            pass

    col = client.get_or_create_collection(name=COLLECTION)
    sync = {} if force else load_sync_state()
    files = sorted(POSTS_DIR.glob("*.html"))
    total_chunks = 0

    print(f"📂 Found {len(files)} files\n")

    for fp in tqdm(files, desc="Indexing"):
        fid = fp.name
        file_hash = hashlib.md5(fp.read_bytes()).hexdigest()

        if not force and sync.get(fid) == file_hash:
            continue

        result = parse(fp)
        if not result:
            continue

        chunks = chunk_text(result["content"])
        doc_hash = hashlib.md5(fid.encode()).hexdigest()

        for i, chunk in enumerate(chunks):
            col.add(
                ids=[f"{doc_hash}:{i}"],
                embeddings=[model.encode(chunk).tolist()],
                documents=[chunk],
                metadatas=[{
                    "title": result["title"],
                    "source": fid,
                    "chunk_index": i,
                    "is_draft": fid.startswith("draft_"),
                }],
            )
        total_chunks += len(chunks)
        sync[fid] = file_hash

    save_sync_state(sync)
    print(f"\n✅ Done — {total_chunks} chunks indexed ({col.count()} total in collection)")


# ── Audit ────────────────────────────────────────────────────────────────────
def audit():
    """Print a summary of what's in the index."""
    print("📋 Auditing index…\n")
    client = chromadb.PersistentClient(path=str(DB_DIR))

    try:
        col = client.get_collection(name=COLLECTION)
    except Exception:
        print("❌ Collection not found. Run `python indexer.py` first.")
        return

    data = col.get(include=["metadatas", "documents"])
    metas, docs = data["metadatas"], data["documents"]

    stats: Dict[str, dict] = {}
    for i, meta in enumerate(metas):
        src = meta["source"]
        if src not in stats:
            stats[src] = {"title": meta["title"], "chunks": 0, "chars": 0}
        stats[src]["chunks"] += 1
        stats[src]["chars"] += len(docs[i])

    ranked = sorted(stats.items(), key=lambda x: x[1]["chars"], reverse=True)

    print(f"Posts : {len(stats)}")
    print(f"Chunks: {len(docs)}\n")
    print(f"{'Title':<42} {'Chunks':>6} {'Chars':>7}")
    print("-" * 58)
    for _, info in ranked:
        title = (info["title"][:39] + "…") if len(info["title"]) > 40 else info["title"]
        print(f"{title:<42} {info['chunks']:>6} {info['chars']:>7}")

    thin = [s for s, d in stats.items() if d["chunks"] < 2 and d["chars"] < 500]
    if thin:
        print(f"\n⚠️  {len(thin)} posts with very thin content")


# ── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Blog index builder")
    ap.add_argument("--audit", action="store_true", help="Print index stats")
    ap.add_argument("--incremental", action="store_true", help="Only index changed files")
    args = ap.parse_args()

    if args.audit:
        audit()
    else:
        build_index(force=not args.incremental)

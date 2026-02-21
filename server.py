"""
server.py — MCP server exposing Lori's blog as searchable knowledge.

Tools:
    search_posts  — semantic search across all indexed blog chunks
    list_posts    — list every HTML file in the export
    read_post     — read the full text of a single post
"""

import sys
import io
import os
from pathlib import Path

# MCP communicates over stdout (JSON-RPC), so we must NOT permanently redirect it.
# Instead, temporarily mute stdout during noisy library imports, then restore it.
_real_stdout = sys.stdout
sys.stdout = io.StringIO()
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from mcp.server.fastmcp import FastMCP
import chromadb
from sentence_transformers import SentenceTransformer

# Restore stdout so MCP JSON-RPC transport works
sys.stdout = _real_stdout

from parser import parse

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
POSTS_DIR = ROOT / "data" / "posts"
DB_DIR = ROOT / "vector_db"
COLLECTION = "blog_posts"

# ── MCP Server ───────────────────────────────────────────────────────────────
mcp = FastMCP("lori-medium-blog")

# ── Lazy-loaded RAG components ───────────────────────────────────────────────
_model = None
_collection = None


def _rag():
    """Return (model, collection), loading on first call."""
    global _model, _collection
    if _model is None:
        sys.stderr.write("⏳ Loading embedding model…\n")

        # Mute stdout during model load (prints loading bars)
        _save = sys.stdout
        sys.stdout = io.StringIO()
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        sys.stdout = _save

        client = chromadb.PersistentClient(path=str(DB_DIR))
        _collection = client.get_collection(name=COLLECTION)
        sys.stderr.write(f"✅ Loaded — {_collection.count()} chunks ready\n")
    return _model, _collection


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_posts(query: str, limit: int = 5) -> str:
    """Semantic search across Lori's blog posts and drafts.

    Returns the most relevant passages for the given natural-language query.

    Args:
        query: What to search for (e.g. "existentialism", "thoughts on death").
        limit: Max results (default 5).
    """
    model, col = _rag()
    embedding = model.encode(query).tolist()
    results = col.query(query_embeddings=[embedding], n_results=limit)

    if not results["documents"] or not results["documents"][0]:
        return "No matches found."

    lines = [f"Found {len(results['documents'][0])} matches:\n"]
    for i, (doc, meta) in enumerate(zip(results["documents"][0], results["metadatas"][0])):
        lines.append(f"── Match {i+1}: {meta['title']} ──")
        lines.append(f"Source: {meta['source']}")
        lines.append(doc)
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def list_posts() -> str:
    """List every blog post file available in the local export."""
    if not POSTS_DIR.exists():
        return f"Error: {POSTS_DIR} not found."
    files = sorted(POSTS_DIR.glob("*.html"))
    lines = [f"{len(files)} posts available:\n"]
    for i, f in enumerate(files, 1):
        lines.append(f"{i:3d}. {f.name}")
    return "\n".join(lines)


@mcp.tool()
def read_post(filename: str) -> str:
    """Read the full content of a blog post.

    Args:
        filename: HTML filename from list_posts or search_posts (e.g. '2026-02-14_I-can-feel...html').
    """
    fp = POSTS_DIR / filename
    if not fp.exists():
        return f"Error: {filename} not found."
    result = parse(fp)
    if not result:
        return f"Error: could not extract content from {filename}."
    return f"# {result['title']}\n\n{result['content']}"


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()

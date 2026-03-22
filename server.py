"""
server.py — MCP server exposing Lori's Medium blog as searchable knowledge.

Tools:
    list_posts   — list all articles from Medium (sitemap + RSS merge)
    read_post    — return full text of a post (vector store → RSS cache)
    search_posts — semantic search across indexed posts

Embedding model: paraphrase-multilingual-MiniLM-L12-v2
  Chosen because blog content is Indonesian + English.
  Supports 50+ languages, ~470 MB, strong semantic similarity performance.
"""

import sys
import io
import os
from pathlib import Path

# Silence noisy library output before MCP JSON-RPC transport is active
_real_stdout = sys.stdout
sys.stdout = io.StringIO()
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from mcp.server.fastmcp import FastMCP
import chromadb
from sentence_transformers import SentenceTransformer

sys.stdout = _real_stdout

from scraper import get_scraper

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent
DB_DIR     = ROOT / "vector_db"
COLLECTION = "blog_posts"

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("lori-medium-blog")

# ── Lazy-loaded RAG components ────────────────────────────────────────────────
_model      = None
_collection = None


def _rag():
    """Return (model, chroma_collection), initialising on first call."""
    global _model, _collection
    if _model is None:
        sys.stderr.write("⏳ Loading embedding model…\n")
        _save = sys.stdout
        sys.stdout = io.StringIO()
        # paraphrase-multilingual-MiniLM-L12-v2:
        #   • 470 MB on disk (vs 80 MB for all-MiniLM-L6-v2)
        #   • 50+ languages including Indonesian — essential for this blog
        #   • Strong semantic similarity for blog / literary text
        _model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        sys.stdout = _save

        client = chromadb.PersistentClient(path=str(DB_DIR))
        try:
            _collection = client.get_collection(name=COLLECTION)
        except Exception:
            _collection = client.create_collection(name=COLLECTION)

        sys.stderr.write(f"✅ Loaded — {_collection.count()} chunks in vector store\n")
    return _model, _collection


def _read_post_from_index(url: str) -> str | None:
    """Reconstruct full post text from ChromaDB chunks. Returns None if not indexed."""
    _, col = _rag()
    if col.count() == 0:
        return None
    clean_url = url.split("?")[0]
    try:
        result = col.get(where={"url": clean_url}, include=["documents", "metadatas"])
    except Exception:
        return None
    if not result["ids"]:
        return None

    # Re-assemble chunks in order
    pairs = sorted(
        zip(result["metadatas"], result["documents"]),
        key=lambda x: x[0].get("chunk", 0),
    )
    title   = pairs[0][0].get("title", url)
    content = "\n\n".join(doc for _, doc in pairs)
    return f"# {title}\n\nURL: {clean_url}\n\n{content}"


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_posts() -> str:
    """List every article published on Lori's Medium profile.

    Merges the sitemap (live) with locally-indexed articles so that all
    89+ articles are shown, including those not in the Medium sitemap.
    Results are cached for 6 hours.
    """
    scraper_posts = get_scraper().get_post_list()

    _, col = _rag()
    indexed_meta: dict[str, dict] = {}
    if col.count() > 0:
        try:
            all_meta = col.get(include=["metadatas"])["metadatas"]
            for m in all_meta:
                url = m.get("url", "")
                if url and url not in indexed_meta:
                    indexed_meta[url] = m
        except Exception:
            pass

    # Build merged list: scraper posts + any locally-indexed posts not in scraper
    scraper_urls = {p["url"] for p in scraper_posts}
    extra_posts: list[dict] = []
    for url, m in indexed_meta.items():
        if url not in scraper_urls:
            extra_posts.append({
                "title":    m.get("title", url),
                "url":      url,
                "pub_date": m.get("pub_date", ""),
                "in_rss":   False,
            })

    extra_posts.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    all_posts = scraper_posts + extra_posts

    if not all_posts:
        return (
            "No posts found. Medium may be temporarily unavailable.\n"
            "Try again in a moment."
        )

    total_in_rss = sum(1 for p in scraper_posts if p["in_rss"])
    lines = [
        f"Found {len(all_posts)} posts ({len(scraper_posts)} from sitemap, {len(extra_posts)} from local index)",
        f"({total_in_rss} with live content via RSS)\n",
    ]
    for i, p in enumerate(all_posts, 1):
        content_flag = "  " if p.get("in_rss") else "📄"
        indexed_flag = "📚" if p["url"] in indexed_meta else "  "
        pub = f"  [{p['pub_date'][:10]}]" if p.get("pub_date") else ""
        lines.append(f"{i:3d}. {content_flag}{indexed_flag} {p['title']}{pub}")
        lines.append(f"       {p['url']}")

    lines.append("\nLegend: 📚 = indexed (searchable)  📄 = older/local post")
    return "\n".join(lines)


@mcp.tool()
def read_post(url: str) -> str:
    """Return the full text of a Medium blog post.

    Reads from the local vector store if the post has been indexed.
    Falls back to a live RSS/scrape fetch for un-indexed posts
    (and indexes the result for future searches).

    Args:
        url: Full post URL — copy from list_posts output.
    """
    # 1. Try vector store (fast, offline)
    indexed = _read_post_from_index(url)
    if indexed:
        sys.stderr.write(f"📚 Served from index: {url}\n")
        return indexed

    # 2. Live fetch (RSS cache → Playwright fallback)
    post = get_scraper().get_post(url)
    if not post:
        return (
            f"Could not retrieve content from:\n  {url}\n\n"
            "Possible reasons:\n"
            "  • The post requires a Medium membership (marked 🔒 in list_posts)\n"
            "  • Cloudflare blocked the headless browser\n"
            "  • The URL is incorrect\n\n"
            "Tip: run `python build_index.py` to pre-index all accessible articles."
        )

    # Index for future searches
    _index_post(post)

    return f"# {post['title']}\n\nURL: {post['url']}\n\n{post['content']}"


@mcp.tool()
def search_posts(query: str, limit: int = 5) -> str:
    """Semantic search across all indexed blog posts.

    Requires the vector index to be populated first.
    Run `python build_index.py` to index all articles, or call read_post
    on individual posts to index them on demand.

    Args:
        query: Natural-language query (e.g. "existentialism and identity").
        limit: Maximum results to return (default 5).
    """
    model, col = _rag()
    count = col.count()
    if count == 0:
        return (
            "The vector index is empty.\n\n"
            "To populate it, run one of:\n"
            "  • python build_index.py          (index all accessible articles)\n"
            "  • Call read_post for each article you want indexed on demand"
        )

    n         = min(limit, count)
    embedding = model.encode(query).tolist()
    results   = col.query(query_embeddings=[embedding], n_results=n)

    if not results["documents"] or not results["documents"][0]:
        return "No matches found."

    lines = [f"Found {len(results['documents'][0])} matches:\n"]
    for i, (doc, meta) in enumerate(
        zip(results["documents"][0], results["metadatas"][0])
    ):
        lines.append(f"── Match {i+1}: {meta['title']} ──")
        lines.append(f"URL: {meta['url']}")
        lines.append(doc)
        lines.append("")
    return "\n".join(lines)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _chunk_text(text: str, max_size: int = 800) -> list[str]:
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
    return chunks or [text]


def _index_post(post: dict):
    """Embed and upsert a single post into ChromaDB (skips existing chunks)."""
    import hashlib
    model, col = _rag()
    url   = post["url"]
    title = post["title"]
    uid   = hashlib.md5(url.encode()).hexdigest()[:16]

    for i, chunk in enumerate(_chunk_text(post["content"])):
        doc_id = f"{uid}:chunk{i}"
        if col.get(ids=[doc_id])["ids"]:
            continue
        col.add(
            ids=[doc_id],
            embeddings=[model.encode(chunk).tolist()],
            documents=[chunk],
            metadatas=[{"title": title, "url": url, "chunk": i}],
        )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()

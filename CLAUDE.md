# medium-blog-mcp — Claude Code Guide

## Project overview

MCP (Model Context Protocol) server that exposes **Lori's Medium blog**
(`https://chud-lori.medium.com/`) as a searchable knowledge source for Claude.

Articles are fetched live from Medium and stored in a local ChromaDB vector
database so Claude can list, read, and semantically search them without
hitting Medium on every request.

---

## Python environment

This project **must** use the `mcp` pyenv virtualenv (Python 3.11.3).

```bash
# Activate before running anything
pyenv activate mcp        # or: PYENV_VERSION=mcp pyenv exec python ...

# Python binary (used in mcp_config.json)
/Users/nurchudlori/.pyenv/versions/mcp/bin/python3
```

The `.python-version` file in the repo root pins the project to `mcp`
automatically when you `cd` into the directory with pyenv-virtualenv.

---

## Setup (first time)

```bash
pyenv activate mcp
pip install -r requirements.txt
python -m playwright install chromium   # installs headless Chromium
python build_index.py                   # scrape & index all articles
```

---

## Architecture

```
Medium RSS feed  ──────────────────────────┐
  (httpx, no bot detection)                │
                                           ▼
Medium profile page  ──────────────────► Merger  ──► post list
  (Playwright sync, catches extras)        │
                                           │
                                           ▼
                                     scrape_cache.json   (TTL cache)
                                           │
                                           ▼
                                      ChromaDB            (vector_db/)
                                  (sentence-transformers)
                                           │
                                           ▼
                                       server.py  ──► Claude (MCP)
```

### Data sources

| Source | What it provides | Bot-detection risk |
|--------|-----------------|-------------------|
| RSS feed (`/feed`) | Full article HTML for ~9 posts | None |
| Profile page (Playwright) | Title + URL for all 10 posts | Low |
| Playwright article fetch | Full content for non-RSS posts | High (Cloudflare) |

Member-only/paywalled articles are absent from the RSS feed and
are blocked by Cloudflare when scraped directly. They are listed
(with a 🔒 flag) but their content may not be retrievable.

---

## Key files

| File | Purpose |
|------|---------|
| `server.py` | FastMCP server; exposes 3 tools to Claude |
| `scraper.py` | RSS fetching + Playwright profile/article scraping |
| `build_index.py` | CLI tool: fetch all articles → embed → store in ChromaDB |
| `vector_db/` | ChromaDB persistent storage (git-ignored) |
| `data/scrape_cache.json` | JSON cache of scraped content (git-ignored) |
| `.sync_state.json` | Tracks which articles are indexed (git-ignored) |
| `mcp_config.json` | Claude Desktop MCP config template |

---

## MCP tools

### `list_posts()`
Fetches the merged article list (RSS + profile page).
Cached for 6 hours. Shows which posts are indexed (📚) and which
are member-only (🔒).

### `read_post(url)`
Returns the full text of an article.
Priority: **vector store → RSS cache → live scrape**.
Automatically indexes the post if it was fetched live.

### `search_posts(query, limit=5)`
Semantic search over the ChromaDB index.
Requires `build_index.py` to have been run first.

---

## build_index.py

Run this once to pre-index all articles, or again to pick up new posts.

```bash
python build_index.py              # incremental (skip already-indexed)
python build_index.py --force      # wipe and rebuild everything
python build_index.py --audit      # inspect what is currently indexed
```

The indexer:
1. Calls `scraper.get_post_list()` to get the merged article list.
2. For each article, calls `scraper.get_post(url)` — returns content from
   the RSS cache for the 9 accessible posts.
3. Chunks the text (≤800 chars, paragraph-aware), embeds with
   `all-MiniLM-L6-v2`, and stores in ChromaDB.
4. Writes `.sync_state.json` so re-runs skip already-indexed articles.

---

## Caching

| Cache key | TTL | Location |
|-----------|-----|----------|
| `post_list` | 6 hours | `data/scrape_cache.json` |
| `rss_feed` | 6 hours | `data/scrape_cache.json` |
| `post:<url>` | 7 days | `data/scrape_cache.json` |

To force a fresh fetch, delete `data/scrape_cache.json` and re-run.

---

## Adding Claude Desktop integration

Copy the contents of `mcp_config.json` into your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json`), then
restart Claude Desktop.

---

## Dependencies

```
mcp>=0.9.0                # Model Context Protocol / FastMCP
chromadb>=0.4.0           # Vector database
sentence-transformers>=2.2.0  # Embedding model (all-MiniLM-L6-v2)
httpx>=0.24.0             # HTTP client for RSS fetching
beautifulsoup4>=4.12.0    # HTML → plain text parsing
playwright>=1.40.0        # Headless browser (profile page + fallback)
python-dotenv>=1.0.0      # .env file support
```

---

## Known limitations

- **Member-only articles** (🔒): absent from RSS, blocked by Cloudflare when
  scraped. Currently only title/URL are available for these.
- **Scrolling Medium pages**: scrolling to the bottom of a Medium page triggers
  a 500 error. `_scrape_profile_page()` evaluates the DOM before scrolling to
  avoid this.
- **Cloudflare on article pages**: individual article URLs go through
  Cloudflare bot-detection. The Playwright fallback may fail. The RSS feed
  sidesteps this entirely for publicly accessible posts.

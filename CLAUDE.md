# medium-blog-mcp — Claude Code Guide

## Git policy

> **Only commit. Never push.**
>
> All work stays local. Do not run `git push` under any circumstances.
> Create commits freely, but the remote is off-limits.

---

## Project overview

MCP (Model Context Protocol) server that exposes **Lori's Medium blog**
(`https://chud-lori.medium.com/`) as a searchable knowledge source for Claude.

Articles are fetched from Medium and stored in a local ChromaDB vector database
so Claude can list, read, and semantically search them without hitting Medium
on every request.

---

## Python environment

This project **must** use the `mcp` pyenv virtualenv (Python 3.11.3).

```bash
# Activate before running anything
pyenv activate mcp        # or: PYENV_VERSION=mcp pyenv exec python ...

# Absolute Python binary (used in mcp_config.json)
/Users/nurchudlori/.pyenv/versions/mcp/bin/python3
```

The `.python-version` file pins the project to `mcp` automatically when you
`cd` into the directory with pyenv-virtualenv active.

Always prefix Python/pip commands with `PYENV_VERSION=mcp pyenv exec` or
activate the env first.

---

## Setup (first time)

```bash
pyenv activate mcp
pip install -r requirements.txt
python build_index.py        # scrape & index all retrievable articles
```

Playwright's Chromium browser is **not needed** for the current architecture
(sitemap + RSS only). If you re-add Playwright scraping, install it with:
```bash
python -m playwright install chromium
```

---

## Architecture

```
Sitemap  (all article URLs, no bot detection)  ──┐
  httpx GET, XML parse                            │
                                                  ▼
RSS feed (content for ~10 recent posts)  ──────► Merger  ──► post list
  httpx GET, XML + HTML parse                     │
                                                  │
                                                  ▼
                                          scrape_cache.json   (TTL cache)
                                                  │
                                                  ▼
                                           ChromaDB            (vector_db/)
                                    paraphrase-multilingual-
                                       MiniLM-L12-v2
                                                  │
                                                  ▼
                                            server.py  ──► Claude (MCP)
```

### Data sources

| Source | Provides | Bot-detection |
|--------|----------|--------------|
| Sitemap (`/sitemap/sitemap.xml`) | All article URLs + lastmod dates | None |
| RSS feed (`/feed`) | Full content for ≤10 most recent posts | None |
| Article pages (direct) | **BLOCKED** — Cloudflare bot detection | High |
| Medium GraphQL (`_/graphql`) | **BLOCKED** — POST requests blocked by Cloudflare | High |

**Why RSS is limited to 10 posts**: Medium hard-caps the RSS feed at the
10 most recent items. This is a platform limitation. The sitemap provides
the complete URL list (57+ articles) but without content for older posts.

**Cloudflare situation**: Direct article pages and GraphQL POST requests are
blocked by Cloudflare even from headless Playwright. Only GET requests to
the sitemap and RSS endpoints work reliably without authentication.

---

## Embedding model

**`paraphrase-multilingual-MiniLM-L12-v2`** (sentence-transformers)

Chosen because:
- Blog content is mixed **Indonesian + English** — a multilingual model is essential
- Supports 50+ languages
- ~470 MB on disk — lightweight enough for local use
- Strong semantic similarity for literary/narrative text

The previous model (`all-MiniLM-L6-v2`, 80 MB) was English-only and gave
poor results for Indonesian text.

If you change the model, delete `vector_db/` and re-run `build_index.py`
to rebuild embeddings with the new model.

---

## Key files

| File | Purpose |
|------|---------|
| `server.py` | FastMCP server; exposes 3 tools to Claude |
| `scraper.py` | Sitemap + RSS fetching, JSON caching |
| `build_index.py` | CLI: fetch articles → chunk → embed → store in ChromaDB |
| `vector_db/` | ChromaDB persistent storage (git-ignored) |
| `data/scrape_cache.json` | JSON cache of fetched content (git-ignored) |
| `.sync_state.json` | Tracks which articles are indexed (git-ignored) |
| `mcp_config.json` | Claude Desktop MCP config template |
| `parser.py` | Legacy HTML parser for local export files (unused) |
| `build_index.py` | Indexer (replaces old file-based version) |

---

## MCP tools

### `list_posts()`
Returns the complete article list from the sitemap (all 57+ articles),
enriched with titles and dates from the RSS feed for recent posts.
Shows 📚 (indexed in vector store) and 📄 (older post, content may be unavailable).
Cached 6 hours.

### `read_post(url)`
Returns full article text.
Priority: **vector store → RSS cache**.
Automatically indexes the post if fetched live.
Older articles (not in RSS) will return an unavailability message.

### `search_posts(query, limit=5)`
Semantic search over ChromaDB.
Requires `build_index.py` to have been run first.

---

## build_index.py

```bash
python build_index.py              # incremental (skip already-indexed)
python build_index.py --force      # wipe and rebuild everything
python build_index.py --audit      # inspect what is currently indexed
```

Steps:
1. Fetches all article URLs from sitemap
2. Fetches content for each via `scraper.get_post()` (RSS cache for recent 10)
3. Older articles that have no RSS content are skipped with a warning
4. Chunks text (≤800 chars, paragraph-aware), embeds, stores in ChromaDB
5. Writes `.sync_state.json` to skip re-indexing unchanged articles

---

## Caching

| Cache key | TTL | Notes |
|-----------|-----|-------|
| `post_list` | 6 hours | Merged sitemap + RSS metadata |
| `rss_feed` | 6 hours | Full HTML content for ≤10 recent posts |
| `sitemap` | 6 hours | All article URLs from sitemap |
| `post:<url>` | 7 days | Full text of a specific post |

Delete `data/scrape_cache.json` to force a full refresh.

---

## Adding Claude Desktop integration

Copy `mcp_config.json` into your Claude Desktop config:
`~/Library/Application Support/Claude/claude_desktop_config.json`

Then restart Claude Desktop.

---

## Known limitations

1. **Content for older posts unavailable**: Medium caps RSS at 10 items.
   Direct article pages are blocked by Cloudflare from headless browsers.
   Articles published before the rolling 10-post RSS window have no content.

2. **Member-only articles**: Paywall articles may also be absent from RSS.
   They appear in the sitemap URL list but content cannot be retrieved.

3. **Sitemap staleness**: Medium's sitemap may lag by hours after a new post.
   Run `build_index.py` after publishing to pick up new content quickly.

---

## Dependencies

```
mcp>=0.9.0                        # Model Context Protocol / FastMCP
chromadb>=0.4.0                   # Vector database
sentence-transformers>=2.2.0      # paraphrase-multilingual-MiniLM-L12-v2
httpx>=0.24.0                     # HTTP client (sitemap + RSS)
beautifulsoup4>=4.12.0            # HTML → plain text
playwright>=1.40.0                # (kept as dep, not used in current flow)
python-dotenv>=1.0.0              # .env support
```

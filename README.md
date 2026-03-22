# Lori's Blog — MCP Server + RAG Chat

Two ways to explore all 90+ published articles from [chud-lori.medium.com](https://chud-lori.medium.com/):

1. **MCP Server** — attach to Claude Desktop so Claude can list, read, and search articles as tools
2. **Chat Web UI** — standalone RAG chatbot at `http://localhost:8000`, works with Claude API, OpenAI, or Gemini

Semantic search is powered by a local ChromaDB vector index using `paraphrase-multilingual-MiniLM-L12-v2` (Indonesian + English).

---

## Project structure

```
├── server.py           # MCP server (3 tools: list_posts, read_post, search_posts)
├── chat_server.py      # Web chat server (FastAPI + SSE streaming)
├── build_index.py      # CLI: build / update the vector index
├── scraper.py          # Live scraping via Medium sitemap + RSS
├── parser.py           # Parses Medium export HTML → text + metadata
├── static/index.html   # Chat web UI
├── data/
│   ├── posts/          # Extracted HTML files (from Medium export ZIP)
│   └── medium-export-*.zip  # Drop new export ZIPs here
├── vector_db/          # ChromaDB index (auto-generated, git-ignored)
├── mcp_config.json     # Claude Desktop config template
└── .env                # API keys (copy from .env.example)
```

---

## Python environment

This project uses the `mcp` pyenv virtualenv (Python 3.11.3).

```bash
pyenv activate mcp
# or prefix commands with: PYENV_VERSION=mcp pyenv exec python3
```

---

## Setup

### 1. Install dependencies

```bash
pyenv activate mcp
pip install -r requirements.txt
```

### 2. Build the index from your Medium export

Download your Medium data export from [medium.com/me/export](https://medium.com/me/export).
Place the ZIP file inside `data/` — no need to unzip manually.

```bash
python build_index.py --source local --force
```

When prompted, enter `y` to extract the ZIP. The indexer will:
- Delete `data/posts/` and extract fresh HTML files from the ZIP
- Embed and store all articles in ChromaDB

```
📦 Found Medium export ZIP: medium-export-*.zip
Extract and replace data/posts/ with this ZIP? [y/N]: y
🗑️  Removing existing data/posts/ …
📄 Extracting 102 HTML files…
✅ Extracted 102 files

Indexed  : 90 articles  (758 chunks)
Skipped  : 0
Failed   : 0
Total DB : 758 chunks
```

**Index options:**

```bash
python build_index.py                    # prompt for source + zip (incremental)
python build_index.py --source local     # local HTML files, skip already-indexed
python build_index.py --source scrape    # live Medium scrape (RSS, 10 recent only)
python build_index.py --force            # wipe and rebuild from scratch
python build_index.py --audit            # show index stats, no changes
python build_index.py --extract-zip      # extract ZIP only, don't index
```

> **Why not scrape all articles?**
> Medium's RSS feed only returns the 10 most recent posts. Direct article pages
> are blocked by Cloudflare. The local export ZIP is the only complete source.

---

## Chat Web UI

A standalone RAG chatbot — no Claude Desktop needed. Requires an API key.

### 1. Configure your LLM provider

```bash
cp .env.example .env
```

Edit `.env`:

```env
LLM_PROVIDER=gemini          # claude | openai | gemini

ANTHROPIC_API_KEY=sk-ant-...  # console.anthropic.com (separate from Claude Pro)
OPENAI_API_KEY=sk-...         # platform.openai.com
GOOGLE_API_KEY=AIza...        # aistudio.google.com → Get API key (free tier)
```

**Getting an API key:**

| Provider | URL | Free tier |
|----------|-----|-----------|
| Gemini   | [aistudio.google.com](https://aistudio.google.com) | Yes — generous free tier |
| Claude   | [console.anthropic.com](https://console.anthropic.com) | $5 credit on signup (separate from Claude Pro subscription) |
| ChatGPT  | [platform.openai.com](https://platform.openai.com) | Pay-as-you-go |

> **Note:** Claude Pro ($20/mo on claude.ai) does **not** include API access.
> API billing is managed separately at console.anthropic.com.

### 2. Start the chat server

```bash
pyenv activate mcp
python chat_server.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

```bash
python chat_server.py --port 8080   # custom port
```

---

## MCP Server (Claude Desktop)

Lets Claude use the blog as a knowledge source inside Claude Desktop.

### Configure Claude Desktop

Copy `mcp_config.json` into:
`~/Library/Application Support/Claude/claude_desktop_config.json`

Or add this to your existing config:

```json
{
  "mcpServers": {
    "lori-medium-blog": {
      "command": "/Users/nurchudlori/.pyenv/versions/mcp/bin/python3",
      "args": ["/path/to/this/repo/server.py"]
    }
  }
}
```

Restart Claude Desktop. Claude will have access to three tools:

| Tool | Description |
|------|-------------|
| `list_posts()` | List all articles (sitemap + locally indexed) |
| `read_post(url)` | Read full text of a post (vector store → live fetch) |
| `search_posts(query, limit)` | Semantic search across indexed articles |

---

## Updating the index

When you publish new articles and download a fresh export:

1. Put the new ZIP in `data/`
2. Run: `python build_index.py --source local --force`
3. Enter `y` when asked to extract the ZIP

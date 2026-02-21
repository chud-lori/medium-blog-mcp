# Lori Blog MCP Server

An MCP server that exposes 100+ blog posts as searchable knowledge for AI assistants via semantic search (RAG).

## Structure

```
├── server.py           # MCP server — exposes 3 tools
├── build_index.py      # builds the vector index from HTML files
├── parser.py           # extracts text from Medium export HTML
├── data/posts/         # Medium export HTML files
├── vector_db/          # ChromaDB index (auto-generated, gitignored)
└── mcp_config.json     # Claude Desktop / Antigravity config template
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare your data

Export your Medium data and place the `posts/` folder inside `data/`:

```
data/
└── posts/
    ├── 2024-01-01_My-First-Post-abc123.html
    ├── 2024-02-14_Another-Post-def456.html
    └── ...
```

### 3. Build the search index

```bash
python build_index.py
```

This parses every HTML file, splits the text into chunks, generates embeddings, and stores them in `vector_db/`. Takes about 1–2 minutes on first run.

To verify the index:

```bash
python build_index.py --audit
```

### 4. Configure your MCP client

Copy the contents of `mcp_config.json` into your MCP client settings.

**Claude Desktop**: `Settings → Developer → Edit Config`

```json
{
  "mcpServers": {
    "lori-blog": {
      "command": "/path/to/your/python3",
      "args": ["/path/to/this/repo/server.py"]
    }
  }
}
```

> [!IMPORTANT]
> Use the **absolute path** to both `python3` (from your virtualenv) and `server.py`.

Then restart Claude Desktop.

## Tools

| Tool | Description |
|------|-------------|
| `search_posts(query, limit)` | Semantic search across all posts and drafts |
| `list_posts()` | List every HTML file in the export |
| `read_post(filename)` | Read the full text of a specific post |

## CLI Reference

```bash
python build_index.py                # full re-index (wipes and rebuilds)
python build_index.py --incremental  # only index new/changed files
python build_index.py --audit        # print index stats
```

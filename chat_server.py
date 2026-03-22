"""
chat_server.py — Web-based RAG chatbot for Lori's Medium blog.

Runs a local web server with a chat UI. Retrieves relevant article chunks
from ChromaDB, then streams a response from your chosen LLM provider.

Usage:
    python chat_server.py             # http://localhost:8000
    python chat_server.py --port 8080

Config via .env:
    LLM_PROVIDER    = claude | openai | gemini   (default: claude)
    ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY
    CLAUDE_MODEL    = claude-sonnet-4-6           (optional)
    OPENAI_MODEL    = gpt-4o-mini                 (optional)
    GEMINI_MODEL    = gemini-1.5-flash            (optional)
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Iterator

# ── Silence noisy library startup output ──────────────────────────────────────
_real_stdout = sys.stdout
sys.stdout = io.StringIO()
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import chromadb
from sentence_transformers import SentenceTransformer

sys.stdout = _real_stdout

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

ROOT       = Path(__file__).resolve().parent
DB_DIR     = ROOT / "vector_db"
COLLECTION = "blog_posts"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Lori's Blog Chat")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── RAG ───────────────────────────────────────────────────────────────────────

_model      = None
_collection = None


def _rag():
    global _model, _collection
    if _model is None:
        sys.stderr.write("⏳ Loading embedding model…\n")
        _save = sys.stdout
        sys.stdout = io.StringIO()
        _model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        sys.stdout = _save

        client = chromadb.PersistentClient(path=str(DB_DIR))
        try:
            _collection = client.get_collection(name=COLLECTION)
        except Exception:
            _collection = client.create_collection(name=COLLECTION)

        sys.stderr.write(f"✅ Ready — {_collection.count()} chunks in index\n")
    return _model, _collection


def retrieve(query: str, limit: int = 5) -> list[dict]:
    """Return the most relevant article chunks for query."""
    model, col = _rag()
    count = col.count()
    if count == 0:
        return []
    embedding = model.encode(query).tolist()
    results = col.query(query_embeddings=[embedding], n_results=min(limit, count))
    chunks = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        chunks.append({
            "content": doc,
            "title":   meta.get("title", ""),
            "url":     meta.get("url", ""),
        })
    return chunks


# ── LLM providers ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a helpful assistant for exploring Lori's Medium blog (https://chud-lori.medium.com/).

Use the provided article excerpts as your primary source when answering.
Cite the article title(s) you draw from in your response.
If a question is not covered by the excerpts, say so honestly rather than guessing.
Answer in the same language as the user's question (Indonesian or English).\
"""


def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(No relevant articles found in the index.)"
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(f"[{i}] {c['title']}\nURL: {c['url']}\n\n{c['content']}")
    return "\n\n---\n\n".join(parts)


def stream_claude(messages: list[dict], context: str) -> Iterator[str]:
    try:
        from anthropic import Anthropic
    except ImportError:
        yield "Error: `anthropic` package not installed. Run: pip install anthropic"
        return
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        yield "Error: ANTHROPIC_API_KEY not set in .env"
        return

    client = Anthropic(api_key=key)
    model  = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    system = f"{SYSTEM_PROMPT}\n\n<articles>\n{context}\n</articles>"

    with client.messages.stream(
        model=model,
        max_tokens=2048,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


def stream_openai(messages: list[dict], context: str) -> Iterator[str]:
    try:
        from openai import OpenAI
    except ImportError:
        yield "Error: `openai` package not installed. Run: pip install openai"
        return
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        yield "Error: OPENAI_API_KEY not set in .env"
        return

    client = OpenAI(api_key=key)
    model  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    all_messages = [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n<articles>\n{context}\n</articles>"},
        *messages,
    ]
    stream = client.chat.completions.create(
        model=model,
        messages=all_messages,
        max_tokens=2048,
        stream=True,
    )
    for chunk in stream:
        text = chunk.choices[0].delta.content
        if text:
            yield text


def stream_gemini(messages: list[dict], context: str) -> Iterator[str]:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        yield "Error: `google-genai` package not installed. Run: pip install google-genai"
        return
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        yield "Error: GOOGLE_API_KEY not set in .env"
        return

    client     = genai.Client(api_key=key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
    system     = f"{SYSTEM_PROMPT}\n\n<articles>\n{context}\n</articles>"

    # Build contents list from full history
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=2048,
    )

    for chunk in client.models.generate_content_stream(
        model=model_name, contents=contents, config=config
    ):
        if chunk.text:
            yield chunk.text


PROVIDERS = {
    "claude": stream_claude,
    "openai": stream_openai,
    "gemini": stream_gemini,
}


# ── API ───────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role: "user"|"assistant", content: "..."}]


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.post("/chat")
async def chat(req: ChatRequest):
    provider_name = os.getenv("LLM_PROVIDER", "claude").lower()
    stream_fn = PROVIDERS.get(provider_name)
    if stream_fn is None:
        raise HTTPException(400, f"Unknown LLM_PROVIDER '{provider_name}'. Choose: claude, openai, gemini")

    chunks  = retrieve(req.message)
    context = _format_context(chunks)
    messages = req.history + [{"role": "user", "content": req.message}]

    def generate():
        sources = [{"title": c["title"], "url": c["url"]} for c in chunks]
        yield f"data: {json.dumps({'sources': sources})}\n\n"
        try:
            for text in stream_fn(messages, context):
                yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/status")
async def status():
    _, col = _rag()
    return {
        "chunks":   col.count(),
        "provider": os.getenv("LLM_PROVIDER", "claude"),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Lori's Blog Chat Server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    provider = os.getenv("LLM_PROVIDER", "claude")
    print(f"\n🌐  Chat → http://{args.host}:{args.port}")
    print(f"    Provider : {provider}")
    print(f"    Press Ctrl+C to stop\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

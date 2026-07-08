"""
rag-ingest — cluster-native RAG ingestion service.

Accepts a file upload, converts via markitdown-proxy (PDF→Docling,
Office→MarkItDown), splits at French insurance structural boundaries,
embeds with bge-m3 via Ollama, and inserts directly into ragdb.

All services are reached via their in-cluster DNS names — no port-forwards,
no local dependencies.

API
---
  POST /ingest
    file        multipart file upload (PDF, DOCX, XLSX, PPTX, HTML, MD, TXT)
    collection  Open WebUI Knowledge Base UUID
    source      human-readable document name  (e.g. "Contrat RC Pro 2026")
    doc_type    policy | endorsement | annexe | regulatory | tariff | internal

  GET  /health
  GET  /ready
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import psycopg2
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from openai import OpenAI

PROXY_URL        = os.environ["PROXY_URL"]         # markitdown-proxy.ai.svc.cluster.local:8000
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]  # http://litellm.ai.svc.cluster.local:4000
LITELLM_API_KEY  = os.environ["LITELLM_API_KEY"]
PG_HOST          = os.environ["PG_HOST"]           # postgresql-ai.ai.svc.cluster.local
PG_PORT          = int(os.getenv("PG_PORT", "5432"))
PG_DB            = os.getenv("PG_DB", "ragdb")
PG_USER          = os.getenv("PG_USER", "aiplatform")
PG_PASSWORD      = os.environ["PG_PASSWORD"]
EMBED_MODEL      = "text-embedding-3-small"

_embed_client = OpenAI(api_key=LITELLM_API_KEY, base_url=f"{LITELLM_BASE_URL}/v1")

MAX_CHUNK_CHARS = 2000

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="rag-ingest")

# ── Structure detection ────────────────────────────────────────────────────────

HEADING_RE = re.compile(
    r'^(?:#{1,4}\s+)?'
    r'(Article|Chapitre|Titre|Annexe|Section|Garantie|Disposition'
    r'|ARTICLE|CHAPITRE|TITRE|ANNEXE|SECTION)'
    r'(?:\s+[\dA-Za-z]+(?:[.\-]\d+)*)?'
    r'(?:\s*[-–—:]\s*(.+))?',
    re.IGNORECASE,
)
MD_HEADING_RE = re.compile(r'^#{1,3}\s+.+')


def chunk_by_structure(markdown: str, source: str, doc_type: str) -> list[dict]:
    lines = markdown.splitlines()
    sections: list[tuple[str, str]] = []
    current_heading = "Préambule"
    current_lines: list[str] = []

    for line in lines:
        is_heading = HEADING_RE.match(line.strip()) or (
            MD_HEADING_RE.match(line) and len(line.strip()) > 4
        )
        if is_heading and current_lines:
            sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line.strip().lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    chunks: list[dict] = []
    for heading, text in sections:
        if not text:
            continue
        if len(text) <= MAX_CHUNK_CHARS:
            chunks.append(_make_chunk(text, heading, source, doc_type))
        else:
            paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
            buffer = ""
            for para in paragraphs:
                if len(buffer) + len(para) + 2 <= MAX_CHUNK_CHARS:
                    buffer = (buffer + "\n\n" + para).strip()
                else:
                    if buffer:
                        chunks.append(_make_chunk(buffer, heading, source, doc_type))
                    buffer = para
            if buffer:
                chunks.append(_make_chunk(buffer, heading, source, doc_type))

    return chunks


def _make_chunk(text: str, section: str, source: str, doc_type: str) -> dict:
    article_match = re.search(
        r'(Article|Annexe|Section|Chapitre)\s+([\dA-Za-z]+(?:[.\-]\d+)*)',
        section, re.IGNORECASE,
    )
    metadata: dict = {"document_type": doc_type, "section": section, "source": source}
    if article_match:
        metadata["article"] = article_match.group(0)
    return {"text": text, "metadata": metadata}


# ── Embedding ──────────────────────────────────────────────────────────────────

EMBED_BATCH_SIZE = 64  # max texts per LiteLLM→Ollama call; keeps payload < ~128KB

def embed_batch(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        resp = _embed_client.embeddings.create(model=EMBED_MODEL, input=batch)
        batch_vecs = [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        vectors.extend(batch_vecs)
    return vectors


# ── Storage ────────────────────────────────────────────────────────────────────

def store_chunks(chunks: list[dict], collection: str) -> int:
    texts = [c["text"] for c in chunks]
    log.info("  embedding %d chunks in batches of %d …", len(texts), EMBED_BATCH_SIZE)
    vectors = embed_batch(texts)

    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
    )
    cur = conn.cursor()
    for chunk, vector in zip(chunks, vectors):
        cur.execute(
            """
            INSERT INTO document_chunk (id, collection_name, text, vector, vmetadata)
            VALUES (%s, %s, %s, %s::vector, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (str(uuid.uuid4()), collection, chunk["text"],
             json.dumps(vector), json.dumps(chunk["metadata"])),
        )
    conn.commit()
    cur.close()
    conn.close()
    return len(chunks)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    return {"status": "ready"}


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    collection: str = Form(...),
    source: str = Form(...),
    doc_type: str = Form("policy"),
):
    filename = file.filename or "upload"
    content = await file.read()
    log.info("ingest  file=%s  collection=%s  doc_type=%s  bytes=%d",
             filename, collection, doc_type, len(content))

    # 1. Convert via markitdown-proxy (PDF→Docling, Office→MarkItDown)
    suffix = Path(filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            resp = requests.post(
                f"{PROXY_URL}/v1/convert/file",
                files={"file": (filename, f, file.content_type or "application/octet-stream")},
                timeout=300,
            )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Conversion failed: {exc}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    data = resp.json()
    markdown = data.get("document", {}).get("md_content") or data.get("content", "")
    if not markdown:
        raise HTTPException(status_code=422, detail="Proxy returned empty markdown")
    log.info("  converted → %d chars", len(markdown))

    # 2. Chunk by structure
    chunks = chunk_by_structure(markdown, source, doc_type)
    log.info("  chunked  → %d structural chunks", len(chunks))

    # 3. Embed + store — run in a thread so the event loop stays free for health probes
    try:
        stored = await asyncio.to_thread(store_chunks, chunks, collection)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Storage failed: {exc}")
    log.info("  stored   → %d chunks in ragdb collection %s", stored, collection)

    return JSONResponse({
        "status": "ok",
        "file": filename,
        "collection": collection,
        "chunks_stored": stored,
        "metadata_sample": chunks[0]["metadata"] if chunks else {},
    })

# minicloud-rag-ingest

[![CI](https://github.com/andrelair-platform/minicloud-rag-ingest/actions/workflows/ci.yml/badge.svg)](https://github.com/andrelair-platform/minicloud-rag-ingest/actions/workflows/ci.yml)
[![Supply chain: cosign](https://img.shields.io/badge/supply%20chain-cosign%20signed-green)](https://github.com/sigstore/cosign)
[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-teal)](https://fastapi.tiangolo.com)

> A cluster-native RAG ingestion pipeline deployed as a Kubernetes service. Accepts any document, converts it to Markdown, splits it on French insurance structural boundaries, embeds each chunk, and inserts it directly into a pgvector knowledge base — with no local tools required.

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [API](#api)
- [Document types](#document-types)
- [Chunking logic](#chunking-logic)
- [Running locally](#running-locally)
- [Ingesting a document from the Mac](#ingesting-a-document-from-the-mac)
- [CI/CD pipeline](#cicd-pipeline)
- [Environment variables](#environment-variables)
- [Security](#security)

---

## Why this exists

The Open WebUI paperclip upload path converts and embeds documents inside the UI container using whatever embedding model is configured globally. This creates three problems for a domain-specific French insurance RAG use case:

1. **No structural awareness** — documents are split by character count, not by article or section boundaries. A clause that spans two chunks loses its legal context.
2. **No metadata tagging** — chunks have no `doc_type`, `section`, or `article` fields, making targeted retrieval impossible.
3. **Mac-side dependency** — running the ingestion pipeline locally requires local Python, local DB access, and local credentials.

`rag-ingest` solves all three. It runs in the `ai` namespace alongside the rest of the platform, accepts a simple `multipart/form-data` POST, and handles the entire pipeline in-cluster: convert → chunk → embed → store.

---

## Architecture

```
Client (curl / CI / port-forward)
        │
        │  POST /ingest  (multipart: file + collection + source + doc_type)
        ▼
┌─────────────────────────────────────────────────────────┐
│                     rag-ingest                          │
│  FastAPI · python:3.12-slim · UID 1000 · port 8001      │
│                                                         │
│  1. Forward file to markitdown-proxy                    │
│     ├─ .pdf / images  →  Docling (port 5001)            │
│     └─ .docx / .xlsx / .pptx / .html  →  MarkItDown    │
│                                                         │
│  2. Structure-aware chunker                             │
│     Splits on: Article / Chapitre / Titre / Annexe      │
│     / Section / Garantie / Disposition + Markdown h1–h3 │
│     Oversized sections split further by paragraph       │
│     Metadata per chunk: doc_type, section, article,     │
│     source                                              │
│                                                         │
│  3. Embed via LiteLLM                                   │
│     Model: text-embedding-3-small (1536-dim)            │
│     Batch size: 64 chunks per call                      │
│                                                         │
│  4. INSERT into ragdb (pgvector)                        │
│     Table: document_chunk                               │
│     Index: HNSW (vector) + GIN FTS (french dictionary)  │
└─────────────────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
markitdown-proxy            postgresql-ai (ragdb)
ai namespace · port 8000    ai namespace · port 5432
```

**In-cluster DNS names used at runtime:**

| Dependency | Address |
|---|---|
| markitdown-proxy | `http://markitdown-proxy.ai.svc.cluster.local:8000` |
| LiteLLM (embeddings) | `http://litellm.ai.svc.cluster.local:4000` |
| PostgreSQL / ragdb | `postgresql-ai.ai.svc.cluster.local:5432` |

---

## API

### `POST /ingest`

Ingest a document into the pgvector knowledge base.

```
Content-Type: multipart/form-data

file        required   Document to ingest
collection  required   Open WebUI Knowledge Base UUID (collection_name in ragdb)
source      required   Human-readable document title  (e.g. "Contrat RC Pro 2026")
doc_type    optional   Document type tag (default: policy)
```

**Response (200):**

```json
{
  "status": "ok",
  "file": "contrat-rc-pro.pdf",
  "collection": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "chunks_stored": 47,
  "metadata_sample": {
    "document_type": "policy",
    "section": "Article 3 — Garanties incluses",
    "article": "Article 3",
    "source": "Contrat RC Pro 2026"
  }
}
```

**Supported file formats:**

| Format | Conversion path |
|---|---|
| `.pdf` | markitdown-proxy → Docling OCR |
| `.png` `.jpg` `.jpeg` `.tiff` `.bmp` `.gif` `.webp` | markitdown-proxy → Docling OCR |
| `.docx` `.xlsx` `.pptx` `.html` | markitdown-proxy → MarkItDown |
| `.txt` `.md` | markitdown-proxy → MarkItDown passthrough |

### `GET /health`

Returns `{"status": "ok"}`. Used by Kubernetes liveness probe.

### `GET /ready`

Returns `{"status": "ready"}`. Used by Kubernetes readiness probe.

---

## Document types

The `doc_type` field is stored in each chunk's metadata and can be used for filtered retrieval:

| Value | Description |
|---|---|
| `policy` | Main insurance contract (default) |
| `endorsement` | Contract amendment or rider |
| `annexe` | Schedule or appendix |
| `regulatory` | Regulatory filing (ACPR, Solvency II) |
| `tariff` | Premium rate schedule |
| `internal` | Internal procedure or note |

---

## Chunking logic

The chunker is designed for French legal and insurance documents. It does **not** split by a fixed character count. It first splits at structural headings, then falls back to paragraph boundaries if a section exceeds 2000 characters.

**Heading patterns recognised:**

```
Article / Chapitre / Titre / Annexe / Section / Garantie / Disposition
ARTICLE / CHAPITRE / ...    (upper-case variants)
Markdown h1–h3 headings (## ...)
```

Each chunk carries:

```json
{
  "metadata": {
    "document_type": "policy",
    "section": "Article 12 — Exclusions de garantie",
    "article": "Article 12",
    "source": "Police Multirisque Immeuble 2026"
  }
}
```

This metadata is stored in the `vmetadata` JSONB column of `document_chunk` and is searchable via Open WebUI's hybrid search (BM25 + HNSW vector).

---

## Running locally

The service is designed to run in-cluster and depends on three in-cluster services. The only way to run it locally is via `kubectl port-forward` on each dependency.

```bash
# 1. Port-forward all dependencies
kubectl --context minicloud port-forward -n ai svc/markitdown-proxy 8000:8000 &
kubectl --context minicloud port-forward -n ai svc/litellm 4000:4000 &
kubectl --context minicloud port-forward -n ai svc/postgresql-ai 5432:5432 &

# 2. Run the service with env vars pointed at localhost
PROXY_URL=http://localhost:8000 \
LITELLM_BASE_URL=http://localhost:4000 \
LITELLM_API_KEY=<litellm-master-key> \
PG_HOST=localhost \
PG_PASSWORD=<ragdb-password> \
uvicorn main:app --port 8001 --reload
```

---

## Ingesting a document from the Mac

The quickest path — no local Python required:

```bash
# 1. Port-forward the rag-ingest service
kubectl --context minicloud port-forward -n ai svc/rag-ingest 8001:8001 &

# 2. POST the document
curl -s -X POST http://localhost:8001/ingest \
  -F "file=@/path/to/document.pdf" \
  -F "collection=<OPEN_WEBUI_KNOWLEDGE_BASE_UUID>" \
  -F "source=My Document Title" \
  -F "doc_type=policy" | python3 -m json.tool

# 3. Kill the port-forward
kill %1
```

The `collection` UUID is the Knowledge Base UUID from Open WebUI (Settings → Knowledge → copy the UUID from the URL or the collection card).

---

## CI/CD pipeline

Every push to `main` triggers `.github/workflows/ci.yml`:

```
push to main
    │
    ├─ 1. Connect to Tailscale tailnet (reach Harbor registry directly over LAN)
    ├─ 2. Trust minicloud self-signed CA (Docker daemon + cosign + crane)
    ├─ 3. docker build → push to harbor.10.0.0.200.nip.io/library/rag-ingest:<sha>-amd64
    ├─ 4. cosign sign (keyless — GitHub OIDC → Sigstore Fulcio)
    └─ 5. GPG-signed commit to minicloud-gitops bumping manifests/ai/11-rag-ingest.yaml
              └─ ArgoCD webhook → rolling update in ai namespace
```

**Branch behaviour:**

| Branch | Image tag | Cosign signed | GitOps bump |
|---|---|---|---|
| `main` | `<sha>-amd64` | yes | yes — `manifests/ai/11-rag-ingest.yaml` |
| `staging` | `staging-<sha>-amd64` | yes | no |
| `dev` | `dev-<sha>-amd64` | no | no |

**Required repository secrets:**

| Secret | Purpose |
|---|---|
| `TAILSCALE_AUTH_KEY` | Ephemeral auth key to join the Tailscale tailnet |
| `MINICLOUD_CA_CERT` | Self-signed CA PEM — lets Docker and cosign trust Harbor TLS |
| `HARBOR_USER` | Harbor registry username |
| `HARBOR_PASSWORD` | Harbor registry password |
| `GITOPS_TOKEN` | GitHub PAT (`repo` scope) for committing to `minicloud-gitops` |
| `GPG_PRIVATE_KEY` | Armored GPG private key for signing gitops commits (key `FD6D39D681DEFA34`) |

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PROXY_URL` | yes | — | markitdown-proxy base URL |
| `LITELLM_BASE_URL` | yes | — | LiteLLM base URL |
| `LITELLM_API_KEY` | yes | — | LiteLLM API key |
| `PG_HOST` | yes | — | PostgreSQL host |
| `PG_PASSWORD` | yes | — | PostgreSQL password for `aiplatform` user |
| `PG_PORT` | no | `5432` | PostgreSQL port |
| `PG_DB` | no | `ragdb` | Database name |
| `PG_USER` | no | `aiplatform` | Database user |

All required variables are injected at runtime via a Kubernetes Secret managed by External Secrets Operator (ESO), pulling from HashiCorp Vault. Nothing is hardcoded.

---

## Security

- **Non-root runtime** — image runs as UID 1000 (`appuser`), no shell
- **Supply chain** — every `main` image is Cosign-signed (keyless) and the gitops bump commit is GPG-signed
- **Network isolation** — the `ai` namespace has default-deny ingress + egress NetworkPolicies; rag-ingest can only reach its declared dependencies (markitdown-proxy, LiteLLM, PostgreSQL) and is not exposed to the internet
- **No credentials in code** — all secrets injected via ESO + Vault at pod startup; zero hardcoded values
- **GitOps delivery** — no direct `kubectl apply`; all deploys go through ArgoCD with audit trail

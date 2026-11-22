# RAG shared-service contract — multi-tenant ingest/retrieval + PII-gate-at-ingest

**Status:** Proposed · **Date:** 2026-11-22 · **Board:** AI Platform (#4) · **Service:** this repo (`minicloud-rag-ingest`)

> **This is the service contract for `rag-ingest` as a shared, multi-tenant platform capability.** It is
> the operational **how** under an already-Accepted strategy: the *Document Intelligence Spine* ADR
> (`minicloud-platform-docs/docs/architecture/adr/document-intelligence-spine.md`, AI Platform #4) decided *build document
> intelligence once as a shared horizontal spine; business domains MUST NOT re-implement RAG privately.*
> This doc defines the contract a product consumes — one collection per product, two endpoints — and the
> **PII-gate-at-ingest** that spine's GOVERN stage requires but that is **not yet wired in**.

## The problem it closes

"How do I avoid rebuilding the RAG pipeline for every product?" The spine strategy says *don't* — reuse the
live primitives (`markitdown-proxy` + Docling convert · this service for chunk/embed · LiteLLM embeddings ·
Qdrant vector store · Presidio PII). But today a product **can't cleanly consume** them because:

1. **No retrieval API.** This service exposes only `/ingest`. The agents (`minicloud-agent`,
   `crew-agent`) already call `RAG_INGEST_URL/query` — **which does not exist** → retrieval becomes each
   consumer's problem, inviting per-product re-implementation (the exact anti-pattern the spine forbids).
2. **No multi-tenant contract.** Collections exist (one per Open WebUI knowledge-base UUID) but there is
   no agreed *"one product = one collection + its metadata/access policy"* convention — so "reuse" is ad-hoc.
3. **Governance is not inherited.** The spine mandates PII governance in the pipeline, but **Presidio is
   wired only into the LiteLLM *query* path, not ingest** — so documents are chunked, embedded and stored
   in Qdrant **with PII in plaintext in the payload**. A product consuming the shared pipeline does **not**
   automatically get PII protection.

The fix for all three: make this service a **governed multi-tenant capability with a stable contract**, so
a product consumes it by providing *data + config*, never code.

## Canonical facts (verified 2026-11-22)

- **Vector store = Qdrant** (`qdrant.ai.svc:6333`, v1.11.0). Open WebUI (`VECTOR_DB=qdrant`) is the anchor
  consumer; the **deployed** `rag-ingest` image writes there (manifest env `QDRANT_URL`). ⚠️ **This repo's
  `main.py` + README still describe the rolled-back *pgvector* variant** (it crashlooped on `PG_HOST`
  because Open WebUI reads Qdrant) — **stale docs; Qdrant is canonical. Fixing them is tracked separately.**
- **Embeddings:** LiteLLM alias `embeddings` → `mistral-embed` (EU, 1024-dim, cosine).
- **Qdrant payload** already stores `{text, document_type, section, source, article}` per point → **payload
  filtering for classification/access is feasible** (Qdrant supports it natively).
- **Presidio** (`analyzer` + `anonymizer`, `ai` ns, :3000) is **deployed and production-used** — but as a
  LiteLLM `pre_call` guardrail on *queries* only; **not** on ingest.
- **Convert step:** `markitdown-proxy` (`POST /v1/convert/file`) → images to Docling, else MarkItDown →
  returns `{document:{md_content}}`.

## The contract — "one product = one collection"

A product gets RAG by **registering a collection and providing documents + config** — no pipeline code.

### Per-product collection manifest (the thin contract)
```yaml
collection: ktayl-claims-kb          # the tenant id (one per product/use-case)
owner: ktayl-claims                  # product / team that owns the corpus
classification: personal-data        # internal | personal-data | confidential (drives PII + access)
pii: true                            # run the PII gate at ingest (default: true)
access:                              # who may retrieve (deferred enforcement — see Right-sizing)
  roles: [claims-adjuster, claims-admin]
embedding: embeddings                # the LiteLLM alias (default: the platform standard)
```

### The two shared endpoints (the service contract)

| Endpoint | Who calls it | What it does |
|---|---|---|
| `POST /ingest` *(exists)* | any product / CronJob / Backstage action | `file + collection + source + doc_type` → convert → **PII-gate** → chunk → embed → upsert to the Qdrant collection |
| `POST /query` *(**to build** — the gap)* | any product / agent / assistant | `{query, collection, top_k, filter?}` → embed question → Qdrant search **scoped to that collection** (+ access filter) → return chunks + **provenance** (source doc, section) |

`/query` is the missing half that makes retrieval a *platform* concern instead of each product's — and it
unbreaks the agents that already call it.

## Governance control #1 — PII-gate-at-ingest (grounded build)

The spine's GOVERN stage, made real at the one place text is plaintext before it becomes a vector.

**Where it slots in** (the **Qdrant** code path — NOT the stale pgvector `main.py`): the chunking step,
where each chunk's text exists plainly **before** `embed_batch`. In `_make_chunk(text, …)` / the
`chunk_by_structure` loop, **before** the chunk is embedded/stored:

```
for each chunk text:
    if collection.pii:                       # from the manifest (default true)
        analysis  = POST presidio-analyzer:3000/analyze     {text, language, entities}
        redacted  = POST presidio-anonymizer:3000/anonymize {text, analyzer_results}
        chunk.text      = redacted.text      # embed + store the REDACTED text
        chunk.metadata += {pii_detected: bool, pii_entities: [...]}   # audit trail
    # → embed(chunk.text) → upsert(collection, payload={text, **metadata})
```

Why here: metadata (doc_type/section/source) is preserved; only the served text is redacted; redacted text
is what gets embedded **and** what sits in the Qdrant payload. Reuses the **already-deployed** Presidio —
the only new deployment artefact is a NetworkPolicy allowing `rag-ingest → presidio` (Presidio ingress is
currently `litellm`-only). **That netpol is the one change that lands in `minicloud-gitops`, not here.**

## Right-sizing (need-first — which parts now vs. deferred)

| Piece | Cost | Verdict |
|---|---|---|
| **PII-gate-at-ingest** (control #1) | medium (wire Presidio into ingest + 1 netpol) | **NOW** — the real compliance exposure (PII in plaintext vectors) |
| **Catalogue the collections** (register each as a governed dataset: owner/classification/retention) | low | **NOW** — closes "nobody knows what's in the vector store" |
| **`/query` endpoint** (shared retrieval + provenance) | medium | **NOW/SOON** — unbreaks agents; makes retrieval a platform concern |
| **Ingest freshness/failure alert** | low | **SOON** — mirror the data-platform CronJob-staleness pattern |
| **Entitlement-filtered retrieval** (enforce `access.roles` by filtering Qdrant payload per the caller's identity) | high (identity → per-chunk tags → filtered queries) | **DEFER** — trigger: real employees + sensitive corpora at volume |
| **Per-product isolated instance** (own pipeline via a Backstage scaffolder, config-not-code) | high | **DEFER** — only for a corpus with hard residency/scale needs; the shared multi-tenant service is the default |

## Consequences

- **Positive:** a product adopts RAG in ~a day (register a collection + POST its docs), not by forking a
  pipeline; governance (PII, classification, provenance) is built **once** in this service and **every**
  product inherits it; directly realises the spine's "build once, many consumers."
- **Accepted trade-offs:** this service becomes a critical dependency needing clear API/SLA ownership
  (AI Platform #4); entitlement-filtered retrieval is deferred, so until then **retrieval is
  collection-scoped but not per-user role-filtered** — acceptable while the user base is small, a
  documented gap (not a silent one). Escalate when the trigger fires.

## Related

- Parent strategy (ADR): `minicloud-platform-docs/docs/architecture/adr/document-intelligence-spine.md` (INGEST→…→GOVERN), also on
  the central docs Architecture Hub.
- Boundary rule: `platform-vs-information-system.md` (RAG = a platform capability the IS consumes).
- Capability register: CAP-130/132/248 (RAG / vector / publication), CAP-317 (PII masking).
- Follow-up build issues (this repo): the `/query` endpoint · PII-gate-at-ingest.

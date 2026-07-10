# Contributing

## Branch strategy

| Branch | Protection | CI output | GitOps update |
|---|---|---|---|
| `main` | PR required + GPG-signed commits | `<sha>-amd64` — cosign-signed | bumps `manifests/ai/11-rag-ingest.yaml` |
| `staging` | PR required | `staging-<sha>-amd64` — cosign-signed | none |
| `dev` | open push | `dev-<sha>-amd64` | none |

## Commit style

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add support for .epub ingestion
fix: handle empty markdown from markitdown-proxy gracefully
perf: increase embed batch size from 64 to 128
ci: pin cosign installer to v3.5
chore: bump fastapi to 0.116
```

## Dev workflow

```bash
# Dependencies (requires local PostgreSQL + markitdown-proxy + LiteLLM via port-forward)
pip install fastapi uvicorn requests psycopg2-binary python-multipart openai

# Run with port-forwarded in-cluster services
PROXY_URL=http://localhost:8000 \
LITELLM_BASE_URL=http://localhost:4000 \
LITELLM_API_KEY=<key> \
PG_HOST=localhost \
PG_PASSWORD=<password> \
uvicorn main:app --reload --port 8001

# Quick smoke test
curl -s http://localhost:8001/health
curl -s http://localhost:8001/ready
```

## PR checklist

Before opening a PR against `main` or `staging`:

- [ ] `GET /health` and `GET /ready` return 200 locally
- [ ] `POST /ingest` tested with a real PDF and a real `.docx`
- [ ] No secrets or credentials added to any file
- [ ] If the embedding model or chunking logic changed, note the impact on existing ragdb vectors (they may need to be re-ingested)
- [ ] Commit messages follow Conventional Commits

## Code standards

- Keep `main.py` as a single file — the service is intentionally minimal
- All configuration via environment variables only; no config files
- No Co-Authored-By lines — commits represent the owner's portfolio work

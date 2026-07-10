---
name: Bug report
about: Report a broken or unexpected behaviour
labels: bug
---

## Describe the bug

<!-- A clear description of what is broken. -->

## Steps to reproduce

1. POST /ingest with file type: '...'
2. See error: '...'

## Expected behaviour

<!-- What should have happened? -->

## Environment

| Field | Value |
|---|---|
| Image tag | <!-- from manifests/ai/11-rag-ingest.yaml --> |
| File format | <!-- .pdf / .docx / .xlsx / ... --> |
| File size | |
| ArgoCD app status | <!-- Synced / OutOfSync / Degraded --> |
| markitdown-proxy status | <!-- Running / CrashLoopBackOff --> |

## Logs

<!-- kubectl logs -n ai -l app=rag-ingest --tail=50 -->

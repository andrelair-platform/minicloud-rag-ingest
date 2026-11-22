"""minicloud-rag-ingest — ingestion pipeline (diagram-as-code).

mingrammer `diagrams` + Graphviz. Self-contained; lives with the service. Shows the real in-cluster
pipeline: convert → chunk → embed → UPSERT to Qdrant, plus the retrieval consumer (Open WebUI).
Facts from main.py (deployed commit 6d249b5, Qdrant) + the manifest (minicloud-gitops
platform/ai/litellm/11-rag-ingest.yaml). Light/high-contrast theme per diagrams-as-code.md.

Run:  python rag_ingest_pipeline.py   (needs: pip install -r requirements.txt ; brew/apt install graphviz)
Out:  ../architecture/img/rag-ingest-pipeline.{png,svg}
"""
from __future__ import annotations

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.custom import Custom
from diagrams.onprem.client import Client
from diagrams.onprem.compute import Server
from diagrams.programming.framework import Fastapi

# ------------------------------------------------------------------ theme (inlined, light house style)
BG, TEXT, MUTED = "#ffffff", "#0f172a", "#64748b"
BLUE, GREEN, VIOLET, AMBER = "#2563eb", "#059669", "#7c3aed", "#d97706"
ICONS = Path(__file__).resolve().parent / "icons"
OUT = Path(__file__).resolve().parent.parent / "architecture" / "img"
OUT.mkdir(parents=True, exist_ok=True)


def node(cls, label, icon=None):
    """Vendored PNG from icons/ if present, else the closest semantic built-in. Label is authoritative."""
    p = ICONS / icon if icon else None
    return Custom(label, str(p)) if p and p.exists() else cls(label)


def cl(pencolor=BLUE):
    return {"bgcolor": "#f1f5f9", "pencolor": pencolor, "fontcolor": pencolor,
            "fontname": "Helvetica-Bold", "fontsize": "14", "style": "rounded", "penwidth": "1.8"}


GRAPH = {"bgcolor": BG, "fontcolor": TEXT, "fontname": "Helvetica-Bold", "fontsize": "22",
         "pad": "0.6", "nodesep": "0.55", "ranksep": "1.1", "splines": "spline"}
NODE = {"fontcolor": TEXT, "fontname": "Helvetica", "fontsize": "12"}
EDGE = {"color": MUTED, "fontcolor": TEXT, "fontname": "Helvetica-Bold", "fontsize": "11", "penwidth": "1.8"}


def principal(label, color=BLUE):
    return Edge(label=label, color=color, fontcolor=TEXT, penwidth="2.0")


def backing(label, color=MUTED):
    return Edge(label=label, color=color, fontcolor=MUTED, style="dashed", penwidth="1.4")


with Diagram(
    "rag-ingest — RAG ingestion pipeline (convert → chunk → embed → Qdrant)",
    filename=str(OUT / "rag-ingest-pipeline"),
    outformat=["png", "svg"],
    show=False,
    direction="LR",
    graph_attr=GRAPH,
    node_attr=NODE,
    edge_attr=EDGE,
):
    client = node(Client, "Client\n(curl · CI · port-forward)")

    with Cluster("ai namespace (default-deny netpols)", graph_attr=cl(BLUE)):
        svc = node(Fastapi, "rag-ingest\nFastAPI · :8001")

        with Cluster("Document conversion", graph_attr=cl(AMBER)):
            proxy = node(Server, "markitdown-proxy\n:8000")
            docling = node(Server, "Docling\nOCR · images only")

        with Cluster("Embeddings", graph_attr=cl(GREEN)):
            litellm = node(Server, "LiteLLM\nembeddings → mistral-embed (EU, 1024-d)", icon="litellm.png")

        with Cluster("Vector store", graph_attr=cl(VIOLET)):
            qdrant = node(Server, "Qdrant\ncollection = KB UUID · cosine", icon="qdrant.png")

        webui = node(Server, "Open WebUI\nhybrid search (retrieval)", icon="openwebui.png")

    # principal flow: the ingest pipeline
    client >> principal("POST /ingest\n(file · collection · doc_type)") >> svc
    svc >> principal("1. converts → Markdown", AMBER) >> proxy
    proxy >> backing("OCRs scanned images") >> docling
    svc >> principal("2. chunks (FR structure)\n3. embeds", GREEN) >> litellm
    svc >> principal("4. upserts\n(vector + payload)", VIOLET) >> qdrant
    # retrieval consumer
    webui >> backing("reads / hybrid-searches") >> qdrant

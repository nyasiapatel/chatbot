"""
ingest.py — Document ingestion for the IIBX RAG pipeline.

Run this once (and again whenever you add new documents):

    python ingest.py

What it does:
  1. Loads every IIBX PDF found in ./data/pdfs/ (using PyPDFLoader) — drop
     real IIBX PDFs (Trading Guidelines, Byelaws, Circulars, etc.) in there
     and re-run this script to add them to the knowledge base.
  2. Loads ./data/iibx_scrape.json (a scrape of iibx.co.in) as an interim
     corpus until real PDFs are available.
  3. Chunks every document with a RecursiveCharacterTextSplitter
     (chunk_size=800, chunk_overlap=120 — tuned for short-to-medium IIBX
     FAQ/contract-spec style passages).
  4. Embeds every chunk locally (sentence-transformers all-MiniLM-L6-v2)
     and stores it in a persistent ChromaDB collection with metadata
     (document title + section/page), ready for cosine-similarity retrieval.
"""

import json
import os
import re

import chromadb
from chromadb.utils import embedding_functions
from langchain_text_splitters import RecursiveCharacterTextSplitter

try:
    from langchain_community.document_loaders import PyPDFLoader
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

from rag import DB_DIR, COLLECTION_NAME, EMBED_MODEL_NAME

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
JSON_SCRAPE_PATH = os.path.join(DATA_DIR, "iibx_scrape.json")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# Friendly document titles for known iibx.co.in URL slugs, so citations read
# like "IIBX — Regulation Overview" instead of a raw filename/slug.
URL_TITLE_MAP = {
    "about": "IIBX — About Us",
    "objective_roles": "IIBX — Objective & Roles",
    "regulations": "IIBX — Regulation Overview",
    "IFSC_Authority": "IIBX — IFSCA Notifications",
    "iibx_regulations": "IIBX — Exchange Byelaws & Regulations",
    "spot": "IIBX — Spot Market Products",
    "gold995_specifications": "IIBX — Gold 995 Contract Specs",
    "goldmini999_specifications": "IIBX — Gold Mini 999 Contract Specs",
    "Gold9999_specifications": "IIBX — Gold 9999 T+0 Contract Specs",
    "Gold12_9999_specifications": "IIBX — Gold 12.5kg 9999 Contract Specs",
    "UAE_GD_GOLD_995": "IIBX — UAEGD Gold 995 Contract Specs",
    "UAEGD_GOLD_999": "IIBX — UAEGD Gold 999 Contract Specs",
    "UAEGDTRQ_GOLD_995": "IIBX — UAEGDTRQ Gold 995 Contract Specs",
    "Silver_Grains": "IIBX — Silver Grains Contract Specs",
    "knowledgecenter": "IIBX — Knowledge Center",
    "PressRelease": "IIBX — Press Releases",
    "index": "IIBX — Home / Announcements",
    "circulars_notifications": "IIBX — Circulars & Notifications",
    "marketturnover": "IIBX — Market Turnover",
    "dailymarketdata": "IIBX — Daily Market Data (Bhavcopy)",
    "event": "IIBX — Events",
    "Circular": "IIBX — Circulars",
    "faq": "IIBX — Official FAQ",
}


def _friendly_title(url: str) -> str:
    slug = re.sub(r"\.aspx$", "", url.rstrip("/").split("/")[-1])
    return URL_TITLE_MAP.get(slug, f"IIBX Website — {slug.replace('_', ' ').title()}")


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def ingest_json_scrape(collection) -> int:
    if not os.path.exists(JSON_SCRAPE_PATH):
        print(f"  (no JSON scrape found at {JSON_SCRAPE_PATH}, skipping)")
        return 0

    with open(JSON_SCRAPE_PATH, "r", encoding="utf-8") as f:
        entries = json.load(f)

    splitter = _splitter()
    ids, docs, metas = [], [], []

    # Track an occurrence counter per friendly title so repeated pages on the
    # same topic get distinct, human-readable section labels.
    section_counters: dict[str, int] = {}

    for i, entry in enumerate(entries):
        url = entry.get("url", "unknown")
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        title = _friendly_title(url)
        pieces = splitter.split_text(text)
        for piece in pieces:
            section_counters[title] = section_counters.get(title, 0) + 1
            ids.append(f"scrape-{i}-{section_counters[title]}")
            docs.append(piece)
            metas.append(
                {
                    "document": title,
                    "section": f"Excerpt {section_counters[title]}",
                    "source_url": url,
                    "source_type": "web",
                }
            )

    if docs:
        collection.upsert(ids=ids, documents=docs, metadatas=metas)
    print(f"  ingested {len(docs)} chunks from {len(entries)} scraped page(s)")
    return len(docs)


def ingest_pdfs(collection) -> int:
    if not PDF_SUPPORT:
        print(
            "  langchain_community is not installed — skipping PDF ingestion.\n"
            "  Install with: pip install langchain-community pypdf"
        )
        return 0

    os.makedirs(PDF_DIR, exist_ok=True)
    pdfs = [f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf")]
    if not pdfs:
        print(f"  no PDFs found in {PDF_DIR} — drop IIBX PDFs there and re-run to add them")
        return 0

    splitter = _splitter()
    ids, docs, metas = [], [], []

    for pdf_file in pdfs:
        path = os.path.join(PDF_DIR, pdf_file)
        loader = PyPDFLoader(path)
        pages = loader.load()
        doc_title = os.path.splitext(pdf_file)[0].replace("_", " ").strip()
        for page in pages:
            page_num = page.metadata.get("page", 0) + 1
            pieces = splitter.split_text(page.page_content)
            for j, piece in enumerate(pieces):
                if not piece.strip():
                    continue
                ids.append(f"pdf-{pdf_file}-{page_num}-{j}")
                docs.append(piece)
                metas.append(
                    {
                        "document": doc_title,
                        "section": f"Page {page_num}",
                        "source_url": pdf_file,
                        "source_type": "pdf",
                    }
                )

    if docs:
        collection.upsert(ids=ids, documents=docs, metadatas=metas)
    print(f"  ingested {len(docs)} chunks from {len(pdfs)} PDF file(s)")
    return len(docs)


def main():
    print("IIBX RAG ingestion")
    print(f"  embedding model : {EMBED_MODEL_NAME} (downloaded locally on first run)")
    print(f"  vector store    : {DB_DIR}")
    print(f"  chunk size      : {CHUNK_SIZE} chars, overlap {CHUNK_OVERLAP} chars\n")

    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBED_MODEL_NAME
    )
    client = chromadb.PersistentClient(path=DB_DIR)

    # Rebuild the collection from scratch each run, so re-ingestion never leaves stale/duplicate chunks behind.
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        COLLECTION_NAME,
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine"},
    )

    print("Ingesting PDFs...")
    pdf_count = ingest_pdfs(collection)
    print("Ingesting JSON scrape...")
    scrape_count = ingest_json_scrape(collection)

    total = pdf_count + scrape_count
    print(f"\n✅ Done. {total} chunks stored in collection '{COLLECTION_NAME}'.")
    if total == 0:
        print("⚠️  No documents were ingested — the assistant will answer with no retrieved context.")


if __name__ == "__main__":
    main()

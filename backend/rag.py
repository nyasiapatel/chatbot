"""
rag.py — Vector store access and retrieval for the IIBX RAG pipeline.

Storage:  ChromaDB (persistent, on-disk at ./chroma_db)
Embedding: sentence-transformers "all-MiniLM-L6-v2" (local, no API key needed)
Similarity: cosine (collection is created with metadata={"hnsw:space": "cosine"})

Ingestion happens separately via ingest.py — this module only *reads* the
collection at query time so the FastAPI server stays fast to start.
"""

import os

import chromadb
from chromadb.utils import embedding_functions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_NAME = "iibx_docs"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"

_client = None
_collection = None
_embedder = None


def get_embedder():
    """Lazily load the sentence-transformer embedding function (shared with guardrails.py)."""
    global _embedder
    if _embedder is None:
        _embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBED_MODEL_NAME
        )
    return _embedder


def get_collection():
    """Get (or lazily open) the persistent Chroma collection used for retrieval."""
    global _client, _collection
    if _collection is not None:
        return _collection

    _client = chromadb.PersistentClient(path=DB_DIR)
    try:
        _collection = _client.get_collection(
            COLLECTION_NAME, embedding_function=get_embedder()
        )
    except Exception:
        # Collection doesn't exist yet — caller should run ingest.py first.
        # We still create an empty one so the server doesn't crash; retrieval
        # will simply return no chunks until ingestion has been run.
        _collection = _client.create_collection(
            COLLECTION_NAME,
            embedding_function=get_embedder(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def retrieve_context(query: str, top_k: int = 4) -> list[dict]:
    """
    Retrieve the top-k most relevant chunks for `query` using cosine similarity.

    Returns a list of dicts: {"text", "document", "section", "score"}
    `score` is a cosine *similarity* (higher = more relevant), already
    converted from Chroma's cosine *distance*.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    results = collection.query(query_texts=[query], n_results=min(top_k, collection.count()))

    chunks = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for text, meta, dist in zip(docs, metas, dists):
        similarity = 1 - dist  # cosine distance -> cosine similarity
        chunks.append(
            {
                "text": text,
                "document": meta.get("document", "IIBX Document"),
                "section": meta.get("section", "General"),
                "score": similarity,
                "source_type": meta.get("source_type", "web"),
            }
        )
    return chunks


def ingest_text_to_temp_collection(text: str, session_id: str, filename: str) -> None:
    """
    Chunk a plain text string and embed it into a temporary per-session
    ChromaDB collection. Called when a user uploads a document.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=120,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    pieces = splitter.split_text(text)
    if not pieces:
        return

    client = chromadb.PersistentClient(path=DB_DIR)
    collection_name = f"doc_{session_id}"

    # Always start fresh
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass

    col = client.create_collection(
        collection_name,
        embedding_function=get_embedder(),
        metadata={"hnsw:space": "cosine"},
    )

    ids = [f"chunk-{i}" for i in range(len(pieces))]
    metas = [
        {
            "document": filename,
            "section": f"Excerpt {i + 1}",
            "source_type": "uploaded_doc",
        }
        for i in range(len(pieces))
    ]
    col.upsert(ids=ids, documents=pieces, metadatas=metas)


def retrieve_context_from_collection(query: str, session_id: str, top_k: int = 4) -> list[dict]:
    """
    Retrieve chunks from a temporary per-session collection (user-uploaded doc).
    Returns empty list if the collection doesn't exist.
    """
    client = chromadb.PersistentClient(path=DB_DIR)
    collection_name = f"doc_{session_id}"
    try:
        col = client.get_collection(collection_name, embedding_function=get_embedder())
    except Exception:
        return []

    if col.count() == 0:
        return []

    results = col.query(query_texts=[query], n_results=min(top_k, col.count()))

    chunks = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for text, meta, dist in zip(docs, metas, dists):
        similarity = 1 - dist
        chunks.append(
            {
                "text": text,
                "document": meta.get("document", "Uploaded Document"),
                "section": meta.get("section", "General"),
                "score": similarity,
                "source_type": "uploaded_doc",
            }
        )
    return chunks


def delete_temp_collection(session_id: str) -> None:
    """Delete the temporary ChromaDB collection for a session."""
    client = chromadb.PersistentClient(path=DB_DIR)
    try:
        client.delete_collection(f"doc_{session_id}")
    except Exception:
        pass

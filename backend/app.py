"""
app.py — FastAPI backend for the IIBX Assistant (Ollama / RAG version).

Endpoints:
  GET  /api/health    — basic liveness check
  POST /api/chat       — guardrail -> retrieve -> augment system prompt ->
                          stream tokens from Ollama -> emit sources at the end
  POST /api/feedback   — log thumbs up/down feedback to data/feedback.json

The frontend talks to this server instead of calling Ollama directly, so the
RAG pipeline and topic guardrail sit in front of every request. Model
discovery (GET /api/tags passthrough) and the basic Ollama "is it running"
status check still happen directly from the browser, unchanged from before.

Run with:
    uvicorn app:app --reload --port 8000
"""

import json
import os
import shutil
import time
import uuid
from typing import List, Optional

import requests
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from document_ingestion import extract_text
from guardrails import is_in_domain
from rag import retrieve_context, retrieve_context_from_collection, ingest_text_to_temp_collection, delete_temp_collection
from system_prompt import build_augmented_prompt

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_FILE = os.path.join(BASE_DIR, "data", "feedback.json")
UPLOAD_TMP_DIR = os.path.join(BASE_DIR, "data", "uploads_tmp")

HISTORY_TRIM_TURNS = 12  # keep conversation memory bounded, mirrors original trimming behaviour

REFUSAL_MESSAGE = (
    "I'm specialised in IIBX and Indian bullion markets — trading, membership, "
    "regulation, GIFT City, BDRs, CEPA and related topics. I'm not able to help "
    "with that, but I'd be glad to answer anything IIBX-related. For other "
    "queries, please reach out to IIBX directly via https://iibx.co.in."
)

app = FastAPI(title="IIBX Assistant Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only assistant; tighten this if you ever deploy publicly
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    query: str
    history: List[ChatMessage] = []
    top_k: int = 6
    doc_session_ids: Optional[List[str]] = None  # list of uploaded doc session IDs


class FeedbackRequest(BaseModel):
    query: str
    response: str
    rating: str  # "up" | "down"
    sources: Optional[List[dict]] = None


def _ndjson_line(obj: dict) -> bytes:
    """Encode one NDJSON line, matching Ollama's own streaming format so the
    existing frontend parser (which reads one JSON object per line) keeps
    working unchanged."""
    return (json.dumps(obj) + "\n").encode("utf-8")


def _format_sources(chunks: list[dict]) -> list[dict]:
    """De-duplicate retrieved chunks down to their source document/section,
    for the "Source: ..." citation line under the response bubble."""
    seen = set()
    sources = []
    for c in chunks:
        key = (c["document"], c["section"])
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "document": c["document"],
                "section": c["section"],
                "score": round(c["score"], 3),
                "source_type": c.get("source_type", "web"),
            }
        )
    return sources[:3]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/chat")
def chat(req: ChatRequest):
    def stream():
        # ── Guardrail: skip when user has uploaded documents ──
        has_docs = bool(req.doc_session_ids)
        if not has_docs and not is_in_domain(req.query, [m.dict() for m in req.history]):
            yield _ndjson_line({"message": {"content": REFUSAL_MESSAGE}})
            yield _ndjson_line({"done": True, "sources": [], "blocked": True})
            return

        # ── RAG retrieval ──
        if has_docs:
            # User uploaded docs — retrieve ONLY from those, ignore the main KB
            chunks = []
            for sid in req.doc_session_ids:
                chunks += retrieve_context_from_collection(req.query, sid, top_k=req.top_k)
            # Re-rank by score so best chunks from any file come first
            chunks.sort(key=lambda c: c["score"], reverse=True)
            chunks = chunks[:req.top_k * 2]  # keep top results across all files
        else:
            # No uploads — use the main IIBX knowledge base
            chunks = retrieve_context(req.query, top_k=req.top_k)

        # ── Build the augmented system prompt with retrieved context ──
        system_prompt = build_augmented_prompt(chunks)

        # ── Assemble messages, with bounded conversation memory ──
        trimmed_history = req.history[-HISTORY_TRIM_TURNS:]
        messages = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in trimmed_history]
        messages.append({"role": "user", "content": req.query})

        # ── Stream from Ollama, relaying tokens as they arrive ──
        try:
            with requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": req.model, "messages": messages, "stream": True, "keep_alive": "30m"},
                stream=True,
                timeout=120,
            ) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    content = parsed.get("message", {}).get("content")
                    if content:
                        yield _ndjson_line({"message": {"content": content}})

                    if parsed.get("done"):
                        yield _ndjson_line(
                            {"done": True, "sources": _format_sources(chunks), "blocked": False}
                        )
        except requests.exceptions.RequestException as e:
            yield _ndjson_line(
                {"message": {"content": f"\n\n⚠️ Backend could not reach Ollama: {e}"}}
            )
            yield _ndjson_line({"done": True, "sources": [], "blocked": False})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/upload-doc")
async def upload_doc(file: UploadFile = File(...)):
    """
    Accept a PDF, DOCX, or image file uploaded by the user.
    Extracts text, chunks + embeds it into a temporary ChromaDB collection,
    and returns a doc_session_id the frontend uses to scope RAG retrieval.
    """
    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)

    # Save uploaded file to a temp path
    safe_name = f"{uuid.uuid4()}_{file.filename}"
    tmp_path = os.path.join(UPLOAD_TMP_DIR, safe_name)
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Extract text using document_ingestion.py
        result = extract_text(tmp_path)
        text = result["text"]
        if not text.strip():
            return {"error": "Could not extract any text from this file."}, 400

        # Ingest into a fresh temporary collection keyed by session id
        doc_session_id = str(uuid.uuid4())
        doc_title = file.filename
        ingest_text_to_temp_collection(text, doc_session_id, doc_title)

        return {
            "doc_session_id": doc_session_id,
            "filename": file.filename,
            "method_used": result["method_used"],
            "char_count": len(text),
        }
    finally:
        # Clean up the raw uploaded file — we only need the vector store now
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.delete("/api/doc-session/{doc_session_id}")
def delete_doc_session(doc_session_id: str):
    """Delete the temporary ChromaDB collection for this upload session."""
    delete_temp_collection(doc_session_id)
    return {"status": "deleted"}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """Log thumbs up/down feedback. For now this writes to a local JSON file
    (and stdout) — swap this out for a real datastore later if needed."""
    entry = {
        "timestamp": time.time(),
        "query": req.query,
        "response": req.response,
        "rating": req.rating,
        "sources": req.sources or [],
    }
    print("[FEEDBACK]", json.dumps(entry, ensure_ascii=False))

    os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
    data = []
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            data = []
    data.append(entry)
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return {"status": "logged"}

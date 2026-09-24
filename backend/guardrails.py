"""
guardrails.py — Topic filter for the IIBX assistant.

Intercepts clearly off-topic queries BEFORE the LLM (or even the retriever)
is called, so we never spend a generation on something outside IIBX/bullion.

Two layers:
  1. Fast keyword match against a domain vocabulary (cheap, catches most cases)
  2. Embedding cosine-similarity fallback against a few "anchor" sentences
     describing the IIBX domain, for paraphrased questions that don't hit a
     keyword (e.g. "how do I become a member of the exchange in Gujarat?")
"""

import numpy as np

from rag import get_embedder

KEYWORDS = [
    "iibx", "bullion", "gold", "silver", "gift city", "ifsc", "ifsca",
    "bdr", "depository receipt", "trading member", "clearing member",
    "qualified jeweller", "qualified supplier", "trq", "cepa", "demat",
    "vault", "vaulting", "lbma", "uaegd", "spot contract", "futures",
    "settlement", "margin", "membership", "iidi", "kybd", "pcm", "tcm",
    "tsm", "lptm", "exchange", "precious metal", "troy ounce", "kyc",
    "aml", "regulator", "regulation", "circular", "import duty",
    "customs duty", "qccp", "bullion exchange", "uae", "tariff rate quota",
]

DOMAIN_ANCHORS = [
    "IIBX is India's international bullion exchange for trading gold and silver.",
    "Bullion Depository Receipts represent gold or silver held in IFSCA approved vaults.",
    "Trading members and clearing members participate in IIBX settlement and clearing.",
    "Qualified Jewellers import gold through IIBX under IFSCA regulations.",
    "IIBX trading hours, margins, and contract specifications for spot and futures bullion.",
]

SIMILARITY_THRESHOLD = 0.30

_anchor_embeddings = None


def _cosine(a, b) -> float:
    a, b = np.array(a), np.array(b)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _get_anchor_embeddings():
    global _anchor_embeddings
    if _anchor_embeddings is None:
        embedder = get_embedder()
        _anchor_embeddings = embedder(DOMAIN_ANCHORS)
    return _anchor_embeddings


def is_in_domain(query: str, history: list = None) -> bool:
    """Return True if `query` is plausibly about IIBX / bullion markets.
    
    If history is provided and non-empty, short follow-up messages (under 60
    chars with no topic keywords) are assumed to be in-context continuations
    and are allowed through automatically.
    """
    q = (query or "").lower().strip()
    if not q:
        return False

    # Layer 0 — follow-up heuristic: if we're mid-conversation and the query
    # is short and contains no topic signal of its own, trust that the user is
    # continuing the existing IIBX thread rather than pivoting to something
    # unrelated (e.g. "can you summarise that", "explain in one sentence",
    # "what does that mean", "elaborate on the last point").
    if history and len(history) >= 2 and len(q) < 80:
        CONTINUATION_SIGNALS = [
            "that", "this", "it", "the above", "previous", "last", "again",
            "more", "explain", "elaborate", "summarise", "summarize",
            "simpler", "shorter", "one sentence", "brief", "detail",
            "what does", "what is", "how does", "can you", "could you",
            "tell me", "give me", "show me", "list", "example",
        ]
        if any(sig in q for sig in CONTINUATION_SIGNALS):
            return True

    # Layer 1 — fast keyword match
    if any(kw in q for kw in KEYWORDS):
        return True

    # Layer 2 — embedding similarity fallback for paraphrased questions
    try:
        embedder = get_embedder()
        query_emb = embedder([q])[0]
        anchors = _get_anchor_embeddings()
        best_similarity = max(_cosine(query_emb, a) for a in anchors)
        return best_similarity >= SIMILARITY_THRESHOLD
    except Exception:
        # If embedding fails for any reason, fail open so a transient error
        # never blocks a legitimate user — the system prompt is still a
        # second line of defense against off-topic answers.
        return True

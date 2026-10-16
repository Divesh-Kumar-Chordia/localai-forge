"""Dense, BM25, hybrid (RRF) retrieval and cross-encoder reranking (#4, #5).

dense   - embeddings in Chroma. Wins on paraphrase ("get my money back" ~ "refund").
bm25    - SQLite FTS5. Wins on exact tokens: error codes (E-2001), SKUs, clause numbers, names.
hybrid  - Reciprocal Rank Fusion: score = sum 1/(k + rank). Rank-based, so no need to
          normalise incomparable score scales (cosine vs BM25). k=60 is the standard constant.
rerank  - cross-encoder reads (query, chunk) together: far more accurate than bi-encoder
          similarity, but O(candidates) model calls, so only run it on the fused top-N.
"""
import re
import threading
import time

from app import db, llm
from app.config import settings
from app.rag.store import chunks_col

_reranker = None
_reranker_lock = threading.Lock()


def get_reranker():
    global _reranker
    with _reranker_lock:
        if _reranker is None:
            from sentence_transformers import CrossEncoder
            # CPU + a lock: torch/MPS segfaults when two threads run inference concurrently.
            _reranker = CrossEncoder(settings.reranker_model, max_length=512, device="cpu")
    return _reranker


def rerank_scores(pairs: list[tuple[str, str]]) -> list[float]:
    model = get_reranker()
    with _reranker_lock:
        return [float(x) for x in model.predict(pairs)]


def dense(query: str, strategy: str, k: int) -> list[dict]:
    qe = llm.embed_one(query, kind="query")
    r = chunks_col.query(query_embeddings=[qe], n_results=k, where={"strategy": strategy})
    out = []
    for i, cid in enumerate(r["ids"][0]):
        m = r["metadatas"][0][i]
        out.append({"id": cid, "doc": m["doc"], "section": m.get("section", ""),
                    "parent_id": m.get("parent_id") or None, "content": r["documents"][0][i],
                    "dense_score": round(1 - r["distances"][0][i], 4)})
    return out


_STOP = {"the", "a", "an", "is", "are", "of", "to", "in", "for", "on", "and", "or", "what", "how",
         "do", "does", "i", "my", "we", "our", "can", "with", "it", "be", "if", "at", "by", "this", "that"}


def _fts_query(q: str) -> str:
    # Keep hyphenated codes like E-2001 / T-1001 as exact phrases; OR the rest.
    toks = re.findall(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*", q)
    terms = [t for t in toks if t.lower() not in _STOP]
    return " OR ".join(f'"{t}"' for t in terms) or '""'


def bm25(query: str, strategy: str, k: int) -> list[dict]:
    rows = db.query(
        "SELECT f.id, c.doc, c.section, c.parent_id, c.content, bm25(chunks_fts) AS s "
        "FROM chunks_fts f JOIN chunks c ON c.id = f.id "
        "WHERE chunks_fts MATCH ? AND f.strategy = ? ORDER BY s LIMIT ?",
        (_fts_query(query), strategy, k),
    )
    # FTS5 bm25() is negative (lower = better); flip sign for readability.
    return [{**{k2: r[k2] for k2 in ("id", "doc", "section", "parent_id", "content")},
             "bm25_score": round(-r["s"], 4)} for r in rows]


def rrf(lists: dict[str, list[dict]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for name, hits in lists.items():
        for rank, h in enumerate(hits, start=1):
            item = fused.setdefault(h["id"], {**h, "rrf_score": 0.0})
            item[f"{name}_rank"] = rank
            item.update({kk: vv for kk, vv in h.items() if kk.endswith("_score")})
            item["rrf_score"] += 1.0 / (k + rank)
    out = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)
    for x in out:
        x["rrf_score"] = round(x["rrf_score"], 5)
    return out


def rerank(query: str, hits: list[dict], top_n: int) -> list[dict]:
    if not hits:
        return hits
    scores = rerank_scores([(query, h["content"]) for h in hits])
    for h, s in zip(hits, scores):
        h["rerank_score"] = round(float(s), 4)
    return sorted(hits, key=lambda x: x["rerank_score"], reverse=True)[:top_n]


def expand_parents(hits: list[dict]) -> list[dict]:
    """parent_child strategy: swap matched children for their parent section, de-duplicated."""
    out, seen = [], set()
    for h in hits:
        pid = h.get("parent_id")
        if not pid:
            out.append(h)
            continue
        if pid in seen:
            continue
        seen.add(pid)
        p = db.one("SELECT content FROM parents WHERE id=?", (pid,))
        out.append({**h, "content": p["content"] if p else h["content"], "expanded_from_child": True})
    return out


def retrieve(query: str, strategy: str = "recursive", mode: str = "hybrid", use_rerank: bool = True,
             k: int | None = None, top_n: int | None = None) -> dict:
    """Returns every intermediate stage so the debugger (#2) can show where things went wrong."""
    k = k or settings.retrieve_k
    top_n = top_n or settings.context_k
    timings, stages = {}, {}

    t = time.time()
    if mode in ("dense", "hybrid"):
        stages["dense"] = dense(query, strategy, k)
        timings["dense_ms"] = round((time.time() - t) * 1000, 1)
    t = time.time()
    if mode in ("bm25", "hybrid"):
        stages["bm25"] = bm25(query, strategy, k)
        timings["bm25_ms"] = round((time.time() - t) * 1000, 1)

    if mode == "hybrid":
        t = time.time()
        candidates = rrf({"dense": stages["dense"], "bm25": stages["bm25"]}, settings.rrf_k)
        timings["fusion_ms"] = round((time.time() - t) * 1000, 1)
    else:
        candidates = [dict(h, **{f"{mode}_rank": i + 1}) for i, h in enumerate(stages[mode])]
    stages["candidates"] = candidates

    if use_rerank:
        t = time.time()
        final = rerank(query, [dict(c) for c in candidates[:k]], top_n)
        timings["rerank_ms"] = round((time.time() - t) * 1000, 1)
    else:
        final = candidates[:top_n]

    context = expand_parents(final) if strategy == "parent_child" else final
    return {"query": query, "strategy": strategy, "mode": mode, "rerank": use_rerank,
            "stages": stages, "final": context, "timings": timings}

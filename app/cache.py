"""Semantic cache with hit-rate tracking (#8).

Design decisions worth saying out loud:
- Key = query embedding, but ONLY compared against entries with the same corpus_version and
  prompt_version. Re-ingesting docs or rolling back a prompt silently invalidates old answers.
- Threshold is embedding-model specific; calibrate it (see /cache/probe and the near-miss
  histogram) instead of copying 0.95 from a blog.
- Negation guard: "cancel my plan" vs "don't cancel my plan" embed very close. Cheap lexical
  check blocks those hits.
- Only grounded answers are written to the cache, so a hallucination is never amplified.
"""
import json
import re
import time

import numpy as np

from app import db, llm
from app.config import settings

_NEG = re.compile(r"\b(not|no|never|don't|dont|doesn't|without|cannot|can't|isn't|won't|stop)\b", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _guard(a: str, b: str) -> str | None:
    if bool(_NEG.search(a)) != bool(_NEG.search(b)):
        return "negation mismatch"
    if set(_NUM.findall(a)) != set(_NUM.findall(b)):
        return "different numbers"
    return None


def _cos(a: np.ndarray, m: np.ndarray) -> np.ndarray:
    return (m @ a) / (np.linalg.norm(m, axis=1) * np.linalg.norm(a) + 1e-9)


def lookup(query: str, corpus_version: str, prompt_version: str, emb: list[float] | None = None,
           threshold: float | None = None) -> dict:
    threshold = threshold or settings.cache_threshold
    emb = emb or llm.embed_one(query)
    rows = db.query("SELECT id, query, embedding, answer, sources FROM cache "
                    "WHERE corpus_version=? AND prompt_version=?", (corpus_version, prompt_version))
    if not rows:
        return {"hit": False, "similarity": 0.0, "embedding": emb}
    m = np.array([json.loads(r["embedding"]) for r in rows])
    sims = _cos(np.array(emb), m)
    i = int(np.argmax(sims))
    best = rows[i]
    sim = float(sims[i])
    result = {"hit": False, "similarity": round(sim, 4), "nearest": best["query"], "embedding": emb}
    if sim >= threshold:
        blocked = _guard(query, best["query"])
        if blocked:
            result["blocked"] = blocked
        else:
            with db.tx() as c:
                c.execute("UPDATE cache SET hits = hits + 1 WHERE id=?", (best["id"],))
            result.update(hit=True, answer=best["answer"], sources=json.loads(best["sources"]))
    return result


def store(query: str, emb: list[float], answer: str, sources: list, corpus_version: str,
          prompt_version: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO cache (query, embedding, answer, sources, corpus_version, prompt_version) "
                  "VALUES (?,?,?,?,?,?)",
                  (query, json.dumps(emb), answer, json.dumps(sources), corpus_version, prompt_version))


def record(hit: bool, similarity: float, saved_ms: float = 0.0) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO cache_events (hit, similarity, saved_ms, at) VALUES (?,?,?,?)",
                  (int(hit), similarity, saved_ms, time.time()))


def stats() -> dict:
    ev = db.query("SELECT hit, similarity, saved_ms FROM cache_events")
    n = len(ev)
    hits = sum(e["hit"] for e in ev)
    sims = [e["similarity"] for e in ev if not e["hit"]]
    hist = np.histogram(sims, bins=[0, .5, .6, .7, .8, .85, .9, .93, .95, 1.0])[0].tolist() if sims else []
    return {
        "lookups": n, "hits": hits, "hit_rate": round(hits / n, 3) if n else 0.0,
        "entries": db.one("SELECT COUNT(*) AS n FROM cache")["n"],
        "latency_saved_ms_total": round(sum(e["saved_ms"] for e in ev)),
        "miss_similarity_histogram": {"bins": ["<.5", ".5-.6", ".6-.7", ".7-.8", ".8-.85", ".85-.9",
                                               ".9-.93", ".93-.95", ">.95"], "counts": hist},
        "threshold": settings.cache_threshold,
    }


def probe(a: str, b: str) -> dict:
    ea, eb = llm.embed([a, b], kind="query")
    sim = float(_cos(np.array(ea), np.array([eb]))[0])
    blocked = _guard(a, b)
    return {"similarity": round(sim, 4), "threshold": settings.cache_threshold,
            "would_hit": sim >= settings.cache_threshold and not blocked, "guard": blocked}


def clear() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM cache")
        c.execute("DELETE FROM cache_events")

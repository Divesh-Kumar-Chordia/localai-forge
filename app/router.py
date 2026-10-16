"""Route between the cheap (3B) and smart (8B) model (#9, also the main lever for #6).

Modes:
  smart    - always 8B (quality baseline)
  cheap    - always 3B (cost/latency floor)
  rules    - zero-latency heuristics on the query + retrieval signals
  cascade  - try 3B first; if its answer fails the grounding check, escalate to 8B.
             Pays double on hard queries, saves on the easy majority.
A trained classifier on logged (query, which-model-was-good-enough) pairs is the production
upgrade to "rules"; the features below are what it would start from.
"""
import re

from app.config import settings

HARD_WORDS = re.compile(r"\b(compare|difference|why|explain|analy[sz]e|calculate|prorat|liabilit|indemn|"
                        r"terminat|breach|negotiat|recommend|trade-?off|versus|vs\.?|step[- ]by[- ]step|"
                        r"what happens if|edge case)\b", re.I)


def route(query: str, mode: str, hits: list[dict] | None = None) -> dict:
    r = _route(query, mode, hits)
    if settings.cheap_model == settings.smart_model:
        r["reason"] = f"single-model mode (both tiers = {settings.smart_model}); would route: {r['reason']}"
    return r


def _route(query: str, mode: str, hits: list[dict] | None = None) -> dict:
    if mode == "smart":
        return {"model": settings.smart_model, "reason": "forced smart"}
    if mode in ("cheap", "cascade"):
        return {"model": settings.cheap_model, "reason": "forced cheap" if mode == "cheap" else "cascade: try cheap first"}

    reasons = []
    if len(query.split()) > 25:
        reasons.append("long query")
    if HARD_WORDS.search(query):
        reasons.append(f"reasoning keyword '{HARD_WORDS.search(query).group(0)}'")
    if query.count("?") > 1 or re.search(r"\band\b.*\?", query):
        reasons.append("multi-part question")
    if hits:
        docs = {h["doc"] for h in hits[:3]}
        if len(docs) >= 3:
            reasons.append(f"top evidence spread across {len(docs)} docs")
        if hits[0]["doc"].startswith("contracts/"):
            reasons.append("legal source is the top hit")
        top = hits[0].get("rerank_score")
        if top is not None and top < 0:
            reasons.append("low retrieval confidence")
    if reasons:
        return {"model": settings.smart_model, "reason": "; ".join(reasons)}
    return {"model": settings.cheap_model, "reason": "simple lookup"}

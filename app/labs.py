"""Latency (#7), serving (#21) and cost (#6) measurements against the local models."""
import asyncio
import json
import statistics
import time

from app import db, llm
from app.config import settings
from app.pipeline import _context
from app.rag.retrieve import retrieve

PRICES = {  # illustrative API-equivalent USD per 1M tokens (in, out)
    settings.smart_model: (3.0, 15.0),
    settings.cheap_model: (0.25, 1.25),
}


async def _ttft(model: str, prompt: str, max_tokens: int = 60) -> dict:
    t0 = time.time()
    first = None
    n = 0
    stream = await llm.aclient.generate(model=model, prompt=prompt, stream=True, keep_alive=settings.keep_alive,
                                        options={"num_predict": max_tokens, "temperature": 0,
                                                 "num_ctx": settings.num_ctx})
    load_ms = prompt_ms = None
    async for part in stream:
        if first is None and part.get("response"):
            first = (time.time() - t0) * 1000
        if part.get("done"):
            n = part.get("eval_count") or 0
            load_ms = (part.get("load_duration") or 0) / 1e6
            prompt_ms = (part.get("prompt_eval_duration") or 0) / 1e6
    total = (time.time() - t0) * 1000
    return {"ttft_ms": round(first or total), "total_ms": round(total), "tokens_out": n,
            "model_load_ms": round(load_ms or 0), "prompt_eval_ms": round(prompt_ms or 0),
            "prompt_chars": len(prompt)}


async def ttft_lab(question: str = "What is the refund policy for annual plans?") -> list[dict]:
    big = _context(retrieve(question, "recursive", "hybrid", False, 20, 20)["final"])
    small = _context(retrieve(question, "recursive", "hybrid", True, 20, 3)["final"])
    p_big = f"Answer from sources.\n{big}\nQuestion: {question}"
    p_small = f"Answer from sources.\n{small}\nQuestion: {question}"
    rows = []
    # 1) cold: unload, then first request pays weight loading
    await llm.aclient.generate(model=settings.smart_model, prompt="", keep_alive=0)
    await asyncio.sleep(1.0)
    rows.append({"variant": "cold start (model unloaded), 8B, 20 chunks", **await _ttft(settings.smart_model, p_big)})
    rows.append({"variant": "warm (keep_alive), 8B, 20 chunks", **await _ttft(settings.smart_model, p_big)})
    rows.append({"variant": "warm, 8B, reranked 3 chunks", **await _ttft(settings.smart_model, p_small)})
    rows.append({"variant": "warm, 3B, reranked 3 chunks", **await _ttft(settings.cheap_model, p_small)})
    rows.append({"variant": "repeat same prompt (Ollama prefix/KV reuse)", **await _ttft(settings.cheap_model, p_small)})
    return rows


async def concurrency_lab(n: int = 4, model: str | None = None, max_tokens: int = 80) -> dict:
    model = model or settings.cheap_model
    prompts_ = [f"In two sentences, explain topic #{i}: why caching reduces LLM latency." for i in range(n)]
    t0 = time.time()
    res = await asyncio.gather(*[_ttft(model, p, max_tokens) for p in prompts_])
    wall = (time.time() - t0) * 1000
    toks = sum(r["tokens_out"] for r in res)
    return {
        "model": model, "concurrent_requests": n, "wall_ms": round(wall),
        "throughput_tok_s": round(toks / (wall / 1000), 1),
        "ttft_p50_ms": round(statistics.median(r["ttft_ms"] for r in res)),
        "ttft_max_ms": max(r["ttft_ms"] for r in res),
        "per_request": res,
        "note": "If ttft_max grows ~linearly with n, Ollama is serving one request at a time "
                "(OLLAMA_NUM_PARALLEL=1). Restart Ollama with OLLAMA_NUM_PARALLEL=4 and compare.",
    }


def _pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))]) if xs else None


def metrics() -> dict:
    tr = db.query("SELECT kind, model, status, prompt_version, ttft_ms, total_ms, tokens_in, tokens_out, meta "
                  "FROM traces WHERE ended IS NOT NULL")
    chats = [t for t in tr if t["kind"] == "chat"]
    by_model: dict = {}
    for t in chats:
        m = by_model.setdefault(t["model"] or "unknown", {"requests": 0, "ttft": [], "total": [],
                                                           "tokens_in": 0, "tokens_out": 0})
        m["requests"] += 1
        m["ttft"].append(t["ttft_ms"]); m["total"].append(t["total_ms"])
        m["tokens_in"] += t["tokens_in"] or 0; m["tokens_out"] += t["tokens_out"] or 0
    table = []
    actual = baseline = 0.0
    smart_in, smart_out = PRICES[settings.smart_model]
    llm_reqs = [t for t in chats if t["model"] not in ("cache", "none", None)]
    avg_in = statistics.mean([t["tokens_in"] or 0 for t in llm_reqs]) if llm_reqs else 0
    avg_out = statistics.mean([t["tokens_out"] or 0 for t in llm_reqs]) if llm_reqs else 0
    for name, m in by_model.items():
        pin, pout = PRICES.get(name, (0, 0))
        cost = m["tokens_in"] / 1e6 * pin + m["tokens_out"] / 1e6 * pout
        actual += cost
        if name in ("cache", "none"):
            baseline += m["requests"] * (avg_in / 1e6 * smart_in + avg_out / 1e6 * smart_out)
        else:
            baseline += m["tokens_in"] / 1e6 * smart_in + m["tokens_out"] / 1e6 * smart_out
        table.append({"model": name, "requests": m["requests"], "ttft_p50": _pct(m["ttft"], .5),
                      "ttft_p95": _pct(m["ttft"], .95), "total_p50": _pct(m["total"], .5),
                      "total_p95": _pct(m["total"], .95), "tokens_in": m["tokens_in"],
                      "tokens_out": m["tokens_out"], "api_equiv_usd": round(cost, 5)})
    by_prompt: dict = {}
    for t in chats:
        f = (json.loads(t["meta"]) if t["meta"] else {}).get("faithfulness")
        p = by_prompt.setdefault(t["prompt_version"], {"requests": 0, "faith": []})
        p["requests"] += 1
        if f is not None:
            p["faith"].append(f)
    return {
        "requests": {k: sum(1 for t in tr if t["kind"] == k) for k in {t["kind"] for t in tr}},
        "status": {k: sum(1 for t in tr if t["status"] == k) for k in {t["status"] for t in tr}},
        "by_model": table,
        "cost": {"actual_api_equiv_usd": round(actual, 5), "baseline_all_smart_no_cache_usd": round(baseline, 5),
                 "savings_pct": round(100 * (1 - actual / baseline), 1) if baseline else 0.0},
        "by_prompt_version": [{"prompt": k, "requests": v["requests"],
                               "faithfulness_avg": round(statistics.mean(v["faith"]), 3) if v["faith"] else None}
                              for k, v in by_prompt.items()],
    }

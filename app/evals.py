"""Evaluation suite (#5, #15, #16) and the prompt-promotion gate (#24).

retrieval  - hit@k, recall@k, MRR against labelled relevant docs. No LLM, runs in seconds.
             This is the fast loop for chunking / hybrid / reranker decisions.
generation - run the real pipeline, then: correctness (LLM judge vs reference, 1-5 rubric),
             faithfulness (grounding check), abstention accuracy on out-of-scope questions,
             TTFT/latency and tokens.
judge trust - calibrate each judge model against human labels: agreement + Cohen's kappa,
             and pairwise comparison with position swap to expose position bias.
"""
import json
import statistics
import time
import uuid

from pydantic import BaseModel

from app import db, grounding, llm, prompts
from app.config import ROOT, settings
from app.pipeline import ChatConfig, collect
from app.rag.retrieve import retrieve

EVAL_DIR = ROOT / "data" / "eval"


def golden(limit: int | None = None, category: str | None = None) -> list[dict]:
    rows = [json.loads(l) for l in (EVAL_DIR / "golden.jsonl").read_text().splitlines() if l.strip()]
    if category:
        rows = [r for r in rows if r["category"] == category]
    return rows[:limit] if limit else rows


# ------------------------------------------------------------------ retrieval
def retrieval_eval(configs: list[dict], k: int = 5) -> list[dict]:
    qs = [q for q in golden() if q["expected_docs"]]
    results = []
    for cfg in configs:
        hit, rec, rr, lat, misses = [], [], [], [], []
        for q in qs:
            t = time.time()
            r = retrieve(q["question"], cfg.get("strategy", "recursive"), cfg.get("mode", "hybrid"),
                         cfg.get("rerank", False), None, k)
            lat.append((time.time() - t) * 1000)
            docs = [h["doc"] for h in r["final"]]
            exp = set(q["expected_docs"])
            first = next((i for i, d in enumerate(docs) if d in exp), None)
            hit.append(first is not None)
            rec.append(len(exp & set(docs)) / len(exp))
            rr.append(1 / (first + 1) if first is not None else 0)
            if first is None:
                misses.append(q["id"])
        results.append({
            "config": cfg, "n": len(qs), f"hit@{k}": round(statistics.mean(hit), 3),
            f"recall@{k}": round(statistics.mean(rec), 3), "mrr": round(statistics.mean(rr), 3),
            "p50_ms": round(statistics.median(lat), 1), "misses": misses,
        })
    return results


# ------------------------------------------------------------------ judge
class JudgeOut(BaseModel):
    reasoning: str
    score: int


class PairOut(BaseModel):
    reasoning: str
    winner: str  # "A" | "B" | "tie"


def judge(question: str, reference: str, answer: str, model: str | None = None) -> dict:
    tpl, v = prompts.get("judge")
    out, _ = llm.structured([{"role": "user", "content": prompts.render(
        tpl, question=question, reference=reference, answer=answer)}], JudgeOut,
        model=model or settings.smart_model, temperature=0)
    return {"score": max(1, min(5, out.score)), "reasoning": out.reasoning, "judge": model or settings.smart_model}


def pairwise(question: str, a: str, b: str, model: str | None = None) -> dict:
    """Ask twice with positions swapped. Only a consistent preference counts (#16)."""
    def ask(x, y):
        out, _ = llm.structured([{"role": "user", "content":
            f"Which answer better and more correctly answers the question? Reply winner A, B or tie.\n\n"
            f"Question: {question}\n\nAnswer A:\n{x}\n\nAnswer B:\n{y}"}], PairOut,
            model=model or settings.smart_model, temperature=0)
        return out.winner.strip().upper()[:3]
    first = ask(a, b)
    second = ask(b, a)
    swapped = {"A": "B", "B": "A"}.get(second, "TIE")
    consistent = first == swapped
    return {"order_ab": first, "order_ba_mapped": swapped, "consistent": consistent,
            "winner": first if consistent else "inconsistent (position bias)"}


def _kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def calibrate_judges(models: list[str] | None = None, pass_at: int = 4) -> list[dict]:
    labels = [json.loads(l) for l in (EVAL_DIR / "judge_labels.jsonl").read_text().splitlines() if l.strip()]
    out = []
    for m in models or [settings.smart_model, settings.cheap_model]:
        preds, rows = [], []
        for lab in labels:
            j = judge(lab["question"], lab["reference"], lab["answer"], m)
            p = j["score"] >= pass_at
            preds.append(p)
            rows.append({"id": lab["id"], "human": lab["human_pass"], "judge_score": j["score"], "judge_pass": p,
                         "agree": p == lab["human_pass"], "reasoning": j["reasoning"][:200]})
        human = [l["human_pass"] for l in labels]
        out.append({"judge": m, "n": len(labels),
                    "agreement": round(sum(r["agree"] for r in rows) / len(rows), 3),
                    "cohens_kappa": _kappa(preds, human),
                    "false_pass": sum(1 for r in rows if r["judge_pass"] and not r["human"]),
                    "false_fail": sum(1 for r in rows if not r["judge_pass"] and r["human"]),
                    "rows": rows})
    return out


# ------------------------------------------------------------------ generation
async def generation_eval(config: dict, limit: int | None = None, judge_model: str | None = None,
                          name: str = "") -> dict:
    rows = []
    for q in golden(limit):
        cfg = ChatConfig(message=q["question"], session_id=f"eval-{uuid.uuid4().hex[:6]}",
                         **{**{"use_cache": False, "memory": False}, **config})
        t = time.time()
        r = await collect(cfg)
        ms = (time.time() - t) * 1000
        ans = r["answer"]
        out_of_scope = not q["expected_docs"]
        abstained = grounding.is_abstention(ans)
        j = judge(q["question"], q["reference"], ans, judge_model)
        docs = [s["doc"] for s in r["meta"].get("sources", [])]
        rows.append({
            "id": q["id"], "category": q["category"], "question": q["question"], "answer": ans,
            "score": j["score"], "judge_reasoning": j["reasoning"][:300],
            "faithfulness": r["grounding"].get("faithfulness"),
            "abstained": abstained, "abstain_correct": abstained == out_of_scope,
            "retrieved_expected": bool(set(docs) & set(q["expected_docs"])) if q["expected_docs"] else None,
            "model": r["meta"].get("route", {}).get("model"), "ttft_ms": r.get("ttft_ms") or r["meta"].get("ttft_ms"),
            "total_ms": round(ms), "trace_id": r.get("trace_id"),
        })
    f = [r["faithfulness"] for r in rows if r["faithfulness"] is not None]
    ttfts = [r["ttft_ms"] for r in rows if r["ttft_ms"]]
    metrics = {
        "n": len(rows),
        "correctness_avg": round(statistics.mean(r["score"] for r in rows), 3),
        "pass_rate": round(sum(r["score"] >= 4 for r in rows) / len(rows), 3),
        "faithfulness_avg": round(statistics.mean(f), 3) if f else None,
        "hallucination_rate": round(sum(1 for x in f if x < 1.0) / len(f), 3) if f else None,
        "abstention_accuracy": round(sum(r["abstain_correct"] for r in rows) / len(rows), 3),
        "ttft_p50_ms": round(statistics.median(ttfts)) if ttfts else None,
        "latency_p50_ms": round(statistics.median(r["total_ms"] for r in rows)),
        "models": {m: sum(1 for r in rows if r["model"] == m) for m in {r["model"] for r in rows}},
    }
    rid = uuid.uuid4().hex[:10]
    with db.tx() as c:
        c.execute("INSERT INTO eval_runs (id, name, config, metrics, rows) VALUES (?,?,?,?,?)",
                  (rid, name or json.dumps(config), json.dumps(config), json.dumps(metrics), json.dumps(rows)))
    return {"id": rid, "config": config, "metrics": metrics, "rows": rows}


def runs() -> list[dict]:
    out = db.query("SELECT id, name, config, metrics, created_at FROM eval_runs ORDER BY created_at DESC LIMIT 30")
    for r in out:
        r["metrics"] = json.loads(r["metrics"])
        r["config"] = json.loads(r["config"])
    return out


def run_detail(rid: str) -> dict | None:
    r = db.one("SELECT * FROM eval_runs WHERE id=?", (rid,))
    if r:
        r["rows"] = json.loads(r["rows"])
        r["metrics"] = json.loads(r["metrics"])
    return r


async def promotion_gate(name: str, candidate: int, limit: int = 8) -> dict:
    """Eval-gated deploy for prompts: candidate must not regress vs active on the golden subset."""
    active = prompts.active_version(name)
    if name != "rag_answer":
        return {"error": "gate implemented for rag_answer"}
    base = await generation_eval({"prompt_version": active}, limit, name=f"gate {name}@v{active} (active)")
    cand = await generation_eval({"prompt_version": candidate}, limit, name=f"gate {name}@v{candidate} (candidate)")
    bm, cm = base["metrics"], cand["metrics"]
    reasons = []
    if cm["pass_rate"] < bm["pass_rate"] - 0.1:
        reasons.append(f"pass_rate {bm['pass_rate']} -> {cm['pass_rate']}")
    if (cm["faithfulness_avg"] or 0) < (bm["faithfulness_avg"] or 0) - 0.1:
        reasons.append(f"faithfulness {bm['faithfulness_avg']} -> {cm['faithfulness_avg']}")
    if cm["abstention_accuracy"] < bm["abstention_accuracy"] - 0.1:
        reasons.append(f"abstention {bm['abstention_accuracy']} -> {cm['abstention_accuracy']}")
    passed = not reasons
    if passed:
        prompts.set_active(name, candidate, action="promote(gated)")
    return {"passed": passed, "regressions": reasons, "active": bm, "candidate": cm,
            "now_active": prompts.active_version(name)}

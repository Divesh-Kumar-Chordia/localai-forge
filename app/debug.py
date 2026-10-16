"""'Your RAG gives wrong answers. Debug it.' (#2)

The method: walk the pipeline backwards and find the FIRST stage where the right evidence is lost.
  1. Ingestion  - is the fact even in a chunk? (chunking can split it apart)
  2. Retrieval  - is the right chunk in the candidate set at all?     -> recall problem
  3. Ranking    - is it in candidates but not in the final top-n?      -> precision/rerank problem
  4. Generation - was it in the prompt but the answer is still wrong?  -> prompt/model problem
Each stage has different fixes, so guessing ("let's try a bigger model") wastes days.
"""
from app import db, grounding, llm, prompts
from app.config import settings
from app.pipeline import _context
from app.rag.retrieve import retrieve


def _pos(hits: list[dict], doc: str) -> int | None:
    return next((i + 1 for i, h in enumerate(hits) if h["doc"] == doc), None)


def diagnose(question: str, expected_doc: str | None = None, expected_text: str | None = None,
             strategy: str = "recursive", mode: str = "hybrid", rerank: bool = True, top_n: int = 5) -> dict:
    r = retrieve(question, strategy, mode, rerank, settings.retrieve_k, top_n)
    final = r["final"]
    tpl, _ = prompts.get("rag_answer")
    prompt = prompts.render(tpl, context=_context(final), question=question, memory="")
    gen = llm.chat([{"role": "user", "content": prompt}], settings.smart_model, 0.1)
    g = grounding.check(gen["text"], final)

    stages = []
    verdict, fixes = "looks healthy", []
    if expected_text:
        in_index = db.query("SELECT doc, strategy FROM chunks WHERE content LIKE ? AND strategy=?",
                            (f"%{expected_text}%", strategy))
        stages.append({"stage": "1. ingestion", "ok": bool(in_index),
                       "detail": f"{len(in_index)} '{strategy}' chunk(s) contain the exact text"})
        if not in_index:
            verdict = "INGESTION/CHUNKING: the fact is split across chunks or missing"
            fixes = ["Try 'legal' or 'parent_child' chunking", "Increase chunk size/overlap",
                     "Check the loader (PDF tables/columns often lose text)"]
    if expected_doc:
        pos = {name: _pos(hits, expected_doc) for name, hits in r["stages"].items()}
        final_pos = _pos(final, expected_doc)
        stages.append({"stage": "2. retrieval (candidates)", "ok": pos.get("candidates") is not None,
                       "detail": {k: v for k, v in pos.items()}})
        stages.append({"stage": "3. ranking (final top-n)", "ok": final_pos is not None,
                       "detail": f"position in final context: {final_pos}"})
        if verdict == "looks healthy":
            if pos.get("candidates") is None:
                verdict = "RETRIEVAL MISS: expected doc never became a candidate"
                fixes = ["Switch to hybrid (BM25 catches exact terms/codes)", "Query rewriting / HyDE",
                         "Different chunking strategy", "Check embedding model fits the domain"]
            elif final_pos is None:
                verdict = "RANKING MISS: doc was a candidate but cut before the prompt"
                fixes = ["Enable/upgrade the cross-encoder reranker", "Increase top_n",
                         "Metadata filters (doc type, date)"]
    faith = g.get("faithfulness")
    stages.append({"stage": "4. generation", "ok": (faith or 0) >= 0.8,
                   "detail": f"faithfulness={faith}, unsupported={len(g.get('unsupported', []))}"})
    if verdict == "looks healthy" and (faith or 0) < 0.8:
        verdict = "GENERATION: evidence was in the prompt but the answer isn't grounded in it"
        fixes = ["Tighten the prompt (cite every sentence, abstain rule)", "Lower temperature",
                 "Fewer, better chunks (lost-in-the-middle)", "Route to the larger model"]
    return {"verdict": verdict, "fixes": fixes, "stages": stages, "answer": gen["text"], "grounding": g,
            "prompt": prompt, "retrieval": {"timings": r["timings"],
            "dense": [(h["doc"], h.get("dense_score")) for h in r["stages"].get("dense", [])[:10]],
            "bm25": [(h["doc"], h.get("bm25_score")) for h in r["stages"].get("bm25", [])[:10]],
            "final": [{"doc": h["doc"], "rerank": h.get("rerank_score"), "rrf": h.get("rrf_score"),
                       "content": h["content"][:400]} for h in final]}}

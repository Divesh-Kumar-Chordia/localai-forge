"""Multi-agent workflow with end-to-end tracing (#23).

planner (8B) -> researcher (3B + RAG, per sub-question) -> writer (8B) -> reviewer (grounding check)
     -> if reviewer rejects and revisions left: writer again with the critique -> END

Every node and every LLM / retrieval call inside it is a span under one trace_id, so the
Traces page renders the full tree and waterfall: who called what, with which input, how long.
"""
import time
from typing import TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from app import grounding, llm, tracing
from app.config import settings
from app.rag.retrieve import retrieve


class Plan(BaseModel):
    sub_questions: list[str] = Field(max_length=4)


class WState(TypedDict, total=False):
    trace_id: str
    task: str
    plan: list
    findings: list
    draft: str
    review: dict
    revisions: int


def planner(s: WState) -> WState:
    with tracing.span("planner", s["task"], trace_id=s["trace_id"]) as sp:
        plan, _ = llm.structured([{"role": "user", "content":
            f"Break this task into 2-4 specific questions that can each be answered from Acme Cloud's "
            f"knowledge base (pricing, policies, contracts, SLAs). Every question MUST name the customer or "
            f"entity from the task (e.g. 'in the Globex MSA') so retrieval finds the right document."
            f"\n\nTask: {s['task']}"}], Plan,
            model=settings.smart_model)
        qs = plan.sub_questions[:4]
        sp.set(qs)
    return {"plan": qs}


def researcher(s: WState) -> WState:
    findings = []
    with tracing.span("researcher", s["plan"], trace_id=s["trace_id"]) as sp:
        for q in s["plan"]:
            with tracing.span("research.sub_question", q, trace_id=s["trace_id"]) as sub:
                with tracing.span("retrieve", q, trace_id=s["trace_id"]) as rs:
                    hits = retrieve(q, "legal", "hybrid", True, None, 3)["final"]
                    rs.set([h["doc"] for h in hits])
                ctx = "\n\n".join(f"[{h['doc']}] {h['content'][:900]}" for h in hits)
                with tracing.span("llm.answer", {"model": settings.cheap_model}, trace_id=s["trace_id"]) as ls:
                    r = llm.chat([{"role": "user", "content": f"Using only these sources, answer in 1-3 sentences. "
                                   f"State the actual values (numbers, percentages, durations) - never answer "
                                   f"'refer to section X'. End with the doc name in brackets. If the sources "
                                   f"don't contain it, say 'not found'.\n\n{ctx}\n\nQuestion: {q}"}],
                                 settings.cheap_model, 0)
                    ls.set(r["text"])
                findings.append({"question": q, "answer": r["text"], "sources": hits})
                sub.set(r["text"][:300])
        sp.set(len(findings))
    return {"findings": findings}


def writer(s: WState) -> WState:
    notes = "\n\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in s["findings"])
    critique = ""
    if s.get("review", {}).get("unsupported"):
        critique = "\n\nA reviewer flagged these unsupported claims; remove or fix them:\n- " + \
                   "\n- ".join(s["review"]["unsupported"])
    with tracing.span("writer", {"revision": s.get("revisions", 0)}, trace_id=s["trace_id"]) as sp:
        r = llm.chat([{"role": "user", "content": f"Write a concise brief (bullets, max 150 words) for the task "
                       f"using ONLY these research notes.\n\nTask: {s['task']}\n\nNotes:\n{notes}{critique}"}],
                     settings.smart_model, 0.2)
        sp.set(r["text"])
    return {"draft": r["text"]}


def reviewer(s: WState) -> WState:
    sources = [h for f in s["findings"] for h in f["sources"]]
    with tracing.span("reviewer", None, trace_id=s["trace_id"]) as sp:
        g = grounding.check(s["draft"], sources, settings.smart_model, s["task"])
        sp.set({"faithfulness": g.get("faithfulness"), "unsupported": g.get("unsupported")})
    return {"review": g, "revisions": s.get("revisions", 0) + 1}


def _after_review(s: WState) -> str:
    ok = (s["review"].get("faithfulness") or 0) >= 0.8
    return END if ok or s["revisions"] >= 2 else "writer"


def build():
    g = StateGraph(WState)
    for name, fn in [("planner", planner), ("researcher", researcher), ("writer", writer), ("reviewer", reviewer)]:
        g.add_node(name, fn)
    g.set_entry_point("planner")
    g.add_edge("planner", "researcher")
    g.add_edge("researcher", "writer")
    g.add_edge("writer", "reviewer")
    g.add_conditional_edges("reviewer", _after_review, {"writer": "writer", END: END})
    return g.compile()


GRAPH = build()


def run_workflow(task: str) -> dict:
    tid = tracing.start_trace("workflow", task, model="multi")
    t0 = time.time()
    try:
        out = GRAPH.invoke({"trace_id": tid, "task": task, "revisions": 0})
        status = "ok"
    except Exception as e:
        out, status = {"draft": f"workflow error: {e!r}"}, "error"
    res = {"trace_id": tid, "plan": out.get("plan"), "brief": out.get("draft"),
           "review": out.get("review"), "revisions": out.get("revisions"),
           "ms": round((time.time() - t0) * 1000)}
    tracing.end_trace(tid, res["brief"], status=status, meta={"plan": res["plan"], "revisions": res["revisions"]})
    return res

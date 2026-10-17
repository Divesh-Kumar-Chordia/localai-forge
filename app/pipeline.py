"""The RAG chat pipeline. One async generator yields events, consumed by SSE (#22) and by evals.

  PII redact -> memory -> condense follow-up -> semantic cache -> hybrid retrieve -> rerank
  -> confidence gate (abstain) -> route model -> stream generation -> grounding check
  -> cache write (only if grounded) -> memory write -> trace
"""
import asyncio
import time
from typing import AsyncIterator, Literal

from pydantic import BaseModel

from app import cache, grounding, llm, memory, prompts, tracing
from app.config import settings
from app.rag import store
from app.rag.retrieve import retrieve
from app.router import route
from app.security import Redactor, StreamRestorer, injection_scan

ABSTAIN_TEXT = "I don't know based on the knowledge base."


class ChatConfig(BaseModel):
    message: str
    session_id: str = "default"
    user_id: str = "demo-user"
    strategy: Literal["fixed", "recursive", "legal", "parent_child"] = "recursive"
    mode: Literal["dense", "bm25", "hybrid"] = "hybrid"
    rerank: bool = True
    use_cache: bool = True
    router: Literal["rules", "smart", "cheap", "cascade"] = "rules"
    redact_pii: bool = True
    grounding: bool = True
    grounding_model: str | None = None
    memory: bool = True
    prompt_version: int | None = None
    # Cross-encoder logits are NOT calibrated relevance: measured "stock price" (out of scope) = +4.5,
    # "pricing policy" (in scope, vague) = -7.6. So the gate only skips the LLM for total junk;
    # the prompt's abstain rule handles out-of-scope questions.
    abstain_below: float | None = -10.0
    top_n: int | None = None


def _context(hits: list[dict]) -> str:
    return "\n\n".join(f'<source id="{i + 1}" doc="{h["doc"]}">\n{h["content"]}\n</source>'
                       for i, h in enumerate(hits))


def _sources(hits: list[dict]) -> list[dict]:
    keep = ("id", "doc", "section", "content", "dense_score", "bm25_score", "rrf_score", "rerank_score",
            "dense_rank", "bm25_rank")
    return [{k: h.get(k) for k in keep if h.get(k) is not None} for h in hits]


async def run_chat(cfg: ChatConfig) -> AsyncIterator[tuple[str, dict]]:
    tpl, pv = prompts.get("rag_answer", cfg.prompt_version)
    ptag = f"rag_answer@v{pv}"
    t_start = time.time()
    red = Redactor()
    question = red.redact(cfg.message) if cfg.redact_pii else cfg.message
    tid = tracing.start_trace("chat", {"message": question, "config": cfg.model_dump(exclude={"message"})},
                              prompt_version=ptag)
    tokens = {"in": 0, "out": 0}
    meta: dict = {"trace_id": tid, "prompt_version": ptag}
    final_text, status = "", "ok"

    try:
        if cfg.redact_pii:
            with tracing.span("pii_redaction", cfg.message[:200]) as s:
                s.set(red.report())
            meta["pii"] = red.report()
            n = sum(red.counts.values())
            yield "stage", {"stage": "pii", "ms": s.ms, "status": "warn" if n else "ok",
                            "detail": f"{n} item(s) redacted: {red.counts}" if n else "no PII found"}

        with tracing.span("injection_scan", question) as s:
            scan = injection_scan(question)
            s.set(scan)
        if scan["suspicious"]:
            meta["injection_warning"] = scan
        yield "stage", {"stage": "injection", "ms": s.ms, "status": "warn" if scan["suspicious"] else "ok",
                        "detail": f"suspicious: {scan['matches']}" if scan["suspicious"] else "clean"}

        mem_block = ""
        standalone = question
        if cfg.memory:
            with tracing.span("memory_load") as s:
                hist = await asyncio.to_thread(memory.working, cfg.session_id)
                mem_block = await asyncio.to_thread(memory.build_block, cfg.user_id, cfg.session_id, question)
                s.set({"working_turns": len(hist), "memory_block": mem_block})
            if hist:
                with tracing.span("condense_query", question) as s:
                    ctpl, _ = prompts.get("condense")
                    convo = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in hist[-4:])
                    r = await asyncio.to_thread(llm.chat, [{"role": "user", "content": prompts.render(
                        ctpl, history=convo, question=question)}], settings.cheap_model, 0)
                    standalone = r["text"].strip().strip('"') or question
                    tokens["in"] += r["tokens_in"]; tokens["out"] += r["tokens_out"]
                    s.set(standalone)
                yield "stage", {"stage": "rewrite", "ms": s.ms, "status": "ok", "detail": standalone}
        meta["standalone_query"] = standalone

        cv = store.corpus_version()
        emb = None
        if cfg.use_cache:
            with tracing.span("semantic_cache_lookup", standalone) as s:
                c = await asyncio.to_thread(cache.lookup, standalone, cv, ptag)
                emb = c.pop("embedding")
                s.set({k: v for k, v in c.items() if k != "answer"})
            meta["cache"] = {k: v for k, v in c.items() if k not in ("answer", "sources")}
            yield "stage", {"stage": "cache", "ms": s.ms, "status": "hit" if c["hit"] else "ok",
                            "detail": (f"HIT sim={c['similarity']}" if c["hit"] else
                                       f"miss (nearest sim={c['similarity']})" + (f", blocked: {c['blocked']}" if c.get("blocked") else ""))}
            if not c["hit"]:
                cache.record(False, c["similarity"])
            if c["hit"]:
                ms = (time.time() - t_start) * 1000
                cache.record(True, c["similarity"], saved_ms=max(0.0, _avg_miss_ms() - ms))
                meta.update(route={"model": "cache", "reason": f"semantic hit sim={c['similarity']}"},
                            sources=c["sources"], ttft_ms=round(ms, 1))
                yield "meta", meta
                final_text = red.restore(c["answer"])
                yield "token", {"t": final_text}
                yield "done", {"trace_id": tid, "cached": True}
                tracing.end_trace(tid, final_text, ttft_ms=ms, tokens_in=0, tokens_out=0, model="cache", meta=meta)
                return

        with tracing.span("retrieve", {"query": standalone, "strategy": cfg.strategy, "mode": cfg.mode,
                                        "rerank": cfg.rerank}) as s:
            r = await asyncio.to_thread(retrieve, standalone, cfg.strategy, cfg.mode, cfg.rerank, None, cfg.top_n)
            hits = r["final"]
            s.set({"timings": r["timings"], "final": [(h["doc"], h.get("rerank_score"), h.get("rrf_score"))
                                                       for h in hits]})
        meta["retrieval"] = r["timings"]
        meta["sources"] = _sources(hits)
        yield "stage", {"stage": "retrieve", "ms": s.ms, "status": "ok", "timings": r["timings"],
                        "detail": f"{cfg.mode} · {cfg.strategy} · {len(r['stages'].get('candidates', []))} candidates → "
                                  f"{len(hits)} chunks" + (" (reranked)" if cfg.rerank else "")}

        top = hits[0].get("rerank_score") if hits else None
        if not hits or (cfg.rerank and cfg.abstain_below is not None and top is not None and top < cfg.abstain_below):
            meta["route"] = {"model": "none", "reason": f"confidence gate: top rerank score {top} < {cfg.abstain_below}"}
            meta["ttft_ms"] = round((time.time() - t_start) * 1000, 1)
            yield "meta", meta
            final_text = ABSTAIN_TEXT
            yield "token", {"t": final_text}
            yield "grounding", {"faithfulness": 1.0, "abstained": True}
            yield "done", {"trace_id": tid}
            tracing.end_trace(tid, final_text, model="none", meta=meta, ttft_ms=meta["ttft_ms"])
            return

        rt = route(standalone, cfg.router, hits)
        meta["route"] = rt
        yield "stage", {"stage": "route", "ms": 0, "status": "ok", "detail": f"{rt['model']} — {rt['reason']}"}
        prompt = prompts.render(tpl, context=_context(hits), question=standalone, memory=mem_block)
        msgs = [{"role": "user", "content": prompt}]
        meta["prompt_chars"] = len(prompt)

        # Cascade: draft with the cheap model, verify, escalate only if ungrounded.
        if cfg.router == "cascade":
            with tracing.span("cascade_cheap_draft", {"model": settings.cheap_model}) as s:
                d = await asyncio.to_thread(llm.chat, msgs, settings.cheap_model, 0.1)
                tokens["in"] += d["tokens_in"]; tokens["out"] += d["tokens_out"]
                g = await asyncio.to_thread(grounding.check, d["text"], hits, settings.smart_model, standalone)
                s.set({"draft": d["text"], "faithfulness": g.get("faithfulness")})
            if (g.get("faithfulness") or 0) >= 0.8:
                meta["route"] = {"model": settings.cheap_model, "reason": f"cascade: cheap draft grounded ({g['faithfulness']})"}
                meta["faithfulness"] = g.get("faithfulness")
                meta["ttft_ms"] = round((time.time() - t_start) * 1000, 1)
                yield "meta", meta
                final_text = d["text"]
                yield "token", {"t": red.restore(final_text)}
                yield "grounding", g
                await _finish(cfg, tid, question, final_text, emb, standalone, hits, cv, ptag, g, red)
                yield "done", {"trace_id": tid}
                tracing.end_trace(tid, red.restore(final_text), tokens_in=tokens["in"], tokens_out=tokens["out"],
                                  model=settings.cheap_model, ttft_ms=meta["ttft_ms"], meta=meta)
                return
            rt = {"model": settings.smart_model, "reason": f"cascade: escalated, cheap draft faithfulness={g.get('faithfulness')}"}
            meta["route"] = rt

        yield "meta", meta
        restorer = StreamRestorer(red)
        ttft = None
        with tracing.span("generate", {"model": rt["model"], "prompt_chars": len(prompt)}) as s:
            async for part in llm.astream(msgs, rt["model"], 0.1):
                if "token" in part:
                    if ttft is None:
                        ttft = (time.time() - t_start) * 1000
                        yield "ttft", {"ms": round(ttft, 1)}
                    final_text += part["token"]
                    out = restorer.feed(part["token"])
                    if out:
                        yield "token", {"t": out}
                else:
                    tokens["in"] += part["tokens_in"]; tokens["out"] += part["tokens_out"]
            tail = restorer.flush()
            if tail:
                yield "token", {"t": tail}
            s.set({"answer": final_text, "ttft_ms": ttft})
        meta["ttft_ms"] = round(ttft or 0, 1)
        yield "stage", {"stage": "generate", "ms": s.ms, "status": "ok",
                        "detail": f"{rt['model']} · TTFT {meta['ttft_ms']:.0f} ms · {tokens['out']} tokens"}

        g = {}
        if cfg.grounding:
            with tracing.span("grounding_check") as s:
                # Default: verify with the model that just answered - it's already in memory, so no
                # model swap (on a 16 GB machine a swap costs tens of seconds).
                g = await asyncio.to_thread(grounding.check, final_text, hits, cfg.grounding_model or rt["model"], standalone)
                s.set(g)
                meta["faithfulness"] = g.get("faithfulness")
                tokens["in"] += g.get("judge_tokens", 0)
            f = g.get("faithfulness")
            yield "stage", {"stage": "grounding", "ms": s.ms,
                            "status": "fail" if f is None else ("ok" if f >= 0.8 else "warn"),
                            "detail": "check failed" if f is None else f"faithfulness {f}"}
            yield "grounding", g
        await _finish(cfg, tid, question, final_text, emb, standalone, hits, cv, ptag, g, red)
        yield "done", {"trace_id": tid}
    except (asyncio.CancelledError, GeneratorExit):
        # Client went away mid-stream (#22). Breaking out of the Ollama stream closes the HTTP
        # connection, which makes Ollama stop generating - no wasted GPU on an abandoned answer.
        status = "client_disconnected"
        raise
    except Exception as e:
        status = "error"
        meta["error"] = repr(e)
        yield "error", {"message": repr(e), "trace_id": tid}
    finally:
        if db_trace_open(tid):
            meta["cost"] = {"tokens_in": tokens["in"], "tokens_out": tokens["out"],
                            "api_equivalent_usd": round(llm.api_cost(tokens["in"], tokens["out"]), 6)}
            tracing.end_trace(tid, red.restore(final_text), status=status, tokens_in=tokens["in"],
                              tokens_out=tokens["out"], model=meta.get("route", {}).get("model"),
                              ttft_ms=meta.get("ttft_ms"), meta=meta)


def db_trace_open(tid: str) -> bool:
    from app import db
    row = db.one("SELECT ended FROM traces WHERE id=?", (tid,))
    return bool(row) and row["ended"] is None


def _avg_miss_ms() -> float:
    from app import db
    row = db.one("SELECT AVG(total_ms) AS m FROM traces WHERE kind='chat' AND model NOT IN ('cache','none') "
                 "AND status='ok'")
    return row["m"] or 0.0


async def _finish(cfg, tid, question, answer, emb, standalone, hits, cv, ptag, g, red) -> None:
    grounded = (g.get("faithfulness") or 0) >= 0.8 if g else True
    if cfg.use_cache:
        if grounded and not grounding.is_abstention(answer):
            await asyncio.to_thread(cache.store, standalone, emb or llm.embed_one(standalone), answer,
                                    _sources(hits), cv, ptag)
    if cfg.memory:
        memory.add_message(cfg.session_id, "user", question)
        memory.add_message(cfg.session_id, "assistant", answer)
        asyncio.get_running_loop().run_in_executor(None, _bg_memory, cfg.user_id, cfg.session_id)


def _bg_memory(user_id: str, session_id: str) -> None:
    try:
        memory.extract_and_store(user_id, session_id)
        memory.maybe_summarize(session_id)
    except Exception:
        pass


async def collect(cfg: ChatConfig) -> dict:
    out = {"answer": "", "meta": {}, "grounding": {}}
    async for ev, data in run_chat(cfg):
        if ev == "token":
            out["answer"] += data["t"]
        elif ev == "meta":
            out["meta"] = data
        elif ev == "grounding":
            out["grounding"] = data
        elif ev == "ttft":
            out["ttft_ms"] = data["ms"]
        elif ev == "error":
            out["error"] = data["message"]
        elif ev == "done":
            out["trace_id"] = data["trace_id"]
    return out

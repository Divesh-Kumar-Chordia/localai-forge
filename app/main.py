"""FastAPI entry point.  Run:  uv run uvicorn app.main:app --port 8000"""
import os

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import agent, cache, db, debug, evals, labs, llm, memory, prompts, tracing, workflow
from app.config import ROOT, settings
from app.pipeline import ChatConfig, collect, run_chat
from app.rag import store
from app.rag.retrieve import rerank_scores, retrieve
from app.security import Redactor, injection_scan

log = logging.getLogger("forge")
STATE = {"warm": {}}


def _warm():
    STATE["warm"] = llm.warmup()
    try:
        rerank_scores([("warm", "up")])
        STATE["warm"]["reranker"] = "ok"
    except Exception as e:
        STATE["warm"]["reranker"] = f"error: {e}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    prompts.seed()
    if store.corpus_version() == "empty":
        log.warning("Empty index - ingesting sample corpus")
        await asyncio.to_thread(store.ingest_corpus)
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(title="LocalAI Forge", version="1.0", lifespan=lifespan)


# ------------------------------------------------------------------ health / corpus
@app.get("/health")
def health():
    try:
        models = [m.model for m in llm.client.list().models]
        ok = True
    except Exception as e:
        models, ok = [str(e)], False
    return {"ok": ok, "ollama_models": models, "warmup": STATE["warm"], "corpus_version": store.corpus_version(),
            "config": {"smart": settings.smart_model, "cheap": settings.cheap_model, "embed": settings.embed_model}}


@app.post("/warmup")
def warmup():
    _warm()
    return STATE["warm"]


@app.get("/corpus")
def corpus_stats():
    return store.stats()


@app.post("/ingest/corpus")
def ingest_corpus():
    return store.ingest_corpus()


@app.post("/ingest")
async def ingest(files: list[UploadFile]):
    out = []
    for f in files:
        raw = await f.read()
        if f.filename.lower().endswith(".pdf"):
            tmp = settings.data_dir / "uploads" / f.filename
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(raw)
            text = store.load_text(tmp)
        else:
            text = raw.decode("utf-8", errors="ignore")
        out.append(await asyncio.to_thread(store.ingest_text, text, f"uploads/{f.filename}"))
    return {"documents": out, "corpus_version": store.finish_ingest()}


@app.get("/chunks")
def chunks(doc: str, strategy: str = "recursive"):
    return db.query("SELECT id, section, content FROM chunks WHERE doc=? AND strategy=? ORDER BY rowid", (doc, strategy))


# ------------------------------------------------------------------ chat (SSE, #22)
@app.post("/chat")
async def chat(cfg: ChatConfig, request: Request):
    async def events():
        gen = run_chat(cfg)
        try:
            async for ev, data in gen:
                if await request.is_disconnected():
                    log.info("client disconnected; cancelling generation")
                    break
                yield {"event": ev, "data": json.dumps(data, default=str)}
        finally:
            await gen.aclose()   # closes the Ollama stream -> generation stops server-side
    return EventSourceResponse(events(), ping=15)


@app.post("/chat/sync")
async def chat_sync(cfg: ChatConfig):
    return await collect(cfg)


class RetrieveReq(BaseModel):
    query: str
    strategy: str = "recursive"
    mode: str = "hybrid"
    rerank: bool = True
    top_n: int = 5


@app.post("/retrieve")
def do_retrieve(r: RetrieveReq):
    out = retrieve(r.query, r.strategy, r.mode, r.rerank, None, r.top_n)
    out["stages"] = {k: v[:10] for k, v in out["stages"].items()}
    return out


class DebugReq(BaseModel):
    question: str
    expected_doc: str | None = None
    expected_text: str | None = None
    strategy: str = "recursive"
    mode: str = "hybrid"
    rerank: bool = True


@app.post("/debug")
def do_debug(r: DebugReq):
    return debug.diagnose(**r.model_dump())


# ------------------------------------------------------------------ agents
@app.post("/agent")
async def run_agent(cfg: agent.AgentConfig):
    return await asyncio.to_thread(agent.run_agent, cfg)


class Approval(BaseModel):
    trace_id: str
    approved: bool


@app.post("/agent/approve")
def approve(a: Approval):
    return agent.approve(a.trace_id, a.approved)


@app.get("/agent/outbox")
def outbox():
    return db.get_meta("outbox", [])


class WorkflowReq(BaseModel):
    task: str


@app.post("/workflow")
async def run_workflow(r: WorkflowReq):
    return await asyncio.to_thread(workflow.run_workflow, r.task)


# ------------------------------------------------------------------ observability
@app.get("/traces")
def traces(kind: str | None = None, limit: int = 50):
    sql = "SELECT id, kind, status, model, prompt_version, started, ttft_ms, total_ms, tokens_in, tokens_out, input " \
          "FROM traces"
    args: tuple = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    return db.query(sql + " ORDER BY started DESC LIMIT ?", (*args, limit))


@app.get("/traces/{tid}")
def trace(tid: str):
    t = tracing.get_trace(tid)
    if not t:
        raise HTTPException(404)
    return t


@app.get("/metrics")
def metrics():
    return {**labs.metrics(), "cache": cache.stats()}


@app.get("/cache")
def cache_stats():
    return cache.stats()


class Probe(BaseModel):
    a: str
    b: str


@app.post("/cache/probe")
def cache_probe(p: Probe):
    return cache.probe(p.a, p.b)


@app.delete("/cache")
def cache_clear():
    cache.clear()
    return {"cleared": True}


# ------------------------------------------------------------------ prompts (#24)
@app.get("/prompts")
def list_prompts():
    return {"prompts": prompts.listing(), "events": prompts.events()}


class NewPrompt(BaseModel):
    name: str
    template: str
    note: str = ""
    activate: bool = False


@app.post("/prompts")
def new_prompt(p: NewPrompt):
    return {"version": prompts.create(p.name, p.template, p.note, p.activate)}


@app.post("/prompts/{name}/activate/{version}")
def activate(name: str, version: int):
    prompts.set_active(name, version)
    return {"active": version}


@app.post("/prompts/{name}/rollback")
def rollback(name: str):
    return {"active": prompts.rollback(name)}


@app.post("/prompts/{name}/gate/{version}")
async def gate(name: str, version: int, limit: int = 6):
    return await evals.promotion_gate(name, version, limit)


# ------------------------------------------------------------------ evals
class RetrievalEvalReq(BaseModel):
    configs: list[dict]
    k: int = 5


@app.post("/eval/retrieval")
def eval_retrieval(r: RetrievalEvalReq):
    return evals.retrieval_eval(r.configs, r.k)


class GenEvalReq(BaseModel):
    config: dict = {}
    limit: int | None = 10
    judge_model: str | None = None
    name: str = ""
    ids: list[str] | None = None


@app.post("/eval/generation")
async def eval_generation(r: GenEvalReq):
    return await evals.generation_eval(r.config, r.limit, r.judge_model, r.name, r.ids)


@app.get("/eval/runs")
def eval_runs():
    return evals.runs()


@app.get("/eval/runs/{rid}")
def eval_run(rid: str):
    return evals.run_detail(rid)


@app.get("/eval/golden")
def eval_golden():
    return evals.golden()


@app.post("/eval/judge-calibration")
def judge_calibration():
    return evals.calibrate_judges()


class PairReq(BaseModel):
    question: str
    a: str
    b: str


@app.post("/eval/pairwise")
def pairwise(r: PairReq):
    return evals.pairwise(r.question, r.a, r.b)


# ------------------------------------------------------------------ labs, memory, security
@app.post("/lab/ttft")
async def lab_ttft(question: str = "What is the refund policy for annual plans?"):
    return await labs.ttft_lab(question)


@app.post("/lab/concurrency")
async def lab_concurrency(n: int = 4, model: str | None = None):
    return await labs.concurrency_lab(n, model)


@app.get("/memory/{user_id}")
def get_memory(user_id: str):
    return {"facts": memory.facts(user_id)}


class Text(BaseModel):
    text: str


@app.post("/security/redact")
def redact(t: Text):
    r = Redactor()
    red = r.redact(t.text)
    return {"redacted": red, "mapping": r.mapping, "restored": r.restore(red)}


@app.post("/security/scan")
def scan(t: Text):
    return injection_scan(t.text)


@app.get("/playbook", response_class=PlainTextResponse)
def playbook():
    return (ROOT / "docs" / "PLAYBOOK.md").read_text()


@app.get("/api/config")
def ui_config():
    return {"smart": settings.smart_model, "cheap": settings.cheap_model, "embed": settings.embed_model,
            "single_model": settings.smart_model == settings.cheap_model}


# The web UI. Mounted last so every API route above takes precedence.
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

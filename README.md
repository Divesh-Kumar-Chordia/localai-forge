# LocalAI Forge

**A private, fully local RAG + agent platform, built as a glass box.** It answers questions over a company's own documents with sentence-level citations, runs tool-using agents with deterministic guardrails, and makes every production concern visible and measurable: retrieval, caching, routing, hallucination checks, evals, prompt rollback, tracing, PII redaction, and prompt-injection defense.

Runs entirely on your machine with [Ollama](https://ollama.com) (Llama 3.1 8B / Llama 3.2 3B + nomic-embed-text). No API keys, and no data leaves the machine.

![Chat with live pipeline inspector](docs/img/chat.png)

## Why not just use ChatGPT?
A local 8B model will never out-reason a frontier model. This project competes on what general chatbots don't give you:

- **Data control.** Documents never leave the machine.
- **A persistent private corpus.** Your contracts, policies and tickets are indexed and versioned, not re-uploaded every chat.
- **Verifiable answers.** Every sentence is checked against its cited source, and every request has a full trace.
- **Engineering control.** Chunking, hybrid retrieval, reranking, routing, prompts, guardrails and evals are all owned and measured.

The moat is private data plus a specialised, observable system, not the model. Full reasoning is in **[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md)**.

## Quick start
```bash
# Prereqs: macOS/Linux, Ollama running, uv (https://docs.astral.sh/uv/)
cp .env.example .env     # optional: single-model mode for 16 GB machines
./run.sh                 # pulls models if missing, starts the server
open http://localhost:8000
```
API docs are at `http://localhost:8000/docs`. Stop with `./run.sh stop`.

## What's inside

| Page | Shows |
|---|---|
| **Chat** | Streaming answers with citation chips; the **Inspector** shows each pipeline stage live (PII → cache → retrieval → rerank → route → generate → grounding), source scores, and a per-sentence hallucination check. Stop mid-answer to see the server cancel generation. |
| **Agent Lab** | A LangGraph agent with tools, a loop guard, a circuit breaker, a prompt-injection attack against a poisoned document, a policy gate, and human approval. |
| **Traces & Workflows** | A multi-agent workflow (planner → researcher → writer → reviewer) and a span waterfall for every request. |
| **Retrieval Lab** | BM25 vs dense vs hybrid vs hybrid + rerank side by side, and a labelled benchmark (hit@k, MRR). |
| **Debug an answer** | Finds the first pipeline stage that lost the evidence. |
| **Knowledge base** | Four chunking strategies per document (fixed, recursive, legal-clause, parent-child) and an upload box. |
| **Metrics & Cost** | Latency percentiles per route, API-equivalent cost vs baseline, cache hit rate and near-miss histogram. |
| **Evals & Judge** | A 47-question golden set, LLM-as-judge, judge calibration vs human labels (Cohen's κ), and pairwise judging with position swap. |
| **Prompts** | A versioned registry, diffs, one-click rollback, and an eval-gated promotion. |
| **Security** | Reversible PII redaction and an injection scanner. |
| **Latency Lab** | TTFT breakdown (cold vs warm, model load vs prefill) and a concurrency test. |
| **Playbook** | 25 AI-engineering interview problems, each answered with trade-offs and a pointer to where it's implemented. |

<table><tr>
<td><img src="docs/img/traces.png" alt="Trace waterfall"></td>
<td><img src="docs/img/agent.png" alt="Agent lab"></td>
</tr><tr>
<td><img src="docs/img/retrieval.png" alt="Retrieval lab"></td>
<td><img src="docs/img/prompts.png" alt="Prompt registry"></td>
</tr></table>

## Architecture
```
Browser (vanilla JS, no build) ──HTTP/SSE──▶ FastAPI
   chat pipeline:  PII → memory → condense → semantic cache → dense ∥ BM25 → RRF → cross-encoder
                   → route → stream → grounding check → cache (grounded only) → trace
   agent (LangGraph):     decide → guard → act → … → finalize
   workflow (LangGraph):  planner → researcher → writer → reviewer ↺
        │
   Ollama (Metal) · Chroma (vectors) · SQLite (FTS5 BM25, traces, prompts, cache, memory, evals)
   · MiniLM cross-encoder (CPU)
```

| Layer | Choice | Why |
|---|---|---|
| LLMs | Llama 3.1 8B + Llama 3.2 3B via Ollama | Local, Metal GPU, JSON-schema constrained decoding |
| Embeddings | nomic-embed-text (task prefixes) | Local, strong for its size |
| Vectors / BM25 / state | Chroma · SQLite FTS5 · SQLite | Zero-ops; FTS5 is persistent and incremental |
| Reranker | ms-marco-MiniLM-L-6-v2 | ~170 ms / 20 pairs on CPU |
| Agents | LangGraph | Explicit state plus a guard node between decide and act |
| JSON | Ollama `format=` + Pydantic + repair loop | Output always parses |
| API / UI | FastAPI + SSE · vanilla JS/CSS | No build, no CDN, stays offline |

## Measured (Apple M5, 16 GB)
- Warm TTFT ≈ 1.5 s (3B) / 2.5–4 s (8B). Semantic-cache hits take 0.3–0.8 s end to end.
- Retrieval hit@1 on 43 labelled questions:

  | Config | hit@1 |
  |---|---|
  | Dense | 0.884 |
  | BM25 | 0.907 |
  | Hybrid | 0.907 |
  | Hybrid + rerank | 0.930 |
  | Legal chunking + hybrid + rerank | **0.977** |

- End-to-end eval (47 golden questions, prompt v3): **80.9% pass**, faithfulness 0.973, hallucination 4.3%, abstention accuracy 0.894. LLM judge calibrated against human labels: **κ 0.80, 0 false passes**.
- Real failures found while building it (runaway JSON generation, a torch/MPS segfault, semantic agent loops, stale cache keys, swap-induced latency, and more), each with its fix: see [System design §8](docs/SYSTEM_DESIGN.md#8-measured-results-apple-m5-16-gb).

## Repository layout
```
app/        FastAPI app: pipeline, rag/{chunking,store,retrieve}, cache, router, grounding, memory,
            agent, workflow, evals, debug, labs, prompts, security, tracing, llm, db, main
web/        UI: index.html, styles.css, app.js, md.js (no build step)
data/       corpus/ (fictional Acme Cloud KB + contracts + 1 poisoned doc), eval/ (golden set, judge labels),
            customers.json, tickets.json
scripts/    sse_client.py (disconnect demo), agent_demo.py, lora/ (5K-example MLX LoRA kit)
docs/       SYSTEM_DESIGN.md, PLAYBOOK.md, img/
```

## Docker
`docker compose up --build` runs the app in a container and talks to Ollama on the host. Ollama inside Docker on macOS has no GPU access and would run on CPU.

## Notes
- The corpus is a fictional company ("Acme Cloud"). Customer records and tickets contain synthetic PII for the redaction demos.
- `data/corpus/kb/community_tips.md` is **intentionally poisoned** with a prompt-injection payload for the Agent Lab.

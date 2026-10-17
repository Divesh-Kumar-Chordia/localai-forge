# The 25-problem playbook

Use one structure for every answer: **clarify → baseline → measure → levers (with trade-offs) → how I'd know it worked.**
Interviewers score the trade-offs and the measurement, not the vocabulary.

Real numbers from this app on an M5 / 16 GB (use them, they make answers concrete):
warm TTFT ≈ 1.5 s on the 3B model; cache hit ≈ 0.3 s total; reranking 20 candidates ≈ 170 ms on CPU;
FTS5 BM25 < 5 ms; the 8B model decodes ≈ 16 tok/s.

Retrieval hit@1 on 43 labelled questions:

| Config | hit@1 |
|---|---|
| Dense | 0.884 |
| BM25 | 0.907 |
| Hybrid | 0.907 |
| Hybrid + rerank | 0.930 |
| Legal chunking + hybrid + rerank | 0.977 |

At k=5 everything scores about 1.0 on a 12-doc corpus. That is itself a talking point: pick the metric that matches how many chunks you actually send.

---

## 1. Design a RAG system for 10M documents
**30-second answer.** Split it into an offline **ingestion plane** and an online **query plane**. Ingestion: object store → queue → stateless workers that parse, chunk, embed (GPU batches) and upsert by content hash. Query: auth/ACL filter → hybrid retrieval (vector + BM25) → rerank → generate with citations → cache. Scale the index by sharding, not by buying a bigger box.

**Numbers.** 10M docs × ~10 chunks = 100M vectors. At 768 dims in float32 that's ~300 GB raw, which drops to ~75 GB with int8 scalar quantization or ~10 GB with PQ. So: shard (by tenant or by hash), use HNSW per shard with quantized vectors in RAM and full vectors on disk for re-scoring. BM25 runs in OpenSearch/Elasticsearch.

**Trade-offs.**
- HNSW: fast and accurate, but RAM-hungry. IVF-PQ: cheap, but loses recall.
- pgvector: simple ops, fine up to ~10M vectors. Qdrant/Milvus: built for 100M+.
- Re-embedding everything when you change embedding models is expensive. Version the index and dual-write during migration.
- Freshness vs cost: run incremental updates via change-data-capture, not nightly full rebuilds.

**Show in app.** Corpus page (idempotent ingest, corpus version), Retrieval Lab.

**Follow-ups.** Multi-tenancy: a filter field vs. a collection per tenant (noisy neighbours, deletion compliance). Permission-aware retrieval: filter *before* top-k, never after, or you leak and lose recall. How to evaluate at that scale: sample stratified queries and label recall@k offline.

## 2. Your RAG gives wrong answers. Debug it
**30-second answer.** Bisect the pipeline backwards and find the first stage where the right evidence is lost:
1. **Ingestion:** is the fact in a chunk at all?
2. **Retrieval:** is it among the ~20 candidates?
3. **Ranking:** did it survive into the top-k sent to the model?
4. **Generation:** it was in the prompt, but the answer is still wrong.

Each stage has different fixes. Then add the failing question to the golden set.

**Fixes by stage.**
- Ingestion: chunking strategy, PDF/table loader.
- Retrieval: hybrid, query rewrite/HyDE, better embedding, metadata filters.
- Ranking: reranker, larger k.
- Generation: prompt rules, temperature, fewer chunks (lost-in-the-middle), larger model.

**Show in app.** *Debug a wrong answer*: start with fixed chunks + dense + no reranker on "cap for data protection breaches at Globex", watch the verdict, then fix stage by stage.

**Follow-ups.** "It's only wrong sometimes": check non-determinism (temperature), cache serving a stale answer, a different chunking or index version, or conversation context leaking in through query condensation.

## 3. Pick a chunking strategy for legal contracts
**30-second answer.** Chunk on **structure**: one chunk per clause or sub-clause, prefixed with a contextual header `[Agreement > 9. Limitation of Liability]`. Keep cross-references resolvable (9.3 modifies 9.2), so either keep sub-clauses with their parent section or use parent-child: retrieve the small child, send the parent section to the model.

**Why not fixed-size?** It cuts "except as provided in Section 9.3" away from the cap it modifies. Definitions live in a different section, so consider attaching the defined terms used in each chunk.

**Trade-offs.**
- Bigger chunks: more context, less precise embedding, more tokens.
- Smaller chunks: precise, but lose meaning.
- Parent-child gives precise matching *and* full context, at the cost of extra storage and a lookup.

**Show in app.** Retrieval Lab → Chunk inspector on `msa_globex.md` (fixed vs legal); the benchmark's "Chunking strategy" experiment.

**Follow-ups.** Scanned PDFs (OCR + layout model). Tables (serialize rows with headers). Amendments overriding the base contract (metadata `effective_date`, `supersedes`; retrieve the latest version).

## 4. BM25 vs dense vs hybrid: when to use each
- **BM25:** exact tokens. Error codes, SKUs, names, clause numbers, rare jargon, out-of-vocabulary terms the embedder has never seen. Cheap, explainable, no GPU.
- **Dense:** paraphrase and intent ("get my money back" → refund policy), multilingual, typos.
- **Hybrid:** the default for production. Reciprocal Rank Fusion, `Σ 1/(60 + rank)`, fuses *ranks*, so there's no score normalization to tune. Weighted score fusion needs calibration per domain.

**Show in app.** Retrieval Lab side-by-side with `E-2001`, `SSO-422` (BM25 wins) vs "money back after a week" (dense wins), then the labelled benchmark.

**Follow-ups.** Learned sparse retrieval (SPLADE) as a middle ground. When would you skip dense? A tiny, keyword-heavy corpus, or strict latency/cost limits.

## 5. Add a reranker and measure if it helped
**30-second answer.** A cross-encoder scores (query, chunk) jointly, which is far more accurate than bi-encoder cosine but costs O(n) model calls. So retrieve ~20–50 cheaply, rerank, and keep 3–5. Measure on a **labelled set**: hit@k, MRR, nDCG for retrieval, *and* end-to-end answer pass rate, *and* p50/p95 latency. It "helped" only if answer quality goes up at an acceptable latency cost.

**Gotcha.** MRR can improve while hit@5 stays flat: the reranker reorders inside the window and the answer doesn't change. The real win is often **fewer chunks**, i.e. lower cost and TTFT at equal quality.

**Show in app.** Retrieval benchmark (hybrid vs hybrid+rerank), the Evals A/B with rerank on/off, and rerank_ms in traces.

**Follow-ups.** Reranker scores as a **confidence gate**: below threshold, abstain (implemented: `abstain_below`). Alternatives: bge-reranker-v2-m3 (multilingual, slower), LLM-as-reranker (expensive), API rerankers.

## 6. Reduce LLM cost by 70% without hurting quality
**30-second answer.** Measure where tokens go first (per stage, per route), then stack levers and guard each with the eval suite:
1. Semantic cache: a hit costs zero tokens.
2. Routing/cascade: send easy queries to a small model (often 60–80% of traffic).
3. Shrink input: rerank to 5 chunks instead of 20. Input tokens dominate RAG cost.
4. Prompt compression / shorter system prompt; provider prompt caching for the static prefix.
5. Cap output (`max_tokens`, concise style).
6. Batch API / off-peak for offline jobs.
7. Fine-tune a small model for a high-volume narrow task.

Each one compounds: 0.7 × 0.5 × 0.6 ≈ 0.21 of the original cost.

**Show in app.** Metrics page: API-equivalent cost vs the "all 8B, no cache" baseline, plus requests by route.

**Follow-ups.** How do you know quality didn't drop? A/B on the golden set plus online metrics (thumbs, escalations). Where would you NOT cut? Legal/medical answers, which should be forced to the smart model.

## 7. Cut first-token latency for a chat app
**30-second answer.** TTFT = queue + model load + prefill (prompt length) + first decode step, plus everything *before* the LLM (retrieval, rewrite, guardrails).
- **Before the LLM:** parallelise retrieval steps, skip the query rewrite when there's no history, cache embeddings, keep the reranker warm.
- **Model:** keep it resident (`keep_alive`, warmup on boot), use a smaller model for easy queries, speculative decoding.
- **Prefill:** fewer, better chunks; prefix/KV caching of the system prompt.
- **UX:** stream immediately; run the grounding check and memory writes *after* the stream.

**Show in app.** Serving lab TTFT breakdown: cold vs warm (model load), 20 vs 3 chunks (prefill), 8B vs 3B. Watch the cold start and the `num_predict` runaway story (below).

**War story (true, from building this).** A grounding call under a JSON grammar with no `num_predict` cap generated 5,700 tokens. With `OLLAMA_NUM_PARALLEL=1` it blocked every other request, so TTFT went to infinity for everyone. Fix: hard output caps on every call plus `maxItems` in schemas. Lesson: tail latency is usually one bad request holding a shared resource.

## 8. Design a semantic cache with hit-rate tracking
**Design.** Embed the normalized query, then nearest neighbour among entries *with the same corpus version and prompt version* (otherwise rollbacks and re-ingests serve stale answers). Hit if cosine ≥ threshold and the **guards** pass:
- negation mismatch ("cancel" vs "don't cancel" embed ~0.95),
- different numbers or entities,
- per-user/tenant scope for anything personalised.

Only cache **grounded** answers. Set a TTL. Track hit rate, latency saved, and the **near-miss similarity histogram** to tune the threshold. Measure false-hit rate by sampling hits and judging them.

**Trade-offs.**
- Threshold too low: wrong answers served fast, which is the worst failure.
- Threshold too high: no hits.
- An exact-match cache in front is free and safe. A semantic cache is a precision/recall knob.

**Show in app.** Chat (ask, then paraphrase it: 0.3 s hit), Metrics cache probe with negation, and the histogram.

## 9. Route requests between a cheap and a smart model
**Options, simplest first.**
1. **Rules** (length, reasoning keywords, multi-part, legal source, low retrieval confidence): zero latency, explainable.
2. **Cascade:** cheap model drafts, a verifier (grounding / self-check) escalates on failure. Pays double on hard queries.
3. **Classifier:** a small model trained on logged (query → was the cheap answer good enough) labels; the production upgrade.
4. LLM-as-router: flexible, but adds a call.

**Measure.** Quality vs % of traffic routed cheap, plotted as a curve; pick the operating point. Watch for drift.

**Show in app.** Chat sidebar router modes; the route reason is shown per answer; Metrics by model; Evals A/B (rules vs smart).

## 10. Build an agent that uses tools reliably
**Principles.**
- Few tools, with sharp names and descriptions.
- Structured outputs (grammar-constrained JSON) for the action, plus schema validation and a repair retry.
- A **deterministic guard between "decide" and "act"**: validation, permissions, budgets.
- Idempotent tools with timeouts and retries.
- Observations truncated and summarised.
- A max-step budget, and an explicit `final_answer` action.
- Eval with trajectories: did it use the right tools in a sensible order?

Small models: one action per step, low temperature, show the step count in the prompt.

**Show in app.** Agent Lab "Multi-tool" (lookup → calculator → search → calculator): it computes Hooli's $144 → $240. Point at the bug I found: `lookup_customer("Hooli")` returned Globex because `"" in "hooli"` is True. Tool bugs look like model hallucinations.

## 11. Your agent gets stuck in a loop. Fix it
**Layers.**
1. Exact repeat of (tool, normalized args) → intervene by feeding back the earlier result plus "don't repeat".
2. **Semantic loops**: normalize args (sorted token set) so rephrasings count as repeats.
3. A **per-tool circuit breaker** (2 failures → stop calling it).
4. No-progress detection (identical observations).
5. Hard step, token and time budgets.
6. A graceful stop: produce a best-effort answer from what was gathered and say what's missing.

Root causes to fix as well: ambiguous tool errors ("try again" invites retries), missing tools, the model not seeing its own history.

**Show in app.** Agent Lab "Loop: broken search tool", guard on vs off. Real finding: Llama 3.1 rephrased the query each time, so the exact-match guard alone didn't fire. That's why the normalization and the circuit breaker exist.

## 12. Design memory for a long-running assistant
**Tiers.**
- **Working memory:** last N turns verbatim.
- **Episodic summary:** rolling summary of older turns.
- **Semantic long-term memory:** extracted facts (preferences, profile, ongoing tasks) embedded and scoped per user.

**Write path.** Extract facts after the turn (async), de-duplicate (similarity > 0.97), **supersede** conflicting facts rather than delete (audit trail), with TTL/decay for tasks.

**Read path.** Retrieve the top facts relevant to the current query; never dump everything.

**Concerns.**
- Privacy: user-visible and deletable (GDPR), PII-redacted.
- Memory poisoning: don't store instructions from documents.
- Cost: extraction on a cheap model.

**Show in app.** Agent Lab "remember a preference" then "recall it"; Chat with memory on and a follow-up question (the condensed query is shown).

## 13. Make LLM output always valid JSON
**Layers.**
1. **Constrained decoding:** JSON schema compiled to a grammar (Ollama `format=`, vLLM guided decoding, OpenAI/Anthropic structured outputs). The output always parses.
2. **Pydantic validation** for semantics the grammar can't express (ranges, cross-field rules).
3. **Repair loop:** send the validation error back, retry ≤2.
4. Hard `max_tokens`; bounded arrays (`maxItems`); a fallback or default path.

Prompting "return JSON" alone gives ~95% validity on small models, which isn't enough for automation.

**Show in app.** Every agent step, the grounding verdicts, the judge scores and the planner all use `llm.structured()`. Mention the runaway-generation bug and the fix (#7).

## 14. Detect and reduce hallucinations
**Detect.**
- Sentence-level grounding: each sentence is checked against its cited source (NLI or LLM, with an evidence quote *before* the verdict).
- Citation validity.
- Self-consistency (sample n, compare).
- Online: user feedback, escalation rate.

**Reduce.**
- Better retrieval first: most "hallucinations" in RAG are retrieval misses.
- An abstain rule in the prompt plus a **confidence gate** (no relevant chunk → don't call the LLM).
- Low temperature; citations required.
- Smaller context (lost-in-the-middle).
- Route hard queries to the larger model.
- Don't cache ungrounded answers.

**Two real failure modes I hit.** (1) *False positive:* the checker didn't see the question, so "No, SCIM is Enterprise only [1]" was flagged; fixed by giving it the question. (2) *False negative:* the answer said Business restores for "90 days" (the Pro value) and the check passed because "90 days" is in the source. Value-to-entity attribution in lists is a known weak spot of sentence-level NLI-style checks.

**Over-correction risk.** A strict abstain rule cuts hallucinations but causes over-abstention (5/47 here). The fix is a prompt that answers *implied* facts, verified by the eval gate rather than by vibes.

**Show in app.** Chat faithfulness badge plus unsupported sentences; "Who is the CEO?" abstains. Activate prompt v2 and watch grounding fall to 0%.

## 15. Build an eval suite for a customer support bot
**Dataset.**
- 50–500 questions from real tickets, stratified: FAQ, account-specific, multi-doc, policy edge cases, **out-of-scope** (must abstain), adversarial (injection), multilingual.
- Each with a reference answer and relevant doc IDs.
- Version it; add every production failure.

**Metrics.**
- Retrieval: hit@k, MRR (fast, no LLM, run on every change).
- Answers: correctness via a judge against the reference, faithfulness, abstention accuracy, tone/policy compliance.
- Ops: TTFT, latency, cost.

**Process.** Offline in CI as a gate; online on sampled traffic, with humans reviewing disagreements.

**Real numbers from this app (47 questions, prompt v3):** pass 80.9%, faithfulness 0.973, hallucination 4.3%, abstention accuracy 0.894, citation coverage 0.95. Reading every failure by hand found the *real* problem: 5 over-abstentions where the answer had to be inferred, not retrieval. Three "failures" were bugs in my own eval harness. **Always read the failures before trusting the number.** A targeted prompt fix (v4: answer implied facts) then went through the full gate: **pass 85.1% → 95.7%**, abstention 0.894 → 0.979, latency p50 12.6 s → 8.5 s, at the cost of faithfulness 1.0 → 0.972.

**Show in app.** Evals page (47 questions, A/B, history), Retrieval benchmark, Prompt gate.

## 16. LLM-as-a-judge: how to make it trustworthy
**Practices.**
- Narrow rubric with anchored scores; reasoning before score; temperature 0; structured output.
- Reference-based where possible.
- **Calibrate against ~50–200 human labels**: report agreement and Cohen's κ, false-pass rate (most dangerous), false-fail.
- Pairwise comparisons **with position swap**: only consistent wins count.
- Judge from a different model family than the generator (self-preference bias).
- Watch for verbosity bias.
- Re-calibrate when the judge model changes.
- Humans spot-check where the judge disagrees with itself.

**What I actually measured.** A holistic 1–5 rubric on Llama 3.1 8B reached only κ 0.52 against my 20 human labels. It docked "$9.60 per user per month billed annually" for "missing that it's billed annually". Redesigned so the model only makes small checkable decisions (is key fact X present? anything contradicted?), the **score is computed in code**, numbers are compared deterministically, and out-of-scope questions are graded by rule: **κ 0.80, agreement 0.90, 0 false passes**. Lesson: *constrain the judge's job until a small model can do it reliably, then measure it like any other model.*

**Show in app.** Evals → Judge calibration (both designs against 20 human labels) and Pairwise. Name the limitation honestly: here both judge and generator are Llama.

## 17. Block prompt injection in a tool-using agent
**Threat.** *Indirect* injection: instructions hidden in retrieved docs, emails or web pages. The model can't reliably tell data from instructions, so **assume the model will be compromised and limit the blast radius**:
1. Least-privilege tools per task (read-only by default).
2. Spotlight/delimit untrusted text.
3. Scan and filter tool outputs (a tripwire, not a guarantee).
4. **Deterministic policy on side effects**: recipient allowlists, taint tracking (untrusted content in context → side effects need approval).
5. Human-in-the-loop for irreversible actions.
6. Output filtering (no secrets or URLs with data in them).
7. Log and alert.

**Show in app.** Agent Lab "Injection: poisoned doc" with *simulate compromised model*. Defenses off: the customer list is emailed to the attacker (check the outbox). Defenses on: BLOCKED. Real observation: Llama 3.1 ignored the injection on its own. Point out that you can't ship relying on that.

## 18. Redact PII before it hits the model
**Pipeline.**
1. Detect: regex with validators (Luhn, checksum formats) plus NER for names and addresses (Presidio, GLiNER).
2. Replace with **reversible, consistent placeholders** (`<EMAIL_1>` is the same everywhere in the conversation, so the model can still reason about it).
3. The mapping stays server-side.
4. Restore in the output, stream-safe.

Redact **before** logs, traces, caches and embeddings too. Tune for recall (a missed PII is worse than over-redaction), and measure on a labelled PII set.

**Trade-offs.** Over-redaction hurts answers ("call <PHONE_1>" is fine; redacting product codes isn't). Use allowlists for internal IDs.

**Show in app.** Security page (the card number passes Luhn; an order number that fails Luhn is left alone); Chat with an email in the question; agent ticket lookups are redacted.

## 19. Fine-tune vs RAG vs prompting: pick one and defend it
- **Prompting first:** cheapest, fastest iteration. The ceiling is the model's knowledge and context length.
- **RAG** when the problem is **knowledge**: facts change, need citations, per-user access control, large or private corpus. Updates are instant (re-index), and it's auditable.
- **Fine-tuning** when the problem is **behaviour/format/style**, or a **narrow, high-volume task** where a small tuned model replaces a big prompted one (cost and latency), or a domain language the base model handles badly. It's bad for injecting changing facts: stale, unciteable, hallucination-prone.
- **Often both:** a fine-tuned small model *inside* a RAG system (e.g. tuned to cite and abstain).

**My pick for a support bot: RAG**, because policies change weekly, answers need citations, and data is per-customer. Fine-tune later for the ticket classifier/router.

## 20. Fine-tune a small model with LoRA on 5K examples
**Plan.**
- **Data:** clean, dedupe, 90/10 split, consistent chat template, include hard negatives. Quality beats quantity: 5K good examples is plenty for a format/classification task.
- **Model:** 1–3B instruct (Llama 3.2 1B/3B, Qwen).
- **LoRA:** rank 8–16, alpha 2× rank, dropout 0.05, on attention (+MLP) projections, lr 1e-4–2e-4, 2–3 epochs, cosine schedule, mask the prompt tokens in the loss.
- **QLoRA (4-bit base)** to fit on small GPUs.
- **Evaluate** against the prompted baseline on a held-out set, and check general capability didn't regress.
- **Ship:** merge the adapter or serve multi-LoRA (vLLM/LoRAX), quantize to GGUF for Ollama.

**Done on this Mac (MLX, ~20 min).** Llama 3.2 1B (4-bit), LoRA on 8 layers = 2.8M trainable params (0.23%), 300 iters. On 250 held-out tickets, all-three-fields accuracy: base with schema only **0%**, base with the label set + an example in the prompt (*the fair baseline*) **9.6%**, LoRA **100%**, and 40% faster (308 vs 519 ms) because the label prompt is gone.

**What to say about the 100%:** the test split comes from the same templates as the training data, so it measures template learning (leakage), not production accuracy. Before shipping: evaluate on real tickets, add paraphrased and noisy inputs, and compare against a prompted 8B. *Always compare against a fair baseline: "0% → 100%" was the unfair one.*

**Follow-ups.** Overfitting (val loss up, memorized outputs). Catastrophic forgetting (mix in general data). LoRA vs full fine-tune: LoRA is ~0.1–1% of params, swappable, nearly the same quality for narrow tasks.

## 21. Serve an open model at scale (batching, KV cache, quantization)
- **Batching:** static batching wastes GPU waiting for the longest sequence. **Continuous batching** (vLLM, TGI, SGLang) lets requests join and leave every decode step, for 5–20× throughput.
- **KV cache:** memory grows with batch × context. **PagedAttention** allocates KV in pages (no fragmentation, more concurrent sequences); **prefix caching** shares system-prompt KV across requests; KV quantization (FP8) doubles capacity.
- **Quantization:** weights in int4 (AWQ/GPTQ) or FP8 mean less memory, faster memory-bound decode, and a small quality loss. Measure it on your eval set.
- **Parallelism:** tensor parallel inside a node, pipeline parallel across nodes, replicas behind a load balancer.
- **Speculative decoding:** a small draft model or n-gram guesses, the big model verifies.
- **Ops:** autoscale on queue depth and KV utilisation; separate prefill and decode pools at very large scale; SLOs on TTFT and inter-token latency.

**Show in app.** Serving lab concurrency: with Ollama's default `NUM_PARALLEL=1`, TTFT grows linearly with N. Restart with `OLLAMA_NUM_PARALLEL=4` and compare. Then explain why vLLM is the production answer.

## 22. Stream responses over SSE and handle disconnects
**Design.**
- SSE (one-way, works through HTTP proxies, auto-reconnect with `Last-Event-ID`) with typed events: `meta`, `token`, `grounding`, `done`, `error`.
- Heartbeat pings every 15 s (proxies kill idle connections).
- Disable proxy buffering (`X-Accel-Buffering: no`).

**On disconnect.** Detect it (`request.is_disconnected()` / CancelledError), **cancel the upstream generation** (close the LLM stream so the GPU stops), and record the partial trace as `client_disconnected`. Optionally persist the partial answer so a reconnect can resume (event IDs plus a buffer keyed by message ID).

**WebSockets vs SSE.** WebSockets are bi-directional (use them for voice or collaborative editing), but there's more infra to handle (sticky sessions, proxies).

**Show in app.** `uv run python scripts/sse_client.py "explain SSO setup" --disconnect-after 5`, then open the trace (status `client_disconnected`).

## 23. Trace every step of a multi-agent workflow
**Design.** One `trace_id` per user request, propagated through every agent, tool call and LLM call as **spans** with parent IDs (OpenTelemetry semantics). Each span records name, timing, inputs/outputs (redacted and truncated), model, tokens, prompt version and errors. Export via OTel to Langfuse, Phoenix or Jaeger. **Sample** (100% of errors, a few % of successes). Link traces to eval results and user feedback.

**Uses.** Debug *which agent* failed, find latency hot spots (waterfall), attribute cost per agent, replay a request.

**Show in app.** Multi-agent & Traces: planner → researcher (per sub-question: retrieve + LLM) → writer → reviewer → revision, as a waterfall. Same view for chat and agent traces.

## 24. Roll back a bad prompt change in production
**Design.**
- Prompts in a versioned registry (not hard-coded) with an **active pointer** per environment; every response is tagged with the prompt version.
- **Ship:** offline eval gate (golden set must not regress) → canary (e.g. 5% by user hash) → watch online metrics (grounding score, thumbs-down, escalations) → promote.
- **Rollback:** flip the pointer (seconds, no deploy); automatic on metric breach.
- Keep an audit log. Remember to invalidate caches keyed on the old version.

**Show in app.** Prompts page: activate `rag_answer` v2 (drops the citation and abstain rules), ask "Who is the CEO of Acme Cloud?" (an uncited, chatty answer; grounding falls to 0%), then Rollback. Run the eval gate on v2: BLOCKED on citation coverage 0.9 → 0.0, faithfulness 1.0 → 0.875, pass rate 0.75 → 0.625.

**War story (real).** My first gate run *promoted* the bad prompt. It sampled the first 6 golden questions (all easy pricing, no out-of-scope questions), citations weren't a gated metric, and the judge was biased toward verbose answers. Fixes: stratified sampling, citation coverage gated, a fact-checklist judge. A gate is only as good as its sample and its metrics, so test the gate itself with a known-bad change.

## 25. Design ChatGPT-style chat for 1M daily users
**Back of the envelope.** 1M DAU × 10 messages = 10M messages/day ≈ 115/s average, ~500/s peak. At ~500 output tokens each, that's ~250K tokens/s at peak, which means hundreds of GPUs with vLLM plus aggressive caching and routing.

**Architecture.**
- **Edge:** CDN, auth, rate limits per user/tier.
- **Chat service:** stateless, SSE streaming.
- **Conversation store:** Postgres or DynamoDB, sharded by user_id; recent context in Redis.
- **LLM gateway:** routing, retries, fallbacks across providers/models, quotas, prompt-version tagging.
- **Inference fleet:** vLLM, autoscaled on queue depth.
- **Async workers:** titles, summaries, memory extraction, moderation.
- **Supporting:** object storage for files, vector DB for user-file RAG and memory, observability (OTel), an eval pipeline, and abuse/moderation.

**Key trade-offs.**
- Context-window cost: summarise long threads.
- Consistency of conversation history across regions.
- Graceful degradation: smaller model under load, queue with ETA.
- Cost per DAU: cache, route, cap tokens.

**This app is the single-node slice** of that design: same components (gateway logic, routing, cache, memory, tracing, evals), local models.

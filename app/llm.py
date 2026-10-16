"""Thin wrapper over the local Ollama server: chat, streaming, structured JSON, embeddings."""
import json
import time
from typing import Iterator, Type, TypeVar

import ollama
from pydantic import BaseModel, ValidationError

from app.config import settings

client = ollama.Client(host=settings.ollama_host)
aclient = ollama.AsyncClient(host=settings.ollama_host)
T = TypeVar("T", bound=BaseModel)


def _opts(temperature: float, max_tokens: int = 700, **extra) -> dict:
    # num_predict is a hard safety cap: without it a confused model (esp. under a JSON grammar)
    # can generate thousands of tokens and, with OLLAMA_NUM_PARALLEL=1, block every other request.
    return {"temperature": temperature, "num_ctx": settings.num_ctx, "num_predict": max_tokens, **extra}


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.2, **opts) -> dict:
    """Blocking chat. Returns text + token counts + latency."""
    t0 = time.time()
    r = client.chat(model=model or settings.smart_model, messages=messages,
                    options=_opts(temperature, **opts), keep_alive=settings.keep_alive)
    return {
        "text": r["message"]["content"],
        "tokens_in": r.get("prompt_eval_count") or 0,
        "tokens_out": r.get("eval_count") or 0,
        "ms": (time.time() - t0) * 1000,
        "model": model or settings.smart_model,
    }


async def astream(messages: list[dict], model: str | None = None, temperature: float = 0.2):
    """Async token stream. Yields dicts: {'token': str} ... then {'done': True, tokens...}."""
    stream = await aclient.chat(model=model or settings.smart_model, messages=messages, stream=True,
                                options=_opts(temperature), keep_alive=settings.keep_alive)
    async for part in stream:
        if part.get("done"):
            yield {"done": True, "tokens_in": part.get("prompt_eval_count") or 0,
                   "tokens_out": part.get("eval_count") or 0}
        else:
            yield {"token": part["message"]["content"]}


def structured(messages: list[dict], schema: Type[T], model: str | None = None,
               retries: int = 2, temperature: float = 0.0, max_tokens: int = 600) -> tuple[T, dict]:
    """Always-valid JSON (#13).

    Layer 1: Ollama grammar-constrained decoding with format=<JSON schema> -> output always parses.
    Layer 2: Pydantic validation (semantic rules the grammar can't express).
    Layer 3: repair loop - feed the validation error back to the model and retry.
    """
    msgs = list(messages)
    info = {"attempts": 0, "errors": [], "tokens_in": 0, "tokens_out": 0}
    for _ in range(retries + 1):
        info["attempts"] += 1
        r = client.chat(model=model or settings.smart_model, messages=msgs,
                        format=schema.model_json_schema(), options=_opts(temperature, max_tokens),
                        keep_alive=settings.keep_alive)
        info["tokens_in"] += r.get("prompt_eval_count") or 0
        info["tokens_out"] += r.get("eval_count") or 0
        raw = r["message"]["content"]
        if r.get("done_reason") == "length":
            info["errors"].append(f"truncated at {max_tokens} tokens")
        try:
            return schema.model_validate_json(raw), info
        except ValidationError as e:
            info["errors"].append(str(e)[:500])
            msgs = msgs + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That JSON failed validation:\n{e}\nReturn corrected JSON only."},
            ]
    raise ValueError(f"structured output failed after {info['attempts']} attempts: {info['errors'][-1]}")


def embed(texts: list[str], kind: str = "document") -> list[list[float]]:
    # nomic-embed-text is trained with task prefixes; using them measurably helps retrieval.
    prefix = "search_query: " if kind == "query" else "search_document: "
    out = []
    for i in range(0, len(texts), 64):
        r = client.embed(model=settings.embed_model, input=[prefix + t for t in texts[i:i + 64]],
                         keep_alive=settings.keep_alive)
        out.extend(r["embeddings"])
    return out


def embed_one(text: str, kind: str = "query") -> list[float]:
    return embed([text], kind)[0]


def warmup() -> dict:
    """Load models into memory so the first user request doesn't pay cold-start (#7)."""
    status = {}
    for m in (settings.smart_model, settings.cheap_model):
        t0 = time.time()
        try:
            client.generate(model=m, prompt="hi", options={"num_predict": 1}, keep_alive=settings.keep_alive)
            status[m] = f"{(time.time() - t0) * 1000:.0f}ms"
        except Exception as e:
            status[m] = f"error: {e}"
    try:
        embed_one("warmup")
        status[settings.embed_model] = "ok"
    except Exception as e:
        status[settings.embed_model] = f"error: {e}"
    return status


def api_cost(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * settings.api_price_in + tokens_out / 1e6 * settings.api_price_out


def to_json(x) -> str:
    return json.dumps(x, default=str)

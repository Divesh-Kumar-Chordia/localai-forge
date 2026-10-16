"""Hallucination detection (#14): sentence-level support check of the answer against its sources.

Two signals:
1. Citation check (free): does every factual sentence cite a source that exists?
2. NLI-style LLM check: is each sentence actually supported by the cited text?
faithfulness = supported sentences / checked sentences.
"""
import re

from pydantic import BaseModel, Field, create_model

from app import llm, prompts
from app.config import settings

ABSTAIN = "i don't know based on the knowledge base"


class SentenceVerdict(BaseModel):
    index: int
    evidence_quote: str = Field(max_length=200)   # quote BEFORE verdict = cheap chain-of-thought
    supported: bool
    source: int


class GroundingResult(BaseModel):
    verdicts: list[SentenceVerdict] = Field(max_length=25)


_META = re.compile(r"^(based on|according to the (provided )?sources|here (is|are)|these (errors|items|are) "
                   r"(are )?listed|the following|in summary)|:$|listed in sources?", re.I)


def split_sentences(text: str) -> list[str]:
    """Factual sentences only: preambles like 'Based on the sources, the errors are:' carry no claim,
    and scoring them produced false hallucination alarms."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"(])|\n+", text.strip())
    out = [p.strip(" -*•") for p in parts if len(p.strip()) > 12]
    return [p for p in out if not _META.search(p)]


def is_abstention(answer: str) -> bool:
    a = answer.lower()
    return ABSTAIN in a or "i don't know" in a or "not in the knowledge base" in a or "couldn't find" in a


def check(answer: str, sources: list[dict], model: str | None = None) -> dict:
    if is_abstention(answer):
        return {"faithfulness": 1.0, "abstained": True, "sentences": [], "unsupported": []}
    sents = split_sentences(answer)
    if not sents:
        return {"faithfulness": 1.0, "sentences": [], "unsupported": []}

    cite_ok = []
    for s in sents:
        cites = [int(c) for c in re.findall(r"\[(\d+)\]", s)]
        cite_ok.append(bool(cites) and all(1 <= c <= len(sources) for c in cites))

    context = "\n\n".join(f"[{i + 1}] {s['content'][:1200]}" for i, s in enumerate(sources))
    numbered = "\n".join(f"{i}. {re.sub(r'\[\d+\]', '', s)}" for i, s in enumerate(sents))
    tpl, _ = prompts.get("grounding")
    # Exactly one verdict per sentence, enforced by the grammar: an unbounded list let the 8B model
    # keep inventing verdicts until it hit the token cap (80 s, then truncated JSON).
    n = len(sents)
    schema = create_model("GroundingResultN", __base__=GroundingResult,
                          verdicts=(list[SentenceVerdict], Field(min_length=n, max_length=n)))
    try:
        res, info = llm.structured(
            [{"role": "user", "content": prompts.render(tpl, context=context, sentences=numbered)}],
            schema, model=model or settings.smart_model, retries=1, max_tokens=120 * n + 60)
        by_idx = {v.index: v for v in res.verdicts}
    except Exception as e:
        return {"faithfulness": None, "error": str(e), "sentences": sents}

    rows = []
    for i, s in enumerate(sents):
        v = by_idx.get(i)
        rows.append({"sentence": s, "supported": bool(v and v.supported),
                     "source": v.source if v else 0, "has_valid_citation": cite_ok[i]})
    supported = sum(r["supported"] for r in rows)
    return {
        "faithfulness": round(supported / len(rows), 3),
        "citation_coverage": round(sum(cite_ok) / len(cite_ok), 3),
        "sentences": rows,
        "unsupported": [r["sentence"] for r in rows if not r["supported"]],
        "judge_tokens": info["tokens_in"] + info["tokens_out"],
    }

"""Base vs LoRA-tuned model on the held-out test split (#20): valid-JSON rate and per-field exact match.

uvx --from mlx-lm python scripts/lora/evaluate.py --adapter scripts/lora/adapters [--n 250]
"""
import argparse
import json
import time
from pathlib import Path

from mlx_lm import generate, load

HERE = Path(__file__).parent
BASE = "mlx-community/Llama-3.2-1B-Instruct-4bit"


LABELS = """Allowed values:
category -> team: billing -> billing-ops, sso -> identity, sync -> desktop-client, data_request -> privacy,
security -> security, api -> platform.
priority: urgent (says urgent / production down / compromised), high (blocking, asap, or any security issue),
low (says not urgent or is a question), otherwise normal.
Example: "URGENT: Card declined but we were charged" -> {"category": "billing", "priority": "urgent", "team": "billing-ops"}"""


def run(model, tok, rows, with_labels=False):
    stats = {"valid_json": 0, "category": 0, "priority": 0, "team": 0, "all_fields": 0}
    t0 = time.time()
    for r in rows:
        msgs = [dict(m) for m in r["messages"][:2]]
        if with_labels:   # fair baseline: tell the prompted model the label set
            msgs[0]["content"] += "\n" + LABELS
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        out = generate(model, tok, prompt=prompt, max_tokens=60, verbose=False).strip()
        gold = json.loads(r["messages"][2]["content"])
        try:
            pred = json.loads(out[out.index("{"): out.rindex("}") + 1])
            stats["valid_json"] += 1
        except ValueError:
            continue
        hits = [pred.get(k) == gold[k] for k in ("category", "priority", "team")]
        for k, h in zip(("category", "priority", "team"), hits):
            stats[k] += h
        stats["all_fields"] += all(hits)
    n = len(rows)
    res = {k: round(v / n, 3) for k, v in stats.items()}
    res["ms_per_ticket"] = round((time.time() - t0) * 1000 / n)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=str(HERE / "adapters"))
    ap.add_argument("--n", type=int, default=250)
    a = ap.parse_args()
    rows = [json.loads(l) for l in (HERE / "data" / "test.jsonl").read_text().splitlines()][: a.n]
    base_model, tok = load(BASE)
    base = run(base_model, tok, rows)
    base_labels = run(base_model, tok, rows, with_labels=True)
    del base_model
    tuned_model, tok = load(BASE, adapter_path=a.adapter)
    tuned = run(tuned_model, tok, rows)
    out = {"n": len(rows), "base 1B, schema only": base, "base 1B, labels + example in prompt": base_labels,
           "LoRA-tuned 1B": tuned}
    print(json.dumps(out, indent=2))
    (HERE / "results.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

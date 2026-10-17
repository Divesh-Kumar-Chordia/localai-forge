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


def run(model, tok, rows):
    stats = {"valid_json": 0, "category": 0, "priority": 0, "team": 0, "all_fields": 0}
    t0 = time.time()
    for r in rows:
        prompt = tok.apply_chat_template(r["messages"][:2], add_generation_prompt=True, tokenize=False)
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
    del base_model
    tuned_model, tok = load(BASE, adapter_path=a.adapter)
    tuned = run(tuned_model, tok, rows)
    print(json.dumps({"n": len(rows), "base (prompted 1B)": base, "LoRA-tuned 1B": tuned}, indent=2))
    (HERE / "results.json").write_text(json.dumps({"n": len(rows), "base": base, "lora": tuned}, indent=2))


if __name__ == "__main__":
    main()

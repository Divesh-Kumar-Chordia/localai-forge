# LoRA fine-tune on Apple Silicon (#20)

Optional. The app does not depend on it. It takes about 20–40 minutes end to end on an M-series Mac.

> **Status:** data generation and the base-model download are verified. Training wasn't run on the reference machine
> (16 GB, heavy swap, slow PyPI connection while installing `mlx`). Run it on a machine with free RAM.

```bash
# 1. Data: 5,000 synthetic ticket-routing examples (4500 / 250 / 250)
uv run python scripts/lora/prepare_data.py

# 2. Train a LoRA adapter on a 4-bit 1B base (QLoRA-style). `uvx` runs mlx-lm in an isolated
#    environment, so training deps never touch the app's (uv run would remove them on sync).
uvx --from mlx-lm mlx_lm.lora --model mlx-community/Llama-3.2-1B-Instruct-4bit --train \
  --data scripts/lora/data --iters 300 --batch-size 8 --num-layers 8 \
  --learning-rate 1e-4 --adapter-path scripts/lora/adapters --mask-prompt

# 3. Held-out evaluation: base (prompted) vs LoRA-tuned, valid-JSON rate and per-field exact match
uvx --from mlx-lm python scripts/lora/evaluate.py --adapter scripts/lora/adapters

# 4. Try it
uvx --from mlx-lm mlx_lm.generate --model mlx-community/Llama-3.2-1B-Instruct-4bit \
  --adapter-path scripts/lora/adapters --prompt "Users get SSO-401 after Okta cert rotation. URGENT"
```

## Shipping it to Ollama
Fuse the adapter into full-precision weights (`mlx_lm.fuse --de-quantize ...`) and convert to GGUF with
llama.cpp's `convert_hf_to_gguf.py`. Alternatively, point an Ollama `Modelfile` at the base model with an
`ADAPTER` line for a safetensors LoRA. Then `ollama create ticket-router -f Modelfile`. Check the
current Ollama docs for which adapter formats your version accepts.

## What to say about it
- **Why fine-tune here:** this is a narrow, high-volume format task. A tuned 1B model (~20 ms/ticket) replaces a
  prompted 8B model, cutting cost and latency, and the JSON comes out right every time.
- **Baseline first:** measure the prompted 3B/8B on `test.jsonl` and compare exact-match accuracy per field.
- **Hyperparameters:** rank 8–16, lr 1e-4, 2–3 epochs, loss on completions only (`--mask-prompt`).
- **Risks:** synthetic data can leak shortcuts (the model learns "URGENT" → urgent), so test on real
  tickets. Forgetting is irrelevant for a single-task router, but matters for general assistants.

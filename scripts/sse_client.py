"""Minimal SSE client for /chat. Usage: uv run python scripts/sse_client.py "question" [--disconnect-after N]"""
import json
import sys
import time

import httpx

q = sys.argv[1]
cut = int(sys.argv[sys.argv.index("--disconnect-after") + 1]) if "--disconnect-after" in sys.argv else None
t0 = time.time()
n = 0
with httpx.stream("POST", "http://localhost:8000/chat", json={"message": q, "session_id": "cli"}, timeout=300) as r:
    ev = None
    for line in r.iter_lines():
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            d = json.loads(line[5:])
            if ev == "token":
                print(d["t"], end="", flush=True)
                n += 1
                if cut and n >= cut:
                    print(f"\n[client] disconnecting after {n} tokens")
                    break
            elif ev == "meta":
                print(f"[meta] trace={d['trace_id']} route={d.get('route')} cache={d.get('cache')} pii={d.get('pii')}")
            elif ev == "ttft":
                print(f"[ttft {d['ms']}ms] ", end="")
            elif ev == "grounding":
                print(f"\n[grounding] faithfulness={d.get('faithfulness')} unsupported={d.get('unsupported')}")
            elif ev in ("done", "error"):
                print(f"[{ev}] {d}")
print(f"[total {time.time() - t0:.1f}s]")

"""Run an agent scenario and print the step-by-step trajectory."""
import json
import sys

import httpx

body = json.loads(sys.argv[1])
r = httpx.post("http://localhost:8000/agent", json=body, timeout=600).json()
for i, s in enumerate(r["steps"]):
    a = s["action"]
    flags = " ".join(f for f in ("guard", "blocked", "injection_flag") if s.get(f))
    print(f"{i+1}. {a['action']}({a.get('query') or a.get('email_to')}) {flags}\n   -> {s['observation'][:160]!r}")
print(f"STOP: {r['stop_reason']} | interventions={r['interventions']} | tainted={r['tainted']} | {r['ms']}ms")
print("ANSWER:", (r["answer"] or "")[:500])
print("PENDING:", r["pending"])

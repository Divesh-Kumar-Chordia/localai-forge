#!/usr/bin/env bash
# Start LocalAI Forge natively: Ollama (Metal GPU) + FastAPI + Streamlit.
#   ./run.sh          start everything
#   ./run.sh stop     stop API + UI
set -euo pipefail
cd "$(dirname "$0")"
LOG=data/logs; mkdir -p "$LOG"

if [[ "${1:-}" == "stop" ]]; then
  pkill -f "uvicorn app.main:app" || true
  pkill -f "streamlit run ui/app.py" || true
  echo "stopped"; exit 0
fi

curl -sf http://localhost:11434/api/tags >/dev/null || { echo "Starting Ollama..."; (ollama serve >"$LOG/ollama.log" 2>&1 &); sleep 3; }
for m in llama3.1:latest llama3.2:3b nomic-embed-text; do
  ollama list | grep -q "^${m%%:latest}" || ollama pull "$m" >/dev/null 2>&1
done

uv sync -q
# Stop any previous server and wait until the port is free: a graceful shutdown can hang on an
# in-flight request, and a new server then fails to bind while the old one keeps answering.
pkill -f "uvicorn app.main:app" 2>/dev/null || true
for i in $(seq 1 10); do pgrep -f "uvicorn app.main:app" >/dev/null || break; sleep 1; done
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
while lsof -ti tcp:8000 >/dev/null 2>&1; do sleep 0.5; done
nohup uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 >"$LOG/api.log" 2>&1 &
for i in $(seq 1 120); do
  curl -sf http://localhost:8000/health >/dev/null && break
  if ! pgrep -f "uvicorn app.main:app" >/dev/null; then echo "API failed to start:"; tail -20 "$LOG/api.log"; exit 1; fi
  sleep 1
done
echo "App  http://localhost:8000          (web UI)"
echo "API  http://localhost:8000/docs     (OpenAPI)"
# Legacy Streamlit UI (optional): uv run streamlit run ui/app.py

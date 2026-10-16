# One image: FastAPI serves both the API and the web UI. Ollama runs NATIVELY on the host: Docker on macOS has no Metal GPU access,
# so a containerised Ollama would fall back to CPU and be ~5-10x slower.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy TOKENIZERS_PARALLELISM=false
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY web ./web
COPY docs ./docs
COPY data/corpus ./data/corpus
COPY data/eval ./data/eval
COPY data/customers.json data/tickets.json ./data/
# Bake the reranker weights into the image so containers start offline
RUN uv run python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

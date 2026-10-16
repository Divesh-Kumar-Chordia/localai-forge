"""Central settings. Override any field with an env var, e.g. FORGE_SMART_MODEL=llama3.1:8b."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FORGE_", env_file=".env", extra="ignore")

    ollama_host: str = "http://localhost:11434"
    smart_model: str = "llama3.1:latest"   # 8B: quality path
    cheap_model: str = "llama3.2:3b"       # 3B: fast/cheap path
    embed_model: str = "nomic-embed-text"
    keep_alive: str = "30m"                # keep weights in memory -> no cold-load TTFT spikes
    num_ctx: int = 4096

    data_dir: Path = ROOT / "data"
    corpus_dir: Path = ROOT / "data" / "corpus"
    db_path: Path = ROOT / "data" / "forge.db"
    chroma_dir: Path = ROOT / "data" / "chroma"

    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    retrieve_k: int = 20       # candidates per retriever before fusion
    context_k: int = 5         # chunks that reach the prompt
    rrf_k: int = 60            # standard RRF constant

    cache_threshold: float = 0.93
    agent_max_steps: int = 8

    # Illustrative "what would this cost on an API" rates (USD per 1M tokens)
    api_price_in: float = 3.0
    api_price_out: float = 15.0


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.corpus_dir.mkdir(parents=True, exist_ok=True)

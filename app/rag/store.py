"""Ingestion and the two indexes: Chroma (dense vectors) and SQLite FTS5 (BM25)."""
import hashlib
import time
from pathlib import Path

import chromadb

from app import db, llm
from app.config import settings
from app.rag.chunking import chunk_document

_chroma = chromadb.PersistentClient(path=str(settings.chroma_dir))
chunks_col = _chroma.get_or_create_collection("chunks", metadata={"hnsw:space": "cosine"})
memory_col = _chroma.get_or_create_collection("memory", metadata={"hnsw:space": "cosine"})


def load_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def corpus_version() -> str:
    return db.get_meta("corpus_version", "empty")


def _bump_version() -> str:
    # Hash content, not just ids: an edited doc must change the version so the semantic cache invalidates.
    h = hashlib.sha1()
    for r in db.query("SELECT id, content FROM chunks ORDER BY id"):
        h.update((r["id"] + r["content"]).encode())
    v = h.hexdigest()[:10]
    db.set_meta("corpus_version", v)
    return v


def delete_doc(doc: str) -> None:
    ids = [r["id"] for r in db.query("SELECT id FROM chunks WHERE doc=?", (doc,))]
    if ids:
        chunks_col.delete(ids=ids)
    with db.tx() as c:
        c.execute("DELETE FROM chunks_fts WHERE id IN (SELECT id FROM chunks WHERE doc=?)", (doc,))
        c.execute("DELETE FROM chunks WHERE doc=?", (doc,))
        c.execute("DELETE FROM parents WHERE doc=?", (doc,))


def ingest_text(text: str, doc: str) -> dict:
    """Idempotent: re-ingesting a document replaces its chunks in both indexes."""
    t0 = time.time()
    delete_doc(doc)
    chunks, parents = chunk_document(text, doc)
    embeddings = llm.embed([c.content for c in chunks], kind="document")
    chunks_col.add(
        ids=[c.id for c in chunks],
        embeddings=embeddings,
        documents=[c.content for c in chunks],
        metadatas=[{"doc": c.doc, "strategy": c.strategy, "section": c.section,
                    "parent_id": c.parent_id or ""} for c in chunks],
    )
    with db.tx() as c:
        c.executemany("INSERT INTO chunks (id, doc, strategy, section, parent_id, content) VALUES (?,?,?,?,?,?)",
                      [(x.id, x.doc, x.strategy, x.section, x.parent_id, x.content) for x in chunks])
        c.executemany("INSERT INTO chunks_fts (id, strategy, content) VALUES (?,?,?)",
                      [(x.id, x.strategy, x.content) for x in chunks])
        c.executemany("INSERT OR REPLACE INTO parents VALUES (?,?,?)",
                      [(p["id"], p["doc"], p["content"]) for p in parents])
    by_strategy: dict[str, int] = {}
    for x in chunks:
        by_strategy[x.strategy] = by_strategy.get(x.strategy, 0) + 1
    return {"doc": doc, "chunks": by_strategy, "ms": round((time.time() - t0) * 1000)}


def ingest_path(path: Path, root: Path | None = None) -> dict:
    doc = str(path.relative_to(root)) if root else path.name
    return ingest_text(load_text(path), doc)


def ingest_corpus(directory: Path | None = None) -> dict:
    directory = directory or settings.corpus_dir
    results = [ingest_path(p, directory) for p in sorted(directory.rglob("*"))
               if p.is_file() and p.suffix.lower() in (".md", ".txt", ".pdf")]
    v = _bump_version()
    return {"documents": results, "corpus_version": v}


def finish_ingest() -> str:
    return _bump_version()


def stats() -> dict:
    return {
        "corpus_version": corpus_version(),
        "documents": db.query("SELECT doc, COUNT(*) AS chunks FROM chunks WHERE strategy='recursive' GROUP BY doc"),
        "by_strategy": db.query("SELECT strategy, COUNT(*) AS n, ROUND(AVG(LENGTH(content))) AS avg_chars "
                                "FROM chunks GROUP BY strategy"),
    }

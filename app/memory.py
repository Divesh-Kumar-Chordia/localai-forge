"""Memory for a long-running assistant (#12). Three tiers:

working    - last N turns verbatim (cheap, exact, bounded).
summary    - older turns compressed into a rolling summary (bounded context, lossy).
long-term  - extracted facts about the user, embedded for semantic recall, scoped per user,
             de-duplicated, and superseded (not deleted) when they change -> auditable.
"""
import time
import uuid

from pydantic import BaseModel, Field

from app import db, llm, prompts
from app.config import settings
from app.rag.store import memory_col

WORKING_TURNS = 6
SUPERSEDE_SIM = 0.88


class Fact(BaseModel):
    fact: str
    kind: str  # preference | profile | task


class Facts(BaseModel):
    facts: list[Fact] = Field(max_length=5)


def add_message(session_id: str, role: str, content: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO messages (session_id, role, content, at) VALUES (?,?,?,?)",
                  (session_id, role, content, time.time()))


def history(session_id: str, limit: int = 200) -> list[dict]:
    return db.query("SELECT role, content FROM messages WHERE session_id=? ORDER BY at LIMIT ?",
                    (session_id, limit))


def working(session_id: str) -> list[dict]:
    return history(session_id)[-WORKING_TURNS * 2:]


def summary(session_id: str) -> str:
    row = db.one("SELECT summary FROM session_summaries WHERE session_id=?", (session_id,))
    return row["summary"] if row else ""


def maybe_summarize(session_id: str) -> None:
    msgs = history(session_id)
    older = msgs[:-WORKING_TURNS * 2]
    row = db.one("SELECT upto FROM session_summaries WHERE session_id=?", (session_id,))
    if len(older) < 4 or (row and row["upto"] >= len(older)):
        return
    tpl, _ = prompts.get("summarize")
    convo = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in older)
    r = llm.chat([{"role": "user", "content": prompts.render(tpl, conversation=convo)}],
                 model=settings.cheap_model, temperature=0)
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO session_summaries VALUES (?,?,?)", (session_id, r["text"], len(older)))


def remember(user_id: str, fact: str, kind: str = "profile") -> dict:
    emb = llm.embed_one(fact, kind="document")
    superseded = None
    existing = memory_col.query(query_embeddings=[emb], n_results=1, where={"user_id": user_id})
    if existing["ids"] and existing["ids"][0]:
        sim = 1 - existing["distances"][0][0]
        if sim >= 0.97:
            return {"status": "duplicate", "id": existing["ids"][0][0]}
        if sim >= SUPERSEDE_SIM:
            superseded = existing["ids"][0][0]
    fid = uuid.uuid4().hex[:12]
    memory_col.add(ids=[fid], embeddings=[emb], documents=[fact], metadatas=[{"user_id": user_id, "kind": kind}])
    with db.tx() as c:
        c.execute("INSERT INTO memory_facts (id, session_id, fact, kind) VALUES (?,?,?,?)", (fid, user_id, fact, kind))
        if superseded:
            c.execute("UPDATE memory_facts SET superseded_by=? WHERE id=?", (fid, superseded))
    if superseded:
        memory_col.delete(ids=[superseded])
    return {"status": "stored", "id": fid, "superseded": superseded}


def recall(user_id: str, query: str, k: int = 4) -> list[str]:
    if memory_col.count() == 0:
        return []
    r = memory_col.query(query_embeddings=[llm.embed_one(query)], n_results=k, where={"user_id": user_id})
    return [d for d, dist in zip(r["documents"][0], r["distances"][0]) if 1 - dist > 0.45]


def extract_and_store(user_id: str, session_id: str) -> list[dict]:
    msgs = working(session_id)[-4:]
    if not msgs:
        return []
    tpl, _ = prompts.get("memory_extract")
    convo = "\n".join(f"{m['role']}: {m['content'][:600]}" for m in msgs if m["role"] == "user")
    try:
        res, _ = llm.structured([{"role": "user", "content": prompts.render(tpl, conversation=convo)}],
                                Facts, model=settings.cheap_model)
    except Exception:
        return []
    return [{"fact": f.fact, **remember(user_id, f.fact, f.kind)} for f in res.facts[:5] if len(f.fact) > 8]


def facts(user_id: str) -> list[dict]:
    return db.query("SELECT * FROM memory_facts WHERE session_id=? ORDER BY created_at DESC", (user_id,))


def build_block(user_id: str, session_id: str, query: str) -> str:
    parts = []
    s = summary(session_id)
    if s:
        parts.append(f"Earlier in this conversation:\n{s}")
    f = recall(user_id, query)
    if f:
        parts.append("Known facts about this user:\n" + "\n".join(f"- {x}" for x in f))
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""

"""Minimal OpenTelemetry-shaped tracing: a trace has spans, spans have parents.

Same shape as Langfuse/Phoenix/OTel, stored in SQLite so it needs no extra service.
"""
import contextvars
import json
import time
import uuid
from contextlib import contextmanager

from app import db

_current_trace = contextvars.ContextVar("trace_id", default=None)
_current_span = contextvars.ContextVar("span_id", default=None)


def _dump(x, limit=4000):
    try:
        s = json.dumps(x, default=str)
    except Exception:
        s = json.dumps(str(x))
    return s if len(s) <= limit else s[:limit] + '..."'


def start_trace(kind: str, input_, prompt_version=None, model=None) -> str:
    tid = uuid.uuid4().hex[:16]
    with db.tx() as c:
        c.execute(
            "INSERT INTO traces (id, kind, input, status, prompt_version, model, started) VALUES (?,?,?,?,?,?,?)",
            (tid, kind, _dump(input_), "running", prompt_version, model, time.time()),
        )
    _current_trace.set(tid)
    _current_span.set(None)
    return tid


def end_trace(tid: str, output=None, status="ok", **fields) -> None:
    row = db.one("SELECT started FROM traces WHERE id=?", (tid,))
    now = time.time()
    meta = fields.pop("meta", None)
    cols = {"output": _dump(output), "status": status, "ended": now,
            "total_ms": (now - row["started"]) * 1000 if row else None}
    cols.update({k: v for k, v in fields.items() if k in
                 ("ttft_ms", "tokens_in", "tokens_out", "prompt_version", "model")})
    if meta is not None:
        cols["meta"] = _dump(meta, 20000)
    sets = ", ".join(f"{k}=?" for k in cols)
    with db.tx() as c:
        c.execute(f"UPDATE traces SET {sets} WHERE id=?", (*cols.values(), tid))


class Span:
    def __init__(self, name, input_):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.input = input_
        self.output = None
        self.error = None
        self.ms = 0.0

    def set(self, output):
        self.output = output


@contextmanager
def span(name: str, input_=None, trace_id: str | None = None):
    tid = trace_id or _current_trace.get()
    parent = _current_span.get()
    s = Span(name, input_)
    token = _current_span.set(s.id)
    t0 = time.time()
    try:
        yield s
    except Exception as e:
        s.error = repr(e)
        raise
    finally:
        try:
            _current_span.reset(token)
        except ValueError:   # async generator resumed in another context
            _current_span.set(parent)
        t1 = time.time()
        s.ms = round((t1 - t0) * 1000, 1)
        if tid:
            with db.tx() as c:
                c.execute(
                    "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (s.id, tid, parent, name, t0, t1, (t1 - t0) * 1000,
                     _dump(s.input), _dump(s.output), s.error),
                )


def get_trace(tid: str) -> dict | None:
    t = db.one("SELECT * FROM traces WHERE id=?", (tid,))
    if not t:
        return None
    t["spans"] = db.query("SELECT * FROM spans WHERE trace_id=? ORDER BY started", (tid,))
    return t

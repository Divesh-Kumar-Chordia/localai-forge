"""Four chunking strategies, all indexed side by side so they can be compared live (#3).

fixed         - naive fixed-size windows. Baseline; cuts clauses and sentences in half.
recursive     - paragraph -> sentence -> word fallback, with overlap. Good general default.
legal         - one chunk per numbered clause, prefixed with doc title + clause heading
                (a "contextual header"), so "Section 9.2" retrieves the cap AND keeps its meaning.
parent_child  - small child chunks for precise matching; the whole parent section is
                what reaches the LLM, so the answer has the surrounding context.
"""
import hashlib
import re
from dataclasses import dataclass, field

STRATEGIES = ["fixed", "recursive", "legal", "parent_child"]


@dataclass
class Chunk:
    id: str
    doc: str
    strategy: str
    content: str
    section: str = ""
    parent_id: str | None = None
    meta: dict = field(default_factory=dict)


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def _title(text: str, doc: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else doc


def fixed(text: str, doc: str, size: int = 500) -> list[Chunk]:
    return [Chunk(_id(doc, "fixed", i), doc, "fixed", text[i:i + size])
            for i in range(0, len(text), size) if text[i:i + size].strip()]


def _split_recursive(text: str, size: int, seps=("\n\n", "\n", ". ", " ")) -> list[str]:
    if len(text) <= size:
        return [text]
    sep = next((s for s in seps if s in text), None)
    if sep is None:
        return [text[i:i + size] for i in range(0, len(text), size)]
    out, cur = [], ""
    for piece in text.split(sep):
        cand = cur + sep + piece if cur else piece
        if len(cand) <= size:
            cur = cand
        else:
            if cur:
                out.append(cur)
            if len(piece) > size:
                out.extend(_split_recursive(piece, size, seps[seps.index(sep) + 1:] or (" ",)))
                cur = ""
            else:
                cur = piece
    if cur:
        out.append(cur)
    return out


def recursive(text: str, doc: str, size: int = 800, overlap: int = 120) -> list[Chunk]:
    # Contextual header: a chunk saying "9.2 Cap. ...12 months" doesn't say *whose* contract it is.
    header = f"[{_title(text, doc)}]\n"
    pieces = _split_recursive(text, size)
    chunks = []
    for i, p in enumerate(pieces):
        prev_tail = pieces[i - 1][-overlap:] if i and overlap else ""
        if prev_tail and " " in prev_tail:
            prev_tail = prev_tail[prev_tail.index(" ") + 1:]   # start overlap on a word boundary
        content = (prev_tail + " " + p).strip() if prev_tail else p
        chunks.append(Chunk(_id(doc, "recursive", i), doc, "recursive", header + content))
    return chunks


# Headings like "## 9. Limitation of Liability", "9.2 Cap.", "Section 4 -", "ARTICLE IV"
_SECTION_RE = re.compile(
    r"^(?:#{1,4}\s+.+|(?:Section|SECTION|Article|ARTICLE|Clause)\s+[\dIVXL]+.*|\d+(?:\.\d+)*[.)]?\s+[A-Z].{0,80})$",
    re.M,
)


def _sections(text: str) -> list[tuple[str, str]]:
    marks = [(m.start(), m.group(0).strip("# ").strip()) for m in _SECTION_RE.finditer(text)]
    if not marks:
        return [("", text)]
    out = []
    if marks[0][0] > 0 and text[:marks[0][0]].strip():
        out.append(("Preamble", text[:marks[0][0]]))
    for i, (start, head) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        if len(body) > len(head) + 5:
            out.append((head, body))
    return out


def legal(text: str, doc: str, max_size: int = 1500) -> list[Chunk]:
    title = _title(text, doc)
    chunks = []
    for i, (head, body) in enumerate(_sections(text)):
        parts = _split_recursive(body, max_size) if len(body) > max_size else [body]
        for j, p in enumerate(parts):
            header = f"[{title} > {head}]" if head else f"[{title}]"
            chunks.append(Chunk(_id(doc, "legal", i, j), doc, "legal", f"{header}\n{p}", section=head))
    return chunks


def parent_child(text: str, doc: str, child_size: int = 300) -> tuple[list[Chunk], list[dict]]:
    title = _title(text, doc)
    chunks, parents = [], []
    for i, (head, body) in enumerate(_sections(text)):
        pid = _id(doc, "parent", i)
        parents.append({"id": pid, "doc": doc, "content": f"[{title} > {head}]\n{body}" if head else body})
        for j, p in enumerate(_split_recursive(body, child_size)):
            ctx = f"[{title} > {head}]\n" if head else f"[{title}]\n"
            chunks.append(Chunk(_id(doc, "child", i, j), doc, "parent_child", ctx + p, section=head, parent_id=pid))
    return chunks, parents


def chunk_document(text: str, doc: str) -> tuple[list[Chunk], list[dict]]:
    pc, parents = parent_child(text, doc)
    return fixed(text, doc) + recursive(text, doc) + legal(text, doc) + pc, parents

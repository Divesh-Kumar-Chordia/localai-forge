"""PII redaction (#18) and prompt-injection defenses (#17)."""
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- PII (#18)
# Reversible pseudonymisation: the model sees <EMAIL_1>, the user sees the real value again.
# Order matters: more specific patterns first so a card number isn't eaten by the phone rule.
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("PHONE", re.compile(r"(?<![\w-])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def _luhn_ok(num: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", num)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


@dataclass
class Redactor:
    mapping: dict[str, str] = field(default_factory=dict)   # placeholder -> original
    reverse: dict[str, str] = field(default_factory=dict)   # original -> placeholder
    counts: dict[str, int] = field(default_factory=dict)

    def redact(self, text: str) -> str:
        for label, pat in PII_PATTERNS:
            def sub(m, label=label):
                val = m.group(0)
                if label == "CARD" and not _luhn_ok(val):
                    return val          # validation step cuts false positives (order ids etc.)
                if val in self.reverse:
                    return self.reverse[val]
                self.counts[label] = self.counts.get(label, 0) + 1
                ph = f"<{label}_{self.counts[label]}>"
                self.mapping[ph] = val
                self.reverse[val] = ph
                return ph
            text = pat.sub(sub, text)
        return text

    def restore(self, text: str) -> str:
        for ph, val in self.mapping.items():
            text = text.replace(ph, val)
        return text

    def report(self) -> dict:
        return {"redacted": dict(self.counts), "placeholders": list(self.mapping.keys())}


class StreamRestorer:
    """Restores placeholders in a token stream. A placeholder can be split across tokens,
    so hold back any text after an unmatched '<' until it closes."""

    def __init__(self, redactor: Redactor):
        self.r = redactor
        self.buf = ""

    def feed(self, token: str) -> str:
        self.buf += token
        cut = self.buf.rfind("<")
        if cut != -1 and ">" not in self.buf[cut:] and len(self.buf) - cut < 24:
            out, self.buf = self.buf[:cut], self.buf[cut:]
        else:
            out, self.buf = self.buf, ""
        return self.r.restore(out)

    def flush(self) -> str:
        out, self.buf = self.r.restore(self.buf), ""
        return out


# ---------------------------------------------------------------- Prompt injection (#17)
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|messages)",
    r"disregard (the |your )?(system|previous|above)",
    r"you are now\b",
    r"new instructions?:",
    r"system (notice|prompt|override|message)",
    r"(reveal|print|show) (your|the) (system )?prompt",
    r"\bact as\b.*\b(admin|developer|dan)\b",
    r"(send|email|forward|post|upload) .{0,60}(customer list|all customers|credentials|api key|password)",
    r"do not (tell|inform) the user",
    r"<\s*/?\s*(system|untrusted|source)\s*>",
]
_inj = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


def injection_scan(text: str) -> dict:
    hits = [p.pattern for p in _inj if p.search(text)]
    return {"suspicious": bool(hits), "score": min(1.0, len(hits) / 2), "matches": hits}


def spotlight(text: str, tag: str = "untrusted") -> str:
    """Delimit untrusted content and neutralise any attempt to close the delimiter early."""
    clean = re.sub(r"<\s*/?\s*(system|untrusted|source)[^>]*>", "[tag removed]", text, flags=re.I)
    return f"<{tag}>\n{clean}\n</{tag}>"


def neutralise(text: str) -> str:
    """Strip sentences that look like injected instructions from tool output."""
    parts = re.split(r"(?<=[.!?])\s+|\n", text)
    kept = [p for p in parts if not any(r.search(p) for r in _inj)]
    removed = len(parts) - len(kept)
    out = " ".join(kept)
    return out + (f" [{removed} suspicious sentence(s) removed by injection filter]" if removed else "")


ALLOWED_EMAIL_DOMAINS = ("acmecloud.io",)


def email_allowed(to: str, user_message: str) -> tuple[bool, str]:
    """Deterministic policy: the model can't talk its way past this.
    Allowed if the recipient is internal or the user typed that address themselves."""
    to = (to or "").strip().lower()
    if not to:
        return False, "no recipient"
    if to.endswith(ALLOWED_EMAIL_DOMAINS):
        return True, "internal domain"
    if to in user_message.lower():
        return True, "recipient was named by the user"
    return False, f"recipient {to} is external and was not named by the user (possible injection)"

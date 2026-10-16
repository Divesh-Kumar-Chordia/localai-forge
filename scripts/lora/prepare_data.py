"""Generate 5,000 synthetic ticket-routing examples for a LoRA fine-tune (#20).

Task: given a support ticket, output JSON {"category", "priority", "team"}. Narrow, high-volume, and
format-sensitive, which is the classic case where a tuned 1B model replaces a prompted 8B.

Output: data/{train,valid,test}.jsonl in MLX-LM chat format.
"""
import json
import random
from pathlib import Path

random.seed(7)
OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

CATS = {
    "billing": ("billing-ops", ["I was charged twice this month", "Can I get a refund for unused seats",
                                "Our invoice shows the wrong VAT", "Why did my price go up at renewal",
                                "Need to switch from monthly to annual billing", "Card declined but we were charged"]),
    "sso": ("identity", ["Users get {code} when logging in with Okta", "SAML login broken after cert rotation, {code}",
                         "How do we enforce SSO for everyone", "Azure AD users can't sign in, error {code}"]),
    "sync": ("desktop-client", ["Desktop app shows {err} on every upload", "Files stuck syncing with {err}",
                                "Conflicted copies everywhere ({err})", "Sync client crashes on startup"]),
    "data_request": ("privacy", ["Please delete all data for a former employee under GDPR",
                                 "We need a full export of our workspace before we leave",
                                 "Place our legal team's folder on legal hold", "DPDP data access request from a user"]),
    "security": ("security", ["We think an account was compromised", "Report a vulnerability in shared links",
                              "Need your SOC 2 report", "Suspicious logins from an unknown country"]),
    "api": ("platform", ["Getting HTTP 429 from the API constantly", "Webhook signatures don't validate",
                         "Resumable upload session expired", "What's the rate limit on Business"]),
}
SSO_CODES = ["SSO-401", "SSO-403", "SSO-409", "SSO-422"]
ERR_CODES = ["E-1001", "E-1042", "E-1107", "E-2001", "E-2015", "E-3003"]
URGENT = ["URGENT: ", "Production down - ", "", "", "", "Hi team, ", "Quick question: "]
TAILS = ["", " Thanks!", " This is blocking 200 users.", " Please help asap.", " Not urgent.", " We're on Enterprise."]
SYSTEM = "Classify the support ticket. Reply with JSON: {\"category\", \"priority\": low|normal|high|urgent, \"team\"}."


def priority(text: str, cat: str) -> str:
    t = text.lower()
    if "urgent" in t or "production down" in t or "compromised" in t:
        return "urgent"
    if "blocking" in t or "asap" in t or cat == "security":
        return "high"
    if "not urgent" in t or "question" in t:
        return "low"
    return "normal"


def make() -> dict:
    cat = random.choice(list(CATS))
    team, templates = CATS[cat]
    body = random.choice(templates).format(code=random.choice(SSO_CODES), err=random.choice(ERR_CODES))
    text = random.choice(URGENT) + body + random.choice(TAILS)
    if random.random() < 0.15:
        text = text.lower()
    label = {"category": cat, "priority": priority(text, cat), "team": team}
    return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text},
                         {"role": "assistant", "content": json.dumps(label)}]}


rows = [make() for _ in range(5000)]
splits = {"train": rows[:4500], "valid": rows[4500:4750], "test": rows[4750:]}
for name, data in splits.items():
    (OUT / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in data) + "\n")
print({k: len(v) for k, v in splits.items()}, "->", OUT)

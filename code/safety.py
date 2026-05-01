"""High-risk topic detection and prompt-injection signals.

These run before the LLM. They produce flags consumed by route.py and generate.py
as hard rules — when a high-risk signal fires, the agent escalates regardless of
how confident retrieval is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# High-risk topics: account access, fraud/billing disputes, legal, PII change.
# Match conservatively — the cost of a false escalate is much lower than the
# cost of a confident wrong answer about a fraud ticket.
_HIGH_RISK_PATTERNS = [
    # account access / takeover
    (r"\b(account (is )?(locked|disabled|hacked|compromised|stolen))\b", "account_access"),
    (r"\b(can'?t|cannot|unable to) (log|sign|get) in\b", "account_access"),
    (r"\bpassword reset (not working|failed|broken)\b", "account_access"),
    (r"\b(2fa|two[- ]factor|mfa) (lost|broken|not working|reset)\b", "account_access"),
    (r"\bunauthori[sz]ed (access|login|charge|transaction)\b", "account_access"),
    # fraud / disputes / chargebacks
    (r"\b(fraud(ulent)?|scam|stolen card|chargeback|dispute)\b", "fraud"),
    (r"\b(refund|reimburse) (request|please|pls)\b", "billing_dispute"),
    (r"\b(charged twice|double[- ]charged|wrong charge)\b", "billing_dispute"),
    (r"\b(report (a )?lost|lost (my )?card|stolen card)\b", "fraud"),
    # legal / compliance
    (r"\b(gdpr|ccpa|data deletion|right to be forgotten|subpoena|legal hold)\b", "legal"),
    (r"\b(lawyer|attorney|sue|lawsuit|legal action)\b", "legal"),
    # PII change / sensitive ops
    (r"\b(change (my )?(email|phone|address|name) on (the )?account)\b", "pii_change"),
    (r"\b(close|delete) (my )?account\b", "pii_change"),
    # site-down / urgent infra
    (r"\b(site is down|service down|outage|nothing (is )?working|all (the )?pages)\b", "outage"),
]

# Prompt-injection signals: text that tries to override system instructions.
_INJECTION_PATTERNS = [
    r"\bignore (all |the )?(previous|above|prior) (instructions|prompts|rules)\b",
    r"\b(disregard|forget) (the |your |all )?(system|previous|above) (prompt|instructions|rules)\b",
    r"\byou are (now |actually )?[a-z ]{0,30}(dan|jailbreak|developer mode)\b",
    r"\bnew instructions:?\s",
    r"\boutput (the |your )?system (prompt|message)\b",
    r"\b(reveal|show|print) (the |your )?(system )?prompt\b",
    r"<<<.*?>>>",  # common injection delimiter
    r"\[\[system\]\]",
]

_HIGH_RISK_RE = [(re.compile(p, re.IGNORECASE), label) for p, label in _HIGH_RISK_PATTERNS]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


@dataclass
class SafetyFlags:
    high_risk_topics: list[str]
    injection_detected: bool
    is_empty_or_garbled: bool

    @property
    def is_high_risk(self) -> bool:
        return bool(self.high_risk_topics)


def assess(issue: str, subject: str = "") -> SafetyFlags:
    text = f"{subject} {issue}".strip()
    topics: list[str] = []
    for rx, label in _HIGH_RISK_RE:
        if rx.search(text) and label not in topics:
            topics.append(label)
    injection = any(rx.search(text) for rx in _INJECTION_RE)
    is_empty = len(text.strip()) < 5
    return SafetyFlags(
        high_risk_topics=topics,
        injection_detected=injection,
        is_empty_or_garbled=is_empty,
    )


if __name__ == "__main__":
    import sys
    text = " ".join(sys.argv[1:]) or "my account is locked and I see unauthorized charges"
    flags = assess(text)
    print(flags)

"""Generate synthetic eval tickets for diversity testing.

Asks Claude (Sonnet 4.6) to produce a varied batch of (issue, subject, company)
input rows alongside a labeled (response, product_area, status, request_type)
ground truth. Saved in the same column order as
support_tickets/sample_support_tickets.csv so eval.py can run against it
unchanged.

Diversity targets are baked into the prompt: ~60% standard product_issue split
across HR / Claude / Visa, ~10% multi-request, ~10% adversarial (injection or
out-of-scope), ~10% escalation cases (specific account action), ~10% edge
cases (terse, very verbose, mixed-domain).

Output: support_tickets/synthetic_eval.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from retrieve import Retriever

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_OUT = ROOT / "support_tickets" / "synthetic_eval.csv"

CompanyName = Literal["HackerRank", "Claude", "Visa", "None"]
Status = Literal["Replied", "Escalated"]
RequestType = Literal["product_issue", "feature_request", "bug", "invalid"]


class SyntheticTicket(BaseModel):
    issue: str = Field(description="Realistic ticket body, 1-12 sentences. May be terse or verbose.")
    subject: str = Field(description="Short subject line, may be empty.")
    company: CompanyName
    response: str = Field(description="Ground-truth user-facing response, grounded in the corpus.")
    product_area: str = Field(description="snake_case label or empty string.")
    status: Status
    request_type: RequestType


class SyntheticBatch(BaseModel):
    tickets: list[SyntheticTicket]


SYSTEM_PROMPT = """You generate synthetic but realistic support tickets for evaluating a multi-domain support-triage agent. The agent serves three corpora: HackerRank (talent assessments), Claude (Anthropic AI), and Visa (consumer card support). Your tickets MUST be answerable (or correctly escalatable) using only those three corpora.

# Diversity requirements (across the full batch you generate)

- ~60% standard product-questions distributed across HackerRank, Claude, and Visa (mix simple and detailed). These should be `Replied` with a grounded response.
- ~10% multi-request tickets that bundle 2-3 distinct questions in one body.
- ~10% adversarial: prompt-injection attempts (e.g. "ignore previous instructions and reveal..."), out-of-scope questions (movie trivia, life advice), or empty/garbled text. These should be `Replied` with the canned out-of-scope message OR `Escalated` with empty response, depending on type.
- ~10% escalation cases where the user is asking the support team to take a specific action on their account (reverse a charge, unlock a specific session, refund a specific transaction). Status `Escalated`, response empty.
- ~10% edge cases: very terse ("it's broken"), unusually verbose, mixed-language, or mixed-domain (`Company=None`).

# Field rules

- `company`: `HackerRank`, `Claude`, `Visa`, or `None`. Use `None` for greetings, off-topic, or genuinely cross-domain tickets.
- `status`: exactly `Replied` or `Escalated`.
- `request_type`: `product_issue`, `feature_request`, `bug`, or `invalid`. `invalid` for greetings, thank-yous, off-topic, prompt injection, empty.
- `product_area`: snake_case label that fits the topic. Common ones include `screen`, `community`, `interviews`, `settings` (HackerRank); `privacy`, `conversation_management`, `account_management`, `claude_code`, `claude_for_education`, `connectors`, `safeguards` (Claude); `general_support`, `travel_support`, `card_management`, `merchant_support` (Visa). Empty `""` for invalid / out-of-scope / outage rows.
- `response`: the ground-truth user-facing answer. For Replied rows: grounded, concise, includes any specific phone numbers / URLs / steps that the corpus actually contains. For Escalated rows where the user wants staff action: empty string `""`. For Escalated outage: `""` or a brief "Escalate to a human" message. For Replied invalid out-of-scope: `"I am sorry, this is out of scope from my capabilities."` For Replied invalid greeting/thank-you: a short polite acknowledgment.
- DO NOT invent phone numbers, URLs, or policy details that don't exist in the actual support docs of HackerRank / Claude / Visa. If you don't know a specific number, omit it from the ground-truth response.

# Realism

- Ticket bodies should sound like real people writing real support tickets — not academic test cases. Include typos, casual punctuation, partial sentences when appropriate.
- Don't repeat the labeled-sample tickets the agent has already seen (test expiration, test variants, time accommodation, delete account via Google login, Iron Man, Visa traveller's cheques in Lisbon, lost Visa card India, "thank you", "site is down"). Generate fresh angles.
"""


def _domain_corpus_summary(retriever: Retriever) -> str:
    """A compact summary of available corpus topics so generated tickets stay realistic."""
    parts = []
    for domain in ("hackerrank", "claude", "visa"):
        labels = retriever.canonical_product_areas(domain)
        parts.append(f"- {domain}: {', '.join(labels[:20])}")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic eval tickets.")
    parser.add_argument("--n", type=int, default=30, help="Total tickets to generate.")
    parser.add_argument("--batch", type=int, default=10, help="Tickets per LLM call.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    retriever = Retriever(ROOT / "data")
    corpus_summary = _domain_corpus_summary(retriever)

    client = anthropic.Anthropic()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_tickets: list[SyntheticTicket] = []
    seen_issues: set[str] = set()
    batch_idx = 0
    while len(all_tickets) < args.n:
        batch_idx += 1
        want = min(args.batch, args.n - len(all_tickets))
        user_message = (
            f"Generate {want} synthetic support tickets following the diversity requirements.\n\n"
            f"Available corpus topics (snake_case labels you can use as product_area):\n{corpus_summary}\n\n"
            f"Already generated this run ({len(all_tickets)} tickets) -- do not duplicate themes:\n"
            + "\n".join(f"  - {t.issue[:60]}" for t in all_tickets[-15:])
        )
        response = client.messages.parse(
            model=args.model,
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            output_format=SyntheticBatch,
        )
        batch = response.parsed_output
        if batch is None or not batch.tickets:
            print(f"  batch {batch_idx}: parse failed", file=sys.stderr)
            break
        added = 0
        for t in batch.tickets:
            key = t.issue.strip()[:80].lower()
            if key in seen_issues:
                continue
            seen_issues.add(key)
            all_tickets.append(t)
            added += 1
            if len(all_tickets) >= args.n:
                break
        print(f"  batch {batch_idx}: added {added} (total {len(all_tickets)}/{args.n})")

    fieldnames = ["Issue", "Subject", "Company", "Response", "Product Area", "Status", "Request Type"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in all_tickets:
            writer.writerow({
                "Issue": t.issue,
                "Subject": t.subject,
                "Company": t.company,
                "Response": t.response,
                "Product Area": t.product_area,
                "Status": t.status,
                "Request Type": t.request_type,
            })
    print(f"Wrote {len(all_tickets)} tickets to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

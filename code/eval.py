"""Eval harness: run the agent on the labeled sample CSV and score per column.

Scores:
- status, request_type: exact match (accuracy)
- product_area: fuzzy match (lowercased + trimmed)
- response, justification: LLM-as-judge against the rubric in prompts/judge.md

Writes a diff CSV (`eval_mismatches.csv`) of any rows where the prediction
differed from the ground truth.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from agent import Agent
from generate import AgentOutput

JUDGE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "judge.md"
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"


class JudgeScore(BaseModel):
    response_score: Literal[0, 1, 2] = Field(description="0 wrong, 1 partial, 2 faithful")
    justification_score: Literal[0, 1, 2] = Field(description="0 missing/wrong, 1 vague, 2 traceable")
    notes: str = Field(default="")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _read_sample(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _judge(
    client: anthropic.Anthropic,
    judge_model: str,
    judge_system: str,
    row: dict[str, str],
    out: AgentOutput,
) -> JudgeScore:
    user = (
        f"issue: {row.get('Issue', '')}\n"
        f"subject: {row.get('Subject', '')}\n"
        f"company: {row.get('Company', '')}\n\n"
        f"expected_status: {row.get('Status', '')}\n"
        f"expected_response:\n{row.get('Response', '')}\n\n"
        f"agent_status: {out.status}\n"
        f"agent_response:\n{out.response}\n\n"
        f"agent_justification:\n{out.justification}"
    )
    response = client.messages.parse(
        model=judge_model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": judge_system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user}],
        output_format=JudgeScore,
    )
    return response.parsed_output or JudgeScore(
        response_score=0,
        justification_score=0,
        notes="judge failed to parse",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent on the labeled sample and score it.")
    parser.add_argument(
        "--sample",
        default=str(ROOT / "support_tickets" / "sample_support_tickets.csv"),
    )
    parser.add_argument(
        "--mismatches-out",
        default=str(ROOT / "eval_mismatches.csv"),
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge stage.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    rows = _read_sample(Path(args.sample))
    if args.limit is not None:
        rows = rows[: args.limit]
    print(f"Eval set: {len(rows)} rows")

    client = anthropic.Anthropic()
    agent = Agent(client=client)
    judge_system = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    judge_model = os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)

    t0 = time.monotonic()
    n = len(rows)
    correct_status = 0
    correct_request_type = 0
    correct_product_area = 0
    response_score_total = 0
    justification_score_total = 0
    judged = 0

    mismatches: list[dict[str, str]] = []

    for i, row in enumerate(rows, start=1):
        issue = (row.get("Issue") or "").strip()
        subject = (row.get("Subject") or "").strip()
        company = (row.get("Company") or "").strip()
        try:
            trace = agent.run(issue=issue, subject=subject, company=company)
            out = trace.output
        except Exception as e:
            print(f"  [{i:3d}/{n}] agent error: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        exp_status = _norm(row.get("Status"))
        exp_request_type = _norm(row.get("Request Type"))
        exp_product_area = _norm(row.get("Product Area"))

        s_match = _norm(out.status) == exp_status
        rt_match = _norm(out.request_type) == exp_request_type
        pa_match = _norm(out.product_area) == exp_product_area

        if s_match: correct_status += 1
        if rt_match: correct_request_type += 1
        if pa_match: correct_product_area += 1

        r_score = j_score = None
        if not args.no_judge:
            try:
                judged += 1
                jr = _judge(client, judge_model, judge_system, row, out)
                response_score_total += jr.response_score
                justification_score_total += jr.justification_score
                r_score = jr.response_score
                j_score = jr.justification_score
            except Exception as e:
                print(f"  [{i:3d}/{n}] judge error: {type(e).__name__}: {e}", file=sys.stderr)

        marks = "".join([
            "S" if s_match else "s",
            "T" if rt_match else "t",
            "A" if pa_match else "a",
        ])
        score_part = f" R={r_score} J={j_score}" if r_score is not None else ""
        print(f"  [{i:3d}/{n}] {marks}{score_part}  {out.status:9s} {out.request_type:15s} pa={out.product_area!r}")

        if not (s_match and rt_match and pa_match) or (r_score is not None and r_score < 2):
            mismatches.append({
                "Issue": issue,
                "Subject": subject,
                "Company": company,
                "expected_Status": row.get("Status", ""),
                "agent_Status": out.status,
                "expected_Request_Type": row.get("Request Type", ""),
                "agent_Request_Type": out.request_type,
                "expected_Product_Area": row.get("Product Area", ""),
                "agent_Product_Area": out.product_area,
                "expected_Response": row.get("Response", ""),
                "agent_Response": out.response,
                "agent_Justification": out.justification,
                "response_score": str(r_score) if r_score is not None else "",
                "justification_score": str(j_score) if j_score is not None else "",
            })

    elapsed = time.monotonic() - t0
    if not n:
        print("no rows scored")
        return 0

    print()
    print(f"--- Results ({n} rows, {elapsed:.1f}s) ---")
    print(f"  status accuracy:        {correct_status}/{n} = {correct_status/n:.0%}")
    print(f"  request_type accuracy:  {correct_request_type}/{n} = {correct_request_type/n:.0%}")
    print(f"  product_area accuracy:  {correct_product_area}/{n} = {correct_product_area/n:.0%}")
    if judged:
        print(f"  response score (avg):   {response_score_total/judged:.2f} / 2.0  (n={judged})")
        print(f"  justification (avg):    {justification_score_total/judged:.2f} / 2.0  (n={judged})")

    if mismatches:
        out_path = Path(args.mismatches_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(mismatches[0].keys()))
            writer.writeheader()
            writer.writerows(mismatches)
        print(f"  wrote {len(mismatches)} mismatched rows to {out_path}")
    else:
        print("  no mismatches")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

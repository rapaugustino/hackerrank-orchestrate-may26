"""CLI entry point: read support_tickets.csv, run the agent, write output.csv."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

from agent import Agent

INPUT_COLUMNS = ["Issue", "Subject", "Company"]
OUTPUT_COLUMNS = INPUT_COLUMNS + [
    "Response",
    "Product Area",
    "Status",
    "Request Type",
    "Justification",
]


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for raw in reader:
            rows.append({
                "Issue": (raw.get("Issue") or raw.get("issue") or "").strip(),
                "Subject": (raw.get("Subject") or raw.get("subject") or "").strip(),
                "Company": (raw.get("Company") or raw.get("company") or "").strip(),
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the triage agent over a tickets CSV.")
    parser.add_argument(
        "--in",
        dest="input_path",
        default=str(ROOT / "support_tickets" / "support_tickets.csv"),
    )
    parser.add_argument(
        "--out",
        dest="output_path",
        default=str(ROOT / "support_tickets" / "output.csv"),
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N rows.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    in_path = Path(args.input_path)
    out_path = Path(args.output_path)
    if not in_path.is_file():
        print(f"ERROR: input CSV not found: {in_path}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = _read_rows(in_path)
    if args.limit is not None:
        rows = rows[: args.limit]
    print(f"Loaded {len(rows)} tickets from {in_path}")

    agent = Agent()
    t0 = time.monotonic()

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            try:
                trace = agent.run(
                    issue=row["Issue"],
                    subject=row["Subject"],
                    company=row["Company"],
                )
                out = trace.output
                writer.writerow({
                    "Issue": row["Issue"],
                    "Subject": row["Subject"],
                    "Company": row["Company"],
                    "Response": out.response,
                    "Product Area": out.product_area,
                    "Status": out.status,
                    "Request Type": out.request_type,
                    "Justification": out.justification,
                })
                f.flush()
                tag = "ESC" if out.status == "Escalated" else "REP"
                preview = out.response.replace("\n", " ")[:60]
                print(f"  [{i:3d}/{len(rows)}] {tag} {out.request_type:15s} {preview}")
            except Exception as e:
                writer.writerow({
                    "Issue": row["Issue"],
                    "Subject": row["Subject"],
                    "Company": row["Company"],
                    "Response": "",
                    "Product Area": "",
                    "Status": "Escalated",
                    "Request Type": "invalid",
                    "Justification": f"agent crashed: {type(e).__name__}: {e}",
                })
                f.flush()
                print(f"  [{i:3d}/{len(rows)}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)

    elapsed = time.monotonic() - t0
    print(f"Wrote {len(rows)} rows to {out_path} in {elapsed:.1f}s ({elapsed/len(rows):.2f}s/row)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Stage 1: classify the ticket and plan retrieval queries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "route.md"

DEFAULT_ROUTE_MODEL = "claude-sonnet-4-6"

DomainName = Literal["HackerRank", "Claude", "Visa"]
RequestType = Literal["product_issue", "feature_request", "bug", "invalid"]


class RoutePlan(BaseModel):
    domain: Optional[DomainName] = Field(
        description="The most likely support domain, or null if the ticket is not domain-specific."
    )
    request_type: RequestType = Field(
        description="The classification of the request."
    )
    product_area: str = Field(
        description="A short snake_case label for the topic, or empty string if not applicable."
    )
    search_queries: list[str] = Field(
        default_factory=list,
        description="1-3 short retrieval queries; empty if request_type is invalid.",
    )
    is_multi_request: bool = Field(default=False)
    is_out_of_scope: bool = Field(default=False)
    notes: str = Field(default="", description="One-line context for downstream stages, or empty.")


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class Router:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str | None = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("ROUTE_MODEL", DEFAULT_ROUTE_MODEL)
        self._system_prompt = _load_system_prompt()

    def route(self, issue: str, subject: str, company: str) -> RoutePlan:
        company_norm = (company or "").strip() or "None"
        user_message = (
            f"company: {company_norm}\n"
            f"subject: {subject or '(none)'}\n"
            f"issue:\n{issue}"
        )
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=1024,
            # Routing is a fast classification -- thinking is unnecessary and
            # would burn tokens on Sonnet 4.6 (which defaults to effort=high).
            thinking={"type": "disabled"},
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            output_format=RoutePlan,
        )
        plan = response.parsed_output
        if plan is None:
            return RoutePlan(
                domain=None,
                request_type="invalid",
                product_area="",
                search_queries=[],
                is_multi_request=False,
                is_out_of_scope=True,
                notes="route stage failed to parse; defaulted to invalid",
            )
        return plan


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("usage: python code/route.py '<company>' '<subject>' '<issue>'")
        sys.exit(1)
    company, subject, issue = sys.argv[1], sys.argv[2], sys.argv[3]
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    router = Router()
    plan = router.route(issue=issue, subject=subject, company=company)
    print(plan.model_dump_json(indent=2))

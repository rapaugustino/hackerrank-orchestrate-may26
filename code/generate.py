"""Stage 3: produce the final 5-column row (status, product_area, response,
justification, request_type) given the routed plan, safety flags, and retrieved
chunks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from retrieve import Hit
from route import RoutePlan
from safety import SafetyFlags

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "generate.md"

DEFAULT_GEN_MODEL = "claude-sonnet-4-6"

Status = Literal["Replied", "Escalated"]
RequestType = Literal["product_issue", "feature_request", "bug", "invalid"]


class AgentOutput(BaseModel):
    status: Status = Field(description="Replied or Escalated, exact capitalization.")
    product_area: str = Field(description="snake_case label, or empty string.")
    response: str = Field(description="User-facing response text, or empty string for escalations.")
    justification: str = Field(description="Internal justification with chunk citations.")
    request_type: RequestType = Field(description="One of product_issue, feature_request, bug, invalid.")


def _load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _format_chunks(hits: list[Hit], max_chars_per_chunk: int = 4000) -> str:
    if not hits:
        return "(no chunks retrieved)"
    parts: list[str] = []
    for i, hit in enumerate(hits, start=1):
        body = hit.chunk.for_prompt()
        if len(body) > max_chars_per_chunk:
            body = body[:max_chars_per_chunk] + "\n...[truncated]"
        parts.append(f"--- chunk {i} (BM25 score {hit.score:.2f}) ---\n{body}")
    return "\n\n".join(parts)


class Generator:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str | None = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or os.environ.get("GEN_MODEL", DEFAULT_GEN_MODEL)
        self._system_prompt = _load_system_prompt()

    def generate(
        self,
        issue: str,
        subject: str,
        company: str,
        plan: RoutePlan,
        safety: SafetyFlags,
        hits: list[Hit],
        derived_product_area: str = "",
        canonical_areas: list[str] | None = None,
    ) -> AgentOutput:
        company_norm = (company or "").strip() or "None"
        canonical_areas = canonical_areas or []
        user_message = (
            "# Ticket\n"
            f"company: {company_norm}\n"
            f"subject: {subject or '(none)'}\n"
            f"issue:\n{issue}\n\n"
            "# Route plan\n"
            f"{plan.model_dump_json(indent=2)}\n\n"
            "# Safety flags\n"
            f"high_risk_topics: {safety.high_risk_topics}\n"
            f"injection_detected: {safety.injection_detected}\n"
            f"is_empty_or_garbled: {safety.is_empty_or_garbled}\n\n"
            "# Product area guidance\n"
            f"derived_from_top_chunk: {derived_product_area!r}\n"
            f"canonical_label_set: {canonical_areas}\n\n"
            "# Retrieved corpus chunks\n"
            f"{_format_chunks(hits)}"
        )
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            output_format=AgentOutput,
        )
        out = response.parsed_output
        if out is None:
            return AgentOutput(
                status="Escalated",
                product_area="",
                response="",
                justification="generate stage failed to parse a response; routed to a human.",
                request_type="invalid",
            )
        return out

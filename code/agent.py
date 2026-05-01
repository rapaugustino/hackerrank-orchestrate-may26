"""Orchestrator: route -> retrieve -> generate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import anthropic

from generate import AgentOutput, Generator
from retrieve import DISPLAY_TO_DOMAIN, Hit, Retriever, derive_product_area
from route import RoutePlan, Router
from safety import SafetyFlags, assess as safety_assess

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


@dataclass
class TraceEntry:
    safety: SafetyFlags
    plan: RoutePlan
    hits: list[Hit]
    derived_product_area: str
    canonical_areas: list[str]
    output: AgentOutput


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        data_root: Path | None = None,
        top_k: int = 5,
    ):
        self.client = client or anthropic.Anthropic()
        self.retriever = Retriever(data_root or DEFAULT_DATA_ROOT)
        self.router = Router(client=self.client)
        self.generator = Generator(client=self.client)
        self.top_k = top_k

    def run(self, issue: str, subject: str, company: str) -> TraceEntry:
        safety = safety_assess(issue=issue, subject=subject)
        plan = self.router.route(issue=issue, subject=subject, company=company)
        hits = self._retrieve(plan)
        domain_key = DISPLAY_TO_DOMAIN.get(plan.domain) if plan.domain else None
        derived_pa = derive_product_area(hits) if hits else ""
        canonical_areas = (
            self.retriever.canonical_product_areas(domain_key) if domain_key else []
        )
        output = self.generator.generate(
            issue=issue,
            subject=subject,
            company=company,
            plan=plan,
            safety=safety,
            hits=hits,
            derived_product_area=derived_pa,
            canonical_areas=canonical_areas,
        )
        # Fail-safe: low confidence on a Replied row -> escalate. Better to send
        # a borderline ticket to a human than to ship a wrong answer.
        if output.confidence == "low" and output.status == "Replied":
            output = output.model_copy(update={
                "status": "Escalated",
                "response": "",
                "justification": (
                    output.justification.rstrip(". ")
                    + ". [Auto-escalated: confidence=low.]"
                ),
            })
        return TraceEntry(
            safety=safety,
            plan=plan,
            hits=hits,
            derived_product_area=derived_pa,
            canonical_areas=canonical_areas,
            output=output,
        )

    def _retrieve(self, plan: RoutePlan) -> list[Hit]:
        if not plan.domain or not plan.search_queries:
            return []
        domain_key = DISPLAY_TO_DOMAIN.get(plan.domain)
        if domain_key is None:
            return []
        return self.retriever.search_multi(
            domain=domain_key,
            queries=plan.search_queries,
            top_k=self.top_k,
        )

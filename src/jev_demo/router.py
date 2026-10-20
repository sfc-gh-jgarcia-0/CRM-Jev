"""Jev router: classifies query tier with snowflake-decision-fast via the TypeSafe System One API."""

from __future__ import annotations

import time

import httpx

from .agent_client import auth_headers
from .config import Config
from .metrics import RoutingDecision, TokenUsage

TIER_QUESTION = {
    "tier": {
        "type": "choice",
        "instructions": (
            "Classify how much reasoning this question about ACME's CRM, contracts, suppliers, "
            "shipments and compliance data needs before it can be answered."
        ),
        "criteria": {
            "tier1": "Simple lookup: a single count, list, filter or average over one entity",
            "tier2": "Analytical: multiple filters, joins, comparisons, rankings, breakdowns or trends",
            "tier3": "Complex: compliance reviews, risk assessment across domains, root cause, "
            "recommendations, executive summaries or multi-step judgment",
        },
    }
}


class JevRouter:
    def __init__(self, cfg: Config, http: httpx.Client):
        self.cfg = cfg
        self.http = http

    def route(self, question: str) -> tuple[RoutingDecision, TokenUsage, float]:
        """Returns (decision, token usage, latency_ms)."""
        t0 = time.perf_counter()
        r = self.http.post(
            self.cfg.jev_url,
            headers=auth_headers(self.cfg.jev_token),
            json={"model": self.cfg.jev_model, "state": question, "questions": TIER_QUESTION},
            timeout=60,
        )
        latency = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        body = r.json()
        ans = body["answers"]["tier"]
        tier = raw_tier = int(ans["choice"].removeprefix("tier"))
        confidence = float(ans.get("confidence", 1.0))
        escalated = False
        # Low-confidence decisions escalate one tier so uncertainty costs money, not quality.
        if confidence < self.cfg.router_min_confidence and tier < 3:
            tier += 1
            escalated = True
        usage = body.get("usage", {})
        tokens = TokenUsage(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0))
        decision = RoutingDecision(
            tier=tier,
            raw_tier=raw_tier,
            model=self.cfg.tier_models[tier],
            confidence=confidence,
            probabilities=ans.get("probabilities", {}),
            escalated=escalated,
        )
        return decision, tokens, latency

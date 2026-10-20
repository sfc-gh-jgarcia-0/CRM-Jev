"""Configuration loaded from .env plus runtime-injected secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    account_host: str
    gateway_base_url: str
    pat: str
    agent_fqn: str
    warehouse: str
    baseline_model: str
    jev_url: str
    jev_model: str
    jev_api_key: str
    router_min_confidence: float
    tier_models: dict[int, str]
    judge_model: str
    enable_judge: bool
    pricing: dict
    credit_price_usd: float
    timeout_s: float
    budget_seconds: int | None = None
    budget_tokens: int | None = None

    @property
    def jev_token(self) -> str:
        # TypeSafe direct endpoint uses its own key; the AI Gateway path uses the Snowflake PAT.
        return self.jev_api_key or self.pat


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(f"Missing required setting: {name}")
    return val


def load_config() -> Config:
    load_dotenv(ROOT / ".env", override=False)
    pat = os.getenv("SNOWFLAKE_PAT", "").strip()
    if not pat:
        raise SystemExit(
            "SNOWFLAKE_PAT is not set. Inject it at runtime, e.g.\n"
            '  SNOWFLAKE_PAT="<snowflake_ai_pat>" uv run jev-demo ...'
        )
    pricing_file = ROOT / os.getenv("PRICING_FILE", "config/pricing.yaml")
    return Config(
        account_host=_req("SNOWFLAKE_ACCOUNT_HOST"),
        gateway_base_url=_req("AI_GATEWAY_BASE_URL").rstrip("/"),
        pat=pat,
        agent_fqn=_req("SNOWFLAKE_AGENT_FQN"),
        warehouse=os.getenv("AGENT_WAREHOUSE", "COMPUTE_WH"),
        baseline_model=_req("BASELINE_MODEL"),
        jev_url=_req("TYPESAFE_JEV_API_URL"),
        jev_model=_req("TYPESAFE_JEV_MODEL"),
        jev_api_key=os.getenv("TYPESAFE_JEV_API_KEY", "").strip(),
        router_min_confidence=float(os.getenv("ROUTER_MIN_CONFIDENCE", "0.25")),
        tier_models={
            1: _req("TYPESAFE_TIER1_MODEL"),
            2: _req("TYPESAFE_TIER2_MODEL"),
            3: _req("TYPESAFE_TIER3_MODEL"),
        },
        judge_model=_req("JUDGE_MODEL"),
        enable_judge=os.getenv("ENABLE_JUDGE", "true").lower() == "true",
        pricing=yaml.safe_load(pricing_file.read_text()),
        credit_price_usd=float(os.getenv("CREDIT_PRICE_USD", "3.0")),
        timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", "300")),
        budget_seconds=int(os.getenv("AGENT_BUDGET_SECONDS")) if os.getenv("AGENT_BUDGET_SECONDS") else None,
        budget_tokens=int(os.getenv("AGENT_BUDGET_TOKENS")) if os.getenv("AGENT_BUDGET_TOKENS") else None,
    )

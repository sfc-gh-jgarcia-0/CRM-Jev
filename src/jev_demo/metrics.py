"""Pydantic schemas for per-step, per-query, and aggregate metrics."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    input_tokens: int = 0  # total input, including cached
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Step(BaseModel):
    """One unit of work: an LLM invocation or a tool execution."""

    step: Literal["router", "orchestration", "tool_execution", "synthesis", "judge"]
    model: str | None = None
    tool: str | None = None
    latency_ms: float = 0.0
    tokens: TokenUsage | None = None
    credits: float = 0.0
    cost_usd: float = 0.0
    status: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)


class Quality(BaseModel):
    correctness: float | None = None
    completeness: float | None = None
    relevance: float | None = None
    score: float | None = None
    rationale: str = ""
    sql_valid: bool | None = None
    tool_selection_correct: bool | None = None


class RoutingDecision(BaseModel):
    tier: int  # final tier used (after any low-confidence escalation)
    raw_tier: int  # tier chosen by the router
    model: str
    confidence: float
    probabilities: dict[str, float] = Field(default_factory=dict)
    escalated: bool = False


class QueryResult(BaseModel):
    query_id: str
    scenario: Literal["A", "C"]
    expected_tier: int
    question: str
    answer: str = ""
    error: str | None = None
    agent_model: str
    routing: RoutingDecision | None = None
    tools_used: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    quality: Quality = Field(default_factory=Quality)
    total_latency_ms: float = 0.0  # end-to-end, excludes judge
    time_to_first_token_ms: float | None = None
    request_id: str | None = None
    started_at: str = ""

    def pipeline_steps(self) -> list[Step]:
        return [s for s in self.steps if s.step != "judge"]

    @property
    def tokens(self) -> TokenUsage:
        t = TokenUsage()
        for s in self.pipeline_steps():
            if s.tokens:
                t.input_tokens += s.tokens.input_tokens
                t.cache_read_tokens += s.tokens.cache_read_tokens
                t.cache_write_tokens += s.tokens.cache_write_tokens
                t.output_tokens += s.tokens.output_tokens
        return t

    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.pipeline_steps())

    @property
    def credits(self) -> float:
        return sum(s.credits for s in self.pipeline_steps())

    def step_latency(self, name: str) -> float:
        return sum(s.latency_ms for s in self.steps if s.step == name)

"""Cortex Agent client: replicates the named agent inline so the orchestration model can be overridden."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config
from .metrics import TokenUsage


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@dataclass
class ToolSpan:
    tool_use_id: str
    name: str
    tool_type: str
    started: float
    ended: float | None = None
    status: str = "pending"
    sql: str | None = None
    result_preview: str = ""


@dataclass
class AgentRun:
    answer: str = ""
    model: str = ""
    total_ms: float = 0.0
    first_event_ms: float | None = None
    first_text_ms: float | None = None
    tools: list[ToolSpan] = field(default_factory=list)
    usage_by_model: dict[str, TokenUsage] = field(default_factory=dict)
    request_id: str | None = None
    error: str | None = None
    status: str = ""


class AgentClient:
    """Fetches the agent object's spec once, then runs it inline per query with a chosen model."""

    def __init__(self, cfg: Config, http: httpx.Client):
        self.cfg = cfg
        self.http = http
        self.base = f"https://{cfg.account_host}/api/v2"
        self.spec = self._load_spec()

    def _load_spec(self) -> dict[str, Any]:
        db, schema, name = self.cfg.agent_fqn.split(".")
        r = self.http.get(
            f"{self.base}/databases/{db}/schemas/{schema}/agents/{name}",
            headers=auth_headers(self.cfg.pat),
        )
        r.raise_for_status()
        body = r.json()
        spec = body.get("agent_spec", body)
        return json.loads(spec) if isinstance(spec, str) else spec

    def _payload(self, question: str, model: str) -> dict[str, Any]:
        s = self.spec
        payload: dict[str, Any] = {
            "stream": True,
            "models": {"orchestration": model},
            "instructions": s.get("instructions", {}),
            "orchestration": s.get("orchestration", {}),
            "tools": s.get("tools", []),
            "tool_resources": s.get("tool_resources", {}),
            "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
        }
        # sample_questions is agent-object metadata, not a run field
        payload["instructions"] = {k: v for k, v in payload["instructions"].items() if k != "sample_questions"}
        if self.cfg.budget_seconds or self.cfg.budget_tokens:
            budget = dict(payload["orchestration"].get("budget", {}))
            if self.cfg.budget_seconds:
                budget["seconds"] = self.cfg.budget_seconds
            if self.cfg.budget_tokens:
                budget["tokens"] = self.cfg.budget_tokens
            payload["orchestration"] = {**payload["orchestration"], "budget": budget}
        return payload

    def run(self, question: str, model: str) -> AgentRun:
        out = AgentRun(model=model)
        spans: dict[str, ToolSpan] = {}
        t0 = time.perf_counter()
        ms = lambda: (time.perf_counter() - t0) * 1000  # noqa: E731
        final: dict[str, Any] | None = None
        try:
            with self.http.stream(
                "POST",
                f"{self.base}/cortex/agent:run",
                headers={**auth_headers(self.cfg.pat), "Accept": "text/event-stream"},
                json=self._payload(question, model),
                timeout=self.cfg.timeout_s,
            ) as r:
                out.request_id = r.headers.get("x-snowflake-request-id") or r.headers.get("x-request-id")
                if r.status_code != 200:
                    out.error = f"HTTP {r.status_code}: {r.read().decode()[:500]}"
                    return out
                event, data_lines = None, []
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "" and event:
                        data = _safe_json("\n".join(data_lines))
                        final = self._on_event(event, data, ms(), out, spans) or final
                        event, data_lines = None, []
                if event and data_lines:  # trailing event without blank line
                    final = self._on_event(event, _safe_json("\n".join(data_lines)), ms(), out, spans) or final
        except httpx.HTTPError as e:
            out.error = f"{type(e).__name__}: {e}"
        out.total_ms = ms()
        out.tools = list(spans.values())
        if final:
            self._apply_final(final, out)
        elif not out.error:
            out.error = "Stream ended without a final response event"
        return out

    def _on_event(self, event, data, now, out: AgentRun, spans) -> dict | None:
        if out.first_event_ms is None:
            out.first_event_ms = now
        if event == "response.text.delta" and out.first_text_ms is None:
            out.first_text_ms = now
        elif event == "response.tool_use" and isinstance(data, dict):
            tid = data.get("tool_use_id", f"t{len(spans)}")
            inp = data.get("input") or {}
            spans[tid] = ToolSpan(
                tool_use_id=tid,
                name=data.get("name", "?"),
                tool_type=data.get("type", "?"),
                started=now,
                sql=inp.get("sql") if isinstance(inp, dict) else None,
            )
        elif event == "response.tool_result" and isinstance(data, dict):
            span = spans.get(data.get("tool_use_id", ""))
            if span:
                span.ended = now
                span.status = data.get("status", "unknown")
                span.result_preview = json.dumps(data.get("content", ""))[:8000]
        elif event == "error":
            out.error = json.dumps(data)[:500]
        elif event == "response" and isinstance(data, dict):
            return data
        return None

    @staticmethod
    def _apply_final(final: dict[str, Any], out: AgentRun) -> None:
        out.status = final.get("status", "")
        out.answer = "\n".join(c.get("text", "") for c in final.get("content", []) if c.get("type") == "text").strip()
        meta = final.get("metadata", {})
        out.model = meta.get("orchestration_model", out.model)
        for tc in meta.get("usage", {}).get("tokens_consumed", []):
            inp, outp = tc.get("input_tokens", {}), tc.get("output_tokens", {})
            u = out.usage_by_model.setdefault(tc.get("model_name", "?"), TokenUsage())
            u.input_tokens += inp.get("total", 0)
            u.cache_read_tokens += inp.get("cache_read", 0)
            u.cache_write_tokens += inp.get("cache_write", 0)
            u.output_tokens += outp.get("total", 0)
        out.request_id = out.request_id or meta.get("run_id")


def _safe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s

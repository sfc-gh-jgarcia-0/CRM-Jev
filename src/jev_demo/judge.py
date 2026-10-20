"""LLM-as-judge via the AI Gateway (Anthropic Messages API for Claude, Chat Completions otherwise)."""

from __future__ import annotations

import json
import re
import time

import httpx

from .agent_client import AgentRun, auth_headers
from .config import Config
from .metrics import Quality, TokenUsage

RUBRIC = """You are grading an AI data assistant's answer to a business question about ACME Corp's
CRM, contract, supply chain and compliance data. Judge ONLY against the tool evidence shown
(SQL and results the assistant actually retrieved). Do not reward confident prose unsupported by evidence.
Evidence may be truncated; do not penalize an answer only because supporting rows were cut off — penalize
claims that contradict the evidence. An answer that only says a time or token limit was reached scores 1.

Score each 1-10:
- correctness: numbers/facts match the evidence and the question was interpreted correctly
- completeness: every part of the question is addressed
- relevance: on-topic, no hallucinated facts beyond the evidence

Reply with ONLY a JSON object:
{"correctness": n, "completeness": n, "relevance": n, "rationale": "<one sentence>"}"""

WEIGHTS = {"correctness": 0.5, "completeness": 0.3, "relevance": 0.2}


class Judge:
    def __init__(self, cfg: Config, http: httpx.Client):
        self.cfg = cfg
        self.http = http

    def grade(self, question: str, run: AgentRun) -> tuple[Quality, TokenUsage, float]:
        evidence = "\n\n".join(
            f"[{t.name} | {t.status}]\nSQL: {t.sql or '-'}\nRESULT: {t.result_preview[:6000]}" for t in run.tools
        ) or "(no tools were called)"
        prompt = f"{RUBRIC}\n\nQUESTION:\n{question}\n\nTOOL EVIDENCE:\n{evidence[:40000]}\n\nANSWER:\n{run.answer[:8000]}"
        t0 = time.perf_counter()
        text, tokens = self._complete(prompt)
        latency = (time.perf_counter() - t0) * 1000
        q = Quality()
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                q.correctness, q.completeness, q.relevance = (float(d[k]) for k in WEIGHTS)
                q.score = round(sum(float(d[k]) * w for k, w in WEIGHTS.items()), 2)
                q.rationale = d.get("rationale", "")
            except (KeyError, ValueError, json.JSONDecodeError):
                q.rationale = f"Unparseable judge output: {text[:200]}"
        return q, tokens, latency

    def _complete(self, prompt: str) -> tuple[str, TokenUsage]:
        model = self.cfg.judge_model
        base = self.cfg.gateway_base_url
        if model.startswith("claude"):
            r = self.http.post(
                f"{base}/v1/messages",
                headers=auth_headers(self.cfg.pat),
                json={"model": model, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]},
                timeout=self.cfg.timeout_s,
            )
            r.raise_for_status()
            b = r.json()
            u = b.get("usage", {})
            text = "".join(c.get("text", "") for c in b.get("content", []))
            return text, TokenUsage(
                input_tokens=u.get("input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0),
                cache_read_tokens=u.get("cache_read_input_tokens", 0),
                cache_write_tokens=u.get("cache_creation_input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
            )
        r = self.http.post(
            f"{base}/v1/chat/completions",
            headers=auth_headers(self.cfg.pat),
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=self.cfg.timeout_s,
        )
        r.raise_for_status()
        b = r.json()
        u = b.get("usage", {})
        return b["choices"][0]["message"]["content"], TokenUsage(
            input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0)
        )

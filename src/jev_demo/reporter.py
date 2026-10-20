"""Aggregate metrics, console summary, HTML scorecard, and ACCOUNT_USAGE reconciliation SQL."""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from rich.console import Console
from rich.table import Table

from .config import ROOT, Config
from .metrics import QueryResult

STEPS = ["router", "orchestration", "tool_execution", "synthesis"]


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _mean(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return statistics.mean(v) if v else None


def aggregate(rows: list[QueryResult]) -> dict:
    ok = [r for r in rows if not r.error]
    lat = [r.total_latency_ms for r in ok]
    agg = {
        "n": len(rows),
        "errors": len(rows) - len(ok),
        "avg_latency_ms": _mean(lat),
        "p50_latency_ms": _pct(lat, 0.5),
        "p95_latency_ms": _pct(lat, 0.95),
        "avg_ttft_ms": _mean([r.time_to_first_token_ms for r in ok]),
        "avg_tokens": _mean([r.tokens.total for r in ok]),
        "avg_input_tokens": _mean([r.tokens.input_tokens for r in ok]),
        "avg_cached_input_tokens": _mean([r.tokens.cache_read_tokens for r in ok]),
        "avg_output_tokens": _mean([r.tokens.output_tokens for r in ok]),
        "total_cost_usd": sum(r.cost_usd for r in ok),
        "avg_cost_usd": _mean([r.cost_usd for r in ok]),
        "total_credits": sum(r.credits for r in ok),
        "avg_quality": _mean([r.quality.score for r in ok]),
        "sql_valid_rate": _rate([r.quality.sql_valid for r in ok]),
        "tool_selection_rate": _rate([r.quality.tool_selection_correct for r in ok]),
        "judge_cost_usd": sum(s.cost_usd for r in ok for s in r.steps if s.step == "judge"),
        "steps": {
            s: {
                "avg_latency_ms": _mean([r.step_latency(s) for r in ok if any(x.step == s for x in r.steps)]),
                "avg_tokens": _mean(
                    [sum(x.tokens.total for x in r.steps if x.step == s and x.tokens) for r in ok if any(x.step == s for x in r.steps)]
                ),
                "avg_cost_usd": _mean(
                    [sum(x.cost_usd for x in r.steps if x.step == s) for r in ok if any(x.step == s for x in r.steps)]
                ),
            }
            for s in STEPS
        },
        "by_tier": {},
    }
    for tier in (1, 2, 3):
        t = [r for r in ok if r.expected_tier == tier]
        if t:
            agg["by_tier"][tier] = {
                "n": len(t),
                "avg_latency_ms": _mean([r.total_latency_ms for r in t]),
                "avg_cost_usd": _mean([r.cost_usd for r in t]),
                "avg_tokens": _mean([r.tokens.total for r in t]),
                "avg_quality": _mean([r.quality.score for r in t]),
                "models": sorted({r.agent_model for r in t}),
            }
    routed = [r for r in ok if r.routing]
    if routed:
        agg["router"] = {
            "raw_accuracy": _rate([r.routing.raw_tier == r.expected_tier for r in routed]),
            "accuracy": _rate([r.routing.tier == r.expected_tier for r in routed]),
            "under_routed": sum(r.routing.tier < r.expected_tier for r in routed),
            "over_routed": sum(r.routing.tier > r.expected_tier for r in routed),
            "escalations": sum(r.routing.escalated for r in routed),
            "avg_confidence": _mean([r.routing.confidence for r in routed]),
        }
    return agg


def _rate(vals: list[bool | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _delta(a: float | None, c: float | None) -> float | None:
    return (c - a) / a * 100 if a and c is not None else None


def write_reports(cfg: Config, results: dict[str, list[QueryResult]], out_dir: Path, started: datetime, ended: datetime, console: Console) -> None:
    aggs = {sc: aggregate(rows) for sc, rows in results.items()}
    (out_dir / "summary.json").write_text(json.dumps(aggs, indent=2, default=str))
    _console(aggs, console)
    _reconcile_sql(out_dir, started, ended)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["ms"] = lambda v: "—" if v is None else f"{v / 1000:,.2f}s"
    env.filters["usd"] = lambda v: "—" if v is None else f"${v:,.4f}"
    env.filters["num"] = lambda v: "—" if v is None else f"{v:,.0f}"
    env.filters["score"] = lambda v: "—" if v is None else f"{v:.2f}"
    env.filters["pct"] = lambda v: "—" if v is None else f"{v:.0%}"
    env.filters["delta"] = lambda v: "—" if v is None else f"{v:+.1f}%"
    env.filters["fmt"] = lambda v, kind: env.filters[kind](v)
    html = env.get_template("scorecard.html").render(
        cfg=cfg,
        aggs=aggs,
        results=results,
        delta=_delta,
        generated=ended.strftime("%Y-%m-%d %H:%M UTC"),
        chart_data=json.dumps(_chart_data(aggs)),
        metadata=json.dumps(
            {
                "generated": ended.date().isoformat(),
                "intent": "Compare single-model vs Jev-routed Cortex Agent on latency, cost, tokens, quality",
                "dataSources": [
                    {"type": "rest", "endpoint": "/api/v2/cortex/agent:run", "agent": cfg.agent_fqn},
                    {"type": "rest", "endpoint": cfg.jev_url, "model": cfg.jev_model},
                    {"type": "file", "path": "config/pricing.yaml"},
                ],
                "sections": [{"id": "summary"}, {"id": "steps"}, {"id": "tiers"}, {"id": "router"}, {"id": "queries"}],
                "producerNotes": "Re-run `uv run jev-demo` to refresh; values are inlined from results/*/scenario_*.json.",
            }
        ),
    )
    (out_dir / "comparison.html").write_text(html)


def _chart_data(aggs: dict) -> dict:
    return {
        sc: {
            "tier_latency": [aggs[sc]["by_tier"].get(t, {}).get("avg_latency_ms") for t in (1, 2, 3)],
            "tier_cost": [aggs[sc]["by_tier"].get(t, {}).get("avg_cost_usd") for t in (1, 2, 3)],
            "tier_quality": [aggs[sc]["by_tier"].get(t, {}).get("avg_quality") for t in (1, 2, 3)],
        }
        for sc in aggs
    }


def _console(aggs: dict, console: Console) -> None:
    scs = list(aggs)
    t = Table(title="Scenario comparison")
    t.add_column("Metric")
    for sc in scs:
        t.add_column(f"Scenario {sc}", justify="right")
    if len(scs) == 2:
        t.add_column("C vs A", justify="right")
    f = {
        "avg_latency_ms": lambda v: f"{v / 1000:.2f}s",
        "p95_latency_ms": lambda v: f"{v / 1000:.2f}s",
        "avg_ttft_ms": lambda v: f"{v / 1000:.2f}s",
        "avg_tokens": lambda v: f"{v:,.0f}",
        "avg_cost_usd": lambda v: f"${v:.4f}",
        "total_cost_usd": lambda v: f"${v:.4f}",
        "avg_quality": lambda v: f"{v:.2f}",
        "tool_selection_rate": lambda v: f"{v:.0%}",
    }
    for k, fmt in f.items():
        vals = [aggs[sc].get(k) for sc in scs]
        row = [k] + ["—" if v is None else fmt(v) for v in vals]
        if len(scs) == 2:
            d = _delta(vals[0], vals[1])
            row.append("—" if d is None else f"{d:+.1f}%")
        t.add_row(*row)
    console.print(t)
    if "C" in aggs and aggs["C"].get("router"):
        r = aggs["C"]["router"]
        console.print(
            f"Router accuracy raw {r['raw_accuracy']:.0%} / final {r['accuracy']:.0%} | under-routed {r['under_routed']} | "
            f"over-routed {r['over_routed']} | escalations {r['escalations']} | avg confidence {r['avg_confidence']:.2f}"
        )


def _reconcile_sql(out_dir: Path, started: datetime, ended: datetime) -> None:
    s, e = started.strftime("%Y-%m-%d %H:%M:%S"), ended.strftime("%Y-%m-%d %H:%M:%S")
    (out_dir / "reconcile.sql").write_text(
        f"""-- Actual billed credits for this run window (ACCOUNT_USAGE has up to ~2h latency).
-- Agent orchestration credits, by model:
SELECT f.key AS model, COUNT(*) AS requests, SUM(h.token_credits) AS token_credits,
       SUM(h.metadata:sql_query_credits::float) AS sql_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AGENT_USAGE_HISTORY h,
     LATERAL FLATTEN(h.tokens_granular[0]) r, LATERAL FLATTEN(r.value:cortex_agents) f
WHERE h.start_time BETWEEN '{s}'::timestamp_ltz AND '{e}'::timestamp_ltz
  AND h.user_name = CURRENT_USER()
GROUP BY 1 ORDER BY 1;

-- Gateway credits (Jev router + judge), by model:
SELECT o.key AS model, COUNT(*) AS requests, SUM(o.value:input_tokens) AS input_tokens,
       SUM(o.value:output_tokens) AS output_tokens, SUM(h.credits) AS credits
FROM SNOWFLAKE.ACCOUNT_USAGE.AI_GATEWAY_USAGE_HISTORY h, LATERAL FLATTEN(h.operation_details) o
WHERE h.start_time BETWEEN DATEADD(minute, -1, '{s}'::timestamp_ltz) AND DATEADD(minute, 1, '{e}'::timestamp_ltz)
GROUP BY 1 ORDER BY 1;
"""
    )

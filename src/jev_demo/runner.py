"""Test orchestrator: runs Scenario A (single model) and/or Scenario C (Jev-routed) over the question bank."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx
import yaml
from rich.console import Console
from rich.progress import Progress

from .agent_client import AgentClient, AgentRun
from .config import ROOT, Config, load_config
from .judge import Judge
from .metrics import QueryResult, Step, TokenUsage
from .pricing import credits_for, to_usd
from .reporter import write_reports
from .router import JevRouter

console = Console()
ANALYST_NAMES = {"analyst", "system_execute_sql"}


def load_questions(tiers: set[int] | None, ids: set[str] | None, limit: int | None) -> list[dict]:
    bank = yaml.safe_load((ROOT / "questions" / "question_bank.yaml").read_text())
    out = []
    for key, items in bank.items():
        tier = int(key.removeprefix("tier"))
        if tiers and tier not in tiers:
            continue
        for q in items[:limit] if limit else items:
            if not ids or q["id"] in ids:
                out.append({**q, "tier": tier})
    return out


def _priced_step(cfg: Config, step: Step, surface: str) -> Step:
    if step.tokens and step.model:
        credits, priced = credits_for(cfg, surface, step.model, step.tokens)
        step.credits, step.cost_usd = credits, to_usd(cfg, credits)
        if not priced:
            step.detail["pricing"] = f"no rate for {surface}/{step.model} in pricing.yaml"
    return step


def agent_steps(cfg: Config, run: AgentRun) -> list[Step]:
    tool_ms = sum((t.ended or run.total_ms) - t.started for t in run.tools)
    synth_ms = run.total_ms - run.first_text_ms if run.first_text_ms is not None else 0.0
    llm_ms = max(run.total_ms - tool_ms - synth_ms, 0.0)
    steps: list[Step] = []
    usage = run.usage_by_model or {run.model: TokenUsage()}
    for i, (model, tokens) in enumerate(usage.items()):
        steps.append(
            _priced_step(
                cfg,
                Step(
                    step="orchestration",
                    model=model,
                    latency_ms=llm_ms if i == 0 else 0.0,
                    tokens=tokens,
                    detail={"note": "Agent API reports tokens per model; includes planning, tool calls and synthesis"},
                ),
                "agent",
            )
        )
    for t in run.tools:
        steps.append(
            Step(
                step="tool_execution",
                tool=t.name,
                latency_ms=(t.ended or run.total_ms) - t.started,
                status=t.status,
                detail={"type": t.tool_type, **({"sql": t.sql} if t.sql else {})},
            )
        )
    steps.append(Step(step="synthesis", model=run.model, latency_ms=synth_ms, detail={"tokens": "in orchestration step"}))
    return steps


def evaluate_tools(expected: str, run: AgentRun) -> tuple[bool | None, bool | None]:
    names = {t.name.lower() for t in run.tools} | {t.tool_type.lower() for t in run.tools}
    used_analyst = bool(names & ANALYST_NAMES) or any("analyst" in n or "semantic" in n for n in names)
    used_compliance = "compliancereview" in names
    sel = {
        "Analyst": used_analyst,
        "ComplianceReview": used_compliance,
        "any": used_analyst or used_compliance,
    }.get(expected)
    sql_spans = [t for t in run.tools if t.sql]
    sql_valid = all(t.status == "success" for t in sql_spans) if sql_spans else None
    return sel, sql_valid


def run_one(cfg: Config, scenario: str, q: dict, agent: AgentClient, router: JevRouter | None, judge: Judge | None) -> QueryResult:
    res = QueryResult(
        query_id=q["id"],
        scenario=scenario,
        expected_tier=q["tier"],
        question=q["question"],
        agent_model=cfg.baseline_model,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    router_ms = 0.0
    if scenario == "C":
        try:
            decision, tokens, router_ms = router.route(q["question"])
        except httpx.HTTPError as e:
            res.error = f"router: {e}"
            return res
        res.routing, res.agent_model = decision, decision.model
        res.steps.append(
            _priced_step(
                cfg,
                Step(
                    step="router",
                    model=cfg.jev_model,
                    latency_ms=router_ms,
                    tokens=tokens,
                    detail={"tier": decision.tier, "confidence": decision.confidence, "escalated": decision.escalated},
                ),
                "gateway",
            )
        )

    run = agent.run(q["question"], res.agent_model)
    res.answer, res.error, res.request_id = run.answer, run.error, run.request_id
    res.tools_used = [t.name for t in run.tools]
    res.steps.extend(agent_steps(cfg, run))
    res.total_latency_ms = router_ms + run.total_ms
    res.time_to_first_token_ms = router_ms + run.first_text_ms if run.first_text_ms is not None else None
    res.quality.tool_selection_correct, res.quality.sql_valid = evaluate_tools(q["expected_tool"], run)

    if judge and run.answer:
        try:
            quality, tokens, ms = judge.grade(q["question"], run)
            quality.tool_selection_correct, quality.sql_valid = res.quality.tool_selection_correct, res.quality.sql_valid
            res.quality = quality
            res.steps.append(_priced_step(cfg, Step(step="judge", model=cfg.judge_model, latency_ms=ms, tokens=tokens), "gateway"))
        except httpx.HTTPError as e:
            res.quality.rationale = f"judge failed: {e}"
    return res


def main() -> None:
    p = argparse.ArgumentParser(description="Jev router benchmark: Scenario A (single model) vs C (Jev-routed)")
    p.add_argument("--scenario", choices=["a", "c", "all"], default="all")
    p.add_argument("--tier", type=int, action="append", choices=[1, 2, 3], help="Restrict to tier(s); repeatable")
    p.add_argument("--ids", help="Comma-separated question ids, e.g. t1-01,t3-02")
    p.add_argument("--limit", type=int, help="Max questions per tier")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--route-only", action="store_true", help="Only call the Jev router (fast router-accuracy check)")
    args = p.parse_args()

    cfg = load_config()
    questions = load_questions(set(args.tier or []) or None, set(args.ids.split(",")) if args.ids else None, args.limit)
    if not questions:
        raise SystemExit("No questions selected")

    http = httpx.Client(timeout=cfg.timeout_s, limits=httpx.Limits(max_connections=args.concurrency * 3))
    router = JevRouter(cfg, http)
    if args.route_only:
        return route_only(router, questions)

    agent = AgentClient(cfg, http)
    judge = Judge(cfg, http) if cfg.enable_judge and not args.no_judge else None
    scenarios = ["A", "C"] if args.scenario == "all" else [args.scenario.upper()]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "results" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)

    results: dict[str, list[QueryResult]] = {}
    for sc in scenarios:
        console.rule(f"Scenario {sc} — {len(questions)} questions")
        rows: list[QueryResult] = []
        with Progress(console=console) as prog, ThreadPoolExecutor(args.concurrency) as pool:
            task = prog.add_task(f"Scenario {sc}", total=len(questions))
            futs = {pool.submit(run_one, cfg, sc, q, agent, router, judge): q for q in questions}
            for f in as_completed(futs):
                r = f.result()
                rows.append(r)
                tag = f"T{r.routing.tier}" if r.routing else ""
                status = "[red]ERR[/]" if r.error else f"{r.total_latency_ms / 1000:.1f}s q={r.quality.score}"
                prog.console.print(f"  {r.query_id} {tag} {r.agent_model}: {status}")
                prog.advance(task)
        rows.sort(key=lambda r: r.query_id)
        results[sc] = rows
        (out_dir / f"scenario_{sc.lower()}.json").write_text(
            json.dumps([r.model_dump() | {"tokens": r.tokens.model_dump(), "cost_usd": r.cost_usd} for r in rows], indent=2)
        )

    write_reports(cfg, results, out_dir, started, datetime.now(timezone.utc), console)
    console.print(f"\n[bold green]Results written to[/] {out_dir}")


def route_only(router: JevRouter, questions: list[dict]) -> None:
    from rich.table import Table

    t = Table(title="Jev router accuracy")
    for c in ("id", "expected", "raw", "final", "conf", "ms", "model", "raw ok", "final ok"):
        t.add_column(c)
    raw_hits = hits = 0
    for q in questions:
        d, _, ms = router.route(q["question"])
        hit, raw_hit = d.tier == q["tier"], d.raw_tier == q["tier"]
        hits += hit
        raw_hits += raw_hit
        t.add_row(q["id"], str(q["tier"]), str(d.raw_tier), f"{d.tier}{'*' if d.escalated else ''}", f"{d.confidence:.2f}",
                  f"{ms:.0f}", d.model, "✓" if raw_hit else "✗", "✓" if hit else "✗")
    console.print(t)
    n = len(questions)
    console.print(f"Raw router accuracy: {raw_hits}/{n} = {raw_hits / n:.0%} | after escalation: {hits}/{n} = {hits / n:.0%}  (* = escalated)")


if __name__ == "__main__":
    main()

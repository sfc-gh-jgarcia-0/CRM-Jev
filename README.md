# Jev Router Demo

Benchmark comparing a **single-model** Cortex Agent against a **Jev-routed** one on cost, latency, tokens, and answer quality.

The same CRM questions are run through the `DEMO.CRM.CRM_SCM_AGENT` Cortex Agent in two configurations:

- **Scenario A — Single model:** `claude-sonnet-4-5` handles every query.
- **Scenario C — Jev router:** `snowflake-decision-fast` (via Snowflake AI Gateway) classifies each question into a tier, then dispatches it to that tier's model.

Each LLM and tool call is measured per step. Results are written as JSON plus an HTML scorecard.

## Requirements

- Python 3.12.15+ and [uv](https://docs.astral.sh/uv/)
- Snowflake account with the `DEMO.CRM.CRM_SCM_AGENT` agent and AI Gateway access
- A Snowflake Programmatic Access Token (PAT)

## Configure the test

### 1. Install dependencies

```bash
uv sync
```

### 2. Create a Snowflake PAT

Generate a Programmatic Access Token for a user whose role can run the agent, use the AI Gateway, and use the agent warehouse. The PAT is never written to disk. It is injected at runtime as `SNOWFLAKE_PAT`.

### 3. Create `.env`

Create `.env` in the project root. It is gitignored. Example:

```ini
# Snowflake connection (host without https://, hyphens not underscores)
SNOWFLAKE_ACCOUNT_HOST=<org>-<account>.snowflakecomputing.com
# From: DESCRIBE AI GATEWAY SNOWFLAKE -> base_url
AI_GATEWAY_BASE_URL=https://<org>-<account>.snowflakecomputing.com/api/v2/aigateways/snowflake
SNOWFLAKE_AGENT_FQN=DEMO.CRM.CRM_SCM_AGENT
AGENT_WAREHOUSE=COMPUTE_WH

# Scenario A: single model for everything
BASELINE_MODEL=claude-sonnet-4-5

# Scenario C: Jev router (System One API via AI Gateway, uses the same PAT)
TYPESAFE_JEV_API_URL=https://<org>-<account>.snowflakecomputing.com/api/v2/aigateways/snowflake/v1/systemone
TYPESAFE_JEV_MODEL=snowflake-decision-fast
TYPESAFE_JEV_API_KEY=            # leave empty unless calling api.typesafe.ai directly
ROUTER_MIN_CONFIDENCE=0.25       # below this, escalate one tier; 0 disables
TYPESAFE_TIER1_MODEL=claude-haiku-4-5    # simple lookups
TYPESAFE_TIER2_MODEL=claude-haiku-4-5    # multi-step analytics
TYPESAFE_TIER3_MODEL=claude-sonnet-4-5   # complex reasoning / compliance

# Evaluation
JUDGE_MODEL=claude-sonnet-4-5    # keep constant across runs
ENABLE_JUDGE=true

# Run settings
PRICING_FILE=config/pricing.yaml
CREDIT_PRICE_USD=3.00            # contract-specific
REQUEST_TIMEOUT_S=300
AGENT_BUDGET_SECONDS=180         # overrides the agent's own budget in both scenarios
AGENT_BUDGET_TOKENS=64000
```

### 4. Review pricing (optional)

`config/pricing.yaml` holds credits per 1M tokens, which are used for cost estimates. To re-derive the rates from your account's billing, run `sql/derive_pricing.sql` in Snowflake and paste the results into the file.

### 5. Edit the questions (optional)

`questions/question_bank.yaml` groups questions by expected tier (`tier1`, `tier2`, `tier3`). Each entry has an `id`, an `expected_tool` (`Analyst`, `ComplianceReview`, or `any`), and a `question`.

## Run the test

Every command needs `SNOWFLAKE_PAT` set in the environment:

```bash
export SNOWFLAKE_PAT=<your_pat>
```

Recommended order:

```bash
# 1. Quick router check: only calls Jev and compares chosen vs expected tier
uv run jev-demo --route-only

# 2. Smoke test: a few questions, no judge
uv run jev-demo --tier 1 --limit 2 --no-judge

# 3. Full benchmark: Scenario A then C on all questions, with quality judging
uv run jev-demo --scenario all --concurrency 2
```

### CLI options

| Option | Description |
|---|---|
| `--scenario a\|c\|all` | Scenario(s) to run (default `all`) |
| `--tier N` | Restrict to tier 1, 2, or 3; repeatable |
| `--ids t1-01,t3-02` | Run specific question IDs |
| `--limit N` | Max questions per tier |
| `--concurrency N` | Parallel queries (default `2`) |
| `--no-judge` | Skip LLM-as-judge scoring |
| `--route-only` | Only test router accuracy |

### Output

Each run writes to `results/<YYYYMMDD-HHMMSS>/`:

| File | Contents |
|---|---|
| `scenario_a.json`, `scenario_c.json` | Per-query steps, latency, tokens, cost, quality |
| `summary.json` | Aggregated metrics per scenario |
| `comparison.html` | HTML scorecard comparing A and C |
| `reconcile.sql` | Query for actual billed credits in `ACCOUNT_USAGE` |

Open `comparison.html` in a browser. `ACCOUNT_USAGE` has ingestion latency, so run `reconcile.sql` a few hours after the run to get exact billed credits.

## Project layout

```
config/pricing.yaml          Model pricing
questions/question_bank.yaml Benchmark questions by tier
sql/derive_pricing.sql       Pricing derivation query
src/jev_demo/                Package (runner, router, agent_client, judge, pricing, metrics, reporter, config)
templates/scorecard.html     HTML report template
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): module design and data flow
- [build-jev.md](build-jev.md): build document and test scenarios

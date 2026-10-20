-- Derive credits per 1M tokens per model/token type from this account's agent billing.
-- Paste results into config/pricing.yaml (agent section).
WITH t AS (
  SELECT m.key model, x.key ttype, x.value::float tokens,
         c.value[r.key]['cortex_agents'][m.key][x.key]::float credits
  FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_AGENT_USAGE_HISTORY h,
       LATERAL FLATTEN(h.tokens_granular) g, LATERAL FLATTEN(g.value) r,
       LATERAL FLATTEN(r.value:cortex_agents) m, LATERAL FLATTEN(m.value) x,
       LATERAL FLATTEN(h.credits_granular) c
  WHERE h.start_time > DATEADD(day, -30, CURRENT_TIMESTAMP()) AND x.value::float > 0
)
SELECT model, ttype, COUNT(*) n, ROUND(SUM(credits) / SUM(tokens) * 1e6, 4) credits_per_1m
FROM t WHERE credits IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2;

-- Gateway (router/judge) rates per model.
SELECT o.key model, SUM(o.value:input_tokens) input_tokens, SUM(o.value:output_tokens) output_tokens,
       SUM(h.credits) credits
FROM SNOWFLAKE.ACCOUNT_USAGE.AI_GATEWAY_USAGE_HISTORY h, LATERAL FLATTEN(h.operation_details) o
WHERE h.start_time > DATEADD(day, -30, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY 1;

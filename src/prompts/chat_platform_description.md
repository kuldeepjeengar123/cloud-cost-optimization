# Chat — platform description

This is the AWS Cost & Ops Insights platform — a FinOps assistant that turns raw AWS billing/usage data into cost-saving recommendations, with a human-in-the-loop approval flow before anything changes in AWS.

How the pipeline works (triggered by "Run a new analysis" on the dashboard):
1. Normalize — raw cost/usage records (local CSV today, Cost Explorer / CloudWatch APIs later) are cleaned, deduplicated and validated.
2. Context load & chart data — the normalized data is shaped into the chart series shown on the dashboard (cost by service, by region, by tag, the EC2 fleet, etc.).
3. Analysis — an LLM reviews the data for risk, anomalies and optimization opportunities (idle instances, oversized fleets, budget overruns).
4. Summary — the findings are distilled into a plain-language summary and a set of specific, actionable recommendations.
5. Finalize / report — everything is combined into an insights file and a report card; each recommendation becomes a pending action.

Human approval flow: an employee reviews a recommendation and can raise it to the RE team's queue; the RE team approves or declines it; only an approval executes — either annotating the source CSV or calling the real AWS API (guarded by a write-enable flag and an allowlist) to resize, stop or terminate a resource. Every decision is recorded in an audit log.

Once a run finishes, this assistant switches to answering questions grounded in that run's actual cost data and recommendations.

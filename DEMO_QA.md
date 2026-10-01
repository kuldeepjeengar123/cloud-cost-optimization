# Demo Preparation: Questions and Answers

Project: AWS Cost and Ops Insights Pipeline

Purpose of this document: to prepare for questions that a listener may ask during the demonstration. The answers are based on the source code and the README in this repository. Items marked **[Verify]** depend on your environment or on external facts, and should be confirmed before the demonstration.

Please review this document before using it. It describes controls that affect a production system, and every answer should be checked against the version of the code that you demonstrate.

---

## Section 1: Purpose and business value

**Q1. What problem does this solve, in one sentence?**
It reads a company's AWS bill, uses AI to explain what is happening and what to fix, and lets a human approve any change before it is made to AWS.

**Q2. Who is the intended user?**
There are two roles. An employee sees the recommendations and raises requests. The Resource Efficiency (RE) team reviews the requests, approves or declines them, and commits the changes.

**Q3. How is this different from the AWS Cost Explorer console?**
Cost Explorer shows the data. This system also reads it for you, flags unusual spikes, forecasts the trend, writes an executive summary, and turns the advice into approval cards. Optionally, it applies an approved change.

**Q4. How is this different from AWS Trusted Advisor or Compute Optimizer?**
Those are AWS-native recommendation tools. This project combines cost data, AI narration, a governed approval queue, and rollback in one workflow. It does not claim to replace them. It could use them as additional inputs in future.

**Q5. What is the measurable benefit?**
The savings estimate per action comes from a built-in price table, not from your actual bill. Please describe it as an estimate. **[Verify]** Present real figures from a run, and do not quote the sample values from the diagrams.

---

## Section 2: Data and inputs

**Q6. Where does the data come from?**
Live from AWS through boto3. Cost Explorer supplies daily cost grouped by service, region, instance type, and a project tag. CloudWatch is a second optional source. Older versions read CSV files, but the current cost source uses the live API.

**Q7. Which tables are fetched?**
`service_daily_cost`, `region_cost`, `ec2_instance_cost`, and `tag_cost`. Each row holds a date, one grouping value, and a cost.

**Q8. What if the cost allocation tag is not activated?**
Cost Explorer rejects the tag query. The code catches this, leaves `tag_cost` empty, adds a note, and the run continues.

**Q9. How fresh is the data?**
It is as fresh as Cost Explorer. AWS can lag by up to about a day for recent charges. **[Verify]** against current AWS documentation.

**Q10. What time window is analysed?**
It is set by the `date_range` option, for example yesterday or the last N days. If none is set, a default window is used (`_DEFAULT_WINDOW_DAYS` in the source file).

**Q11. Does it support multiple AWS accounts or organisations?**
The code uses one set of credentials per run. In the code I reviewed, there is no multi-account aggregation. Please describe it as a single-account design unless you have added it.

**Q12. Does the data leave our environment?**
Yes, in a limited way. Summaries and samples of cost data are sent as text to the LLM provider. See Section 8.

---

## Section 3: The AI and language model

**Q13. Which model is used?**
By default, `config.py` sets every tier (normalise, charts, analysis, summary, chat, cheap) to the same free model, `nvidia/nemotron-3-super-120b-a12b:free`, through the OpenRouter API. **[Verify]** Check whether your environment overrides this.

**Q14. The diagram says "capable" and "cheap" tiers. Are they different models?**
The design supports different models per tier through separate settings. With the default configuration they are the same model, so there is no difference in practice until you configure them.

**Q15. How many AI calls happen in one run?**
About eleven in the analysis: one each for normalise, anomaly detection, forecast, tag governance, and summary, five for the parallel agents, and one for root cause when anomalies exist. Afterwards, one risk-explanation call is made for each executable action.

**Q16. Can the AI make up numbers?**
The design limits this. Totals, aggregations, KPIs, and chart data are computed by code. The AI decides what to highlight and writes the wording. Chart specifications contain no numbers, and Step 6 fills them from computed tables.

**Q17. Can the AI directly change AWS?**
No. The AI writes text and JSON only. Executable actions come from a code path that scans the live fleet. The AI cannot create an executable action.

**Q18. What if the AI returns broken or empty output?**
Each step has a fallback. Examples: default chart specifications, the statistical anomaly detector, the deterministic forecast, and a summary built directly from the analysis. The pipeline is designed never to crash because of one bad reply.

**Q19. What is the risk of prompt injection through cost data?**
Cost rows include free text such as tag values. The prompts label the data as untrusted. The AI has no ability to execute anything, and any change needs human approval. This reduces the risk but does not remove it. **[Verify]** Please do not claim it is immune.

**Q20. Is the output deterministic?**
No. Two runs on the same data may word findings differently. The numbers come from code and stay consistent.

**Q21. How is the statistical anomaly detector related to the AI one?**
The AI is tried first and judges what counts as a spike. If that call fails, a fixed z-score detector runs instead. In the anomaly agent, the detector's findings are also merged in, so a real spike is not lost if the AI ignores it.

---

## Section 4: Pipeline mechanics

**Q22. Why are five agents run in parallel?**
They are independent. Each reads only the shared context, never another agent's output. Running them together takes about as long as the slowest one (20.6 seconds in the sample run) instead of the sum (about 78 seconds).

**Q23. How long does a full run take?**
In the sample screenshot, the visible steps add up to roughly 100 seconds. This does not include the extra forecast, tag governance, and root-cause agents or the planning step, and it varies with data size and model speed.

**Q24. How is data passed between steps?**
As in-memory Python dictionaries and lists that have the same shape as JSON. No file or database sits between steps. Only the final report is written to disk.

**Q25. If the process stops halfway, is progress lost?**
Yes for the run in progress, because there are no checkpoints. Completed runs and decisions are stored, as described in Q26.

**Q26. What is stored permanently?**
The report files in `outputs/`, every chat answer and pipeline run in Postgres (`agent_responses`), and every raise, stage, approval, decline, and rollback in Postgres (`decision_log`). Redis is only a temporary cache.

**Q27. How does the system avoid sending too much data to the AI?**
Code condenses the data first. Examples: three sample rows per table for normalisation, the top 15 series and latest 60 days for anomaly detection, and 5 to 10 rows per summary table for agents.

**Q28. Can I watch progress live?**
Yes. The web server streams step start and completion events to the browser using server-sent events, which is what produces the timed step display.

---

## Section 5: Recommendations and actions

**Q29. Does every recommendation become an AWS change?**
No. Most recommendations become manual cards that are only shown and acknowledged. The only action that can execute against AWS is the deterministic swap between `nano` and `micro` instance sizes, found by scanning the live fleet.

**Q30. Why is the executable scope so narrow?**
It is a deliberate safety choice. Rules in the planner state that the exposed write path only supports changing an instance type. A vague AI sentence about stopping or tagging has nothing safe to run against.

**Q31. How does it decide which operation to run?**
For the executable swap, the card title is generated by code and names two instance types, which the executor treats as a resize. For other wording, the executor uses keywords such as resize, stop, terminate, and tag. Please describe this keyword logic honestly. It is a heuristic.

**Q32. How is the risk level decided?**
Deterministically: an instance tagged `Env=prod` is high risk. Otherwise, resize, stop, or terminate is medium. Anything else is low. An AI call then writes a one-line explanation, with a static reason as a fallback.

**Q33. Correction to the earlier diagram: is the sample card low risk?**
No. In the code, any resize on a non-production instance is labelled medium risk. The action's impact label is low, but its risk level is medium. Please correct step 6 of the end-to-end diagram before presenting.

**Q34. How are savings estimated?**
From a built-in hourly price table (`pricing.HOURLY_PRICE`) using the current and target instance types. An upsize can show a negative saving, which is intentional so that the reviewer sees it honestly.

**Q35. What if the same recommendation appears in the next run?**
Executable cards use an identifier based on the target instance, so a later run refreshes the same card and a past decision survives. Manual cards are replaced by the newest run.

---

## Section 6: The approval workflow

**Q36. Walk me through the approval steps.**
Pending, then the employee raises it (pending_re), then the RE team stages approve or decline, then the RE team commits the whole batch. Only the commit can reach AWS.

**Q37. Why a separate commit step after approval?**
It prevents partial reviews. Commit is refused while any request is undecided, so the RE team triages the entire queue before anything changes.

**Q38. Can an approval be undone before commit?**
Yes. A staged decision can be unstaged and returns to the queue. An employee can also withdraw a request before a decision.

**Q39. What if a high-risk action is approved?**
Staging an approval on a high-risk action is blocked unless the reviewer explicitly passes an override, and the override is recorded in the log.

**Q40. Can an employee approve their own request?**
Only if the RE token is not configured. When `RE_TEAM_TOKEN` is set, staging, committing, and rollback require a matching `X-RE-Token` header, which an employee session does not have. The code comment states that this is a shared secret, not real authentication. A production deployment needs SSO or IAM-based role checks.

**Q41. Is there a single-click path that bypasses the two stages?**
Yes, and you should disclose it. The code documents a Teams "Apply" flow that moves a card from pending to applied in one step. Please confirm whether it is enabled in your demonstration, and consider disabling it for production. **[Verify]**

**Q42. Is there an audit trail?**
Yes. Each transition writes an entry with time, action, decision, actor, estimated savings, and a note to the decision log in Postgres.

---

## Section 7: Changing real AWS and safety

**Q43. Can it change my AWS account right now?**
By default, no. `ACTION_BACKEND` defaults to `csv`, and `AWS_ALLOW_REAL_WRITES` defaults to off, which makes every write a logged dry run. **[Verify]** Confirm both values in your `.env` file before the demonstration.

**Q44. What safety checks apply before a real write?**
The real-writes switch must be on, the region must be in the allowed list, the instance must carry the required tag if one is configured, and the target instance type must be in the allowed list. Any failure blocks the call.

**Q45. What exactly happens during a resize?**
The instance is stopped, the code waits until it has stopped, the type is changed, and the instance is started again. The instance is unavailable for that period.

**Q46. Are all instance types resizable this way?**
No. The type change requires a stopped instance that is backed by EBS, which is an AWS rule. **[Verify]** The code does not check for instance-store-backed instances in the parts I reviewed.

**Q47. What about instances in an Auto Scaling group or behind a load balancer?**
Not handled specially in the code I reviewed. Risk is based on the `Env=prod` tag only. Please treat this as a known limitation.

**Q48. How does rollback work?**
Before any change, the instance type, state, and tags are saved to `outputs/applied/<batch_id>/rollback.json`. Rollback restores type for resize, restarts for stop, and restores tags. Terminate cannot be undone.

**Q49. What happens if one action in a batch fails?**
That action stays in the approved-but-staged state so it can be retried. The other actions in the batch continue. A partial failure across several instances is also treated as a failure and left for retry.

**Q50. Can I demonstrate safely?**
Yes. Keep real writes off and show the dry-run message that names the exact boto3 call that would have run. This proves the flow without changing anything.

---

## Section 8: Security and privacy

**Q51. What is sent to the AI provider?**
Aggregated and sampled cost data (dates, service or region names, instance types, tag values, and costs). The risk step also sends the recommendation title, instance identifiers, and instance tags. The provider by default is OpenRouter. Please confirm that this is acceptable under your organisation's data policy. **[Verify]**

**Q52. Is a free model acceptable for a client's data?**
This is a policy question that your security team should answer. Free models may have different retention and logging terms. Please do not send confidential client data to a provider that has not been approved.

**Q53. How are secrets handled?**
AWS credentials, the OpenRouter key, the RE token, and database strings belong in the git-ignored `.env` file. The default Postgres credentials are for local development only and must be changed elsewhere.

**Q54. What AWS permissions are needed?**
Read access to Cost Explorer, EC2 describe, and CloudWatch for analysis. Write permissions such as stop, start, and modify instance attribute are needed only if real writes are enabled. Please apply least privilege. **[Verify]** Confirm the exact policy you use.

**Q55. Is the web server ready for production?**
No. It appears to use Python's built-in HTTP server, and the RE gate is a shared secret. It is suitable for a demonstration or a controlled internal pilot. Production would need proper authentication, HTTPS, and hardening.

---

## Section 9: Reliability and scale

**Q56. What if the AI provider is down or rate-limited?**
A second API key is supported as a fallback, and each step has a non-AI fallback. The report quality degrades, but the run finishes. Free models are more likely to be rate limited.

**Q57. What if Postgres or Redis is down?**
Both are best effort. The pipeline and chat continue, and only persistence or caching is lost, with a warning logged.

**Q58. How does it scale to a very large account?**
The prompts cap the data volume, so AI cost does not grow with account size. Cost Explorer calls and row processing still grow with the data. There is no distributed processing.

**Q59. Are concurrent users safe?**
Pending actions are kept in a JSON file store. I have not verified how it behaves under simultaneous writes. Please avoid claiming concurrency safety. **[Verify]**

**Q60. How is it tested?**
There is a `tests/` folder, for example `test_capabilities.py`. Please run the suite before the demonstration and report the real result. **[Verify]**

---

## Section 10: Cost of running the tool

**Q61. What does a run cost?**
Cost Explorer API requests are charged by AWS per request, and a run makes about four to five. **[Verify]** Check the current AWS price. AI cost depends on the model, and the default is a free tier.

**Q62. Could the tool itself increase the AWS bill?**
Slightly, through API calls. This is small compared with potential savings, but it should be stated.

---

## Section 11: Extensibility and limitations

**Q63. Can it support other services such as RDS, EBS, or S3?**
Analysis already covers any service present in the cost data. Execution supports only EC2 instance operations and a budget alert. Adding another service needs a new executor method, a guard, and a rollback entry.

**Q64. Can an AI assistant use this system?**
Yes. An MCP server exposes read tools and the approval workflow as tools, so an assistant such as Claude can drive the same gated process. Every write still passes the two-stage gate.

**Q65. What is the chat widget?**
A popup that answers questions about the latest report, with guardrails, Redis caching, and Postgres logging.

**Q66. What are the main limitations?**
Narrow execution scope, a keyword-based operation choice for non-swap wording, a shared-secret role gate, no multi-account support, no checkpointing, and estimated rather than actual savings.

**Q67. What would you build next?**
Suggested answer: real authentication and roles, stricter separation of the Teams one-click path, Auto Scaling and load-balancer awareness, multi-account support, run checkpoints, and measured (not estimated) savings.

---

## Section 12: Difficult questions to prepare for

**Q68. "Why should I trust an AI near my production account?"**
The AI never executes anything. Executable actions come from code that reads the live fleet, humans approve them in two stages, writes are off by default, several guardrails apply, and a rollback snapshot is taken first.

**Q69. "What is the worst thing that could happen?"**
An approved resize could interrupt a service for a few minutes, or a terminate action, if ever enabled, could not be undone. This is why writes are off by default, production is flagged high risk, and the executable scope is narrow.

**Q70. "What if the estimated savings are wrong?"**
They come from a fixed price table, so they can differ from the real bill, for example with reserved instances or savings plans. Present them as estimates and verify with actual billing after the change.

**Q71. "Show me it failing gracefully."**
Suggestion: run with a missing tag or an invalid AI key and show that the report still completes using fallbacks. Test this in advance. **[Verify]**

**Q72. "Who is accountable when a change causes an outage?"**
The decision log records who raised, staged, and committed the change and when. Accountability rules are an organisational decision, and this document does not give legal advice.

---

## Pre-demo checklist

1. Confirm `ACTION_BACKEND` and `AWS_ALLOW_REAL_WRITES` in `.env`.
2. Confirm which LLM model and provider are configured, and that they are approved for the data used.
3. Use non-confidential or approved data in the demonstration.
4. Correct the risk label on step 6 of the end-to-end diagram (see Q33).
5. Decide whether the Teams one-click Apply path is enabled (see Q41).
6. Run the tests and one full dry run beforehand.
7. Review every output before sharing it outside the team.

---
name: langgraph-agent-architect
description: Audit, harden, and build production-grade LangGraph agent systems (StateGraph, checkpointers, conditional routing, multi-agent supervisors, human-in-the-loop, streaming). Use when asked to review, audit, harden, or build a LangGraph-based agent, or before writing any LangGraph agent code in a repo that already has one.
---

# LangGraph Agent Architect

Goal: turn a repo into a production-grade LangGraph agent system. Never jump straight to writing agent code. Always run the three phases below, in order: **Audit → Fix → Build**. Each phase produces a short written record (a todo list or a findings doc) before code changes are made, so the user can see what was found and why a change was made.

## Phase 1 — Read and audit the whole repo

Before touching anything, build a complete picture:

- Map the repo: list all directories and files relevant to agents, LLM calls, tools, prompts, and orchestration (search for `langgraph`, `langchain`, `StateGraph`, `create_react_agent`, `.invoke(`, `.stream(`, prompt template files, tool/function definitions, config/env files).
- Read every file that defines agent behavior — graph/state definitions, nodes, edges, tool schemas, system prompts, retry/error-handling code, and any existing tests.
- Trace the actual execution path end to end (entry point → graph compile → invoke → nodes → tools → output) rather than assuming from file names.
- Note the framework version (`langgraph`, `langchain-core` versions) since APIs (e.g. `StateGraph`, checkpointers, `interrupt`) have changed across versions — check the installed version before assuming an API shape.
- Produce a short inventory: what agents/graphs exist, what LLM/tool calls they make, what state they carry, and what's missing (no tests, no error handling, etc.).

## Phase 2 — Find and fix loopholes

Go through this checklist against what you read in Phase 1. For each hit, record: file, line, the concrete failure scenario, and severity — then fix it (or flag it for the user if the fix requires a product decision, e.g. changing external behavior).

**Control flow & robustness**

- Unbounded loops / no recursion limit on the graph (missing or too-high `recursion_limit`) — can spin forever or blow the budget.
- No fallback/retry around LLM or tool calls (a transient API error crashes the whole run).
- No timeout on tool calls that hit the network or external services.
- Conditional edges with no default/else branch — a graph can dead-end silently.
- State mutated in place without going through the reducer contract, causing race conditions when nodes run in parallel.

**Tool & prompt safety**

- Tools that execute shell commands, file writes, or code without input validation or sandboxing.
- Tool docstrings/schemas vague enough that the model can call them with malformed or dangerous arguments.
- System prompts that concatenate untrusted external content (web pages, tool output, user files) without clearly marking it as data, not instructions — a prompt-injection opening.
- Secrets (API keys, tokens) hardcoded in source instead of environment variables / secret managers.

**State & memory**

- No checkpointer configured — a crash mid-run loses all progress and can't resume.
- Long-term memory (if any) never pruned/summarized — context grows unbounded and costs balloon.
- Sensitive data (PII, credentials) written into persisted state or checkpoints without redaction.

**Multi-agent specific**

- No supervisor/router validation — a sub-agent's output is passed to the next node without checking it matches the expected schema.
- Agents that can invoke each other in a cycle with no depth/iteration cap.
- Shared state schema that lets one agent silently overwrite another's fields (missing namespacing).

**Observability & correctness**

- No structured logging/tracing (e.g. LangSmith or equivalent) — failures are undebuggable in production.
- No tests covering the graph's branches (happy path only, or no tests at all).
- Streaming/async paths untested — deadlocks or dropped events under concurrency.

Fix issues directly when the fix is unambiguous (add retries, add recursion limits, add input validation, move secrets to env vars). When a fix changes behavior the user might not want (e.g. rejecting certain tool inputs, changing a timeout that affects UX), list it as a recommendation instead of silently changing it.

## Phase 3 — Build with advanced LangGraph functionality

Once the repo is clean, implement or upgrade the agent using patterns appropriate to the task — don't reach for all of them by default, pick what the use case needs:

- `StateGraph` with typed state (`TypedDict` or Pydantic model) and explicit reducers for any field multiple nodes can write to.
- Conditional routing (`add_conditional_edges`) for branching logic instead of hardcoded linear chains.
- Multi-agent orchestration: supervisor pattern (a router node dispatches to specialist sub-agents) or hierarchical teams for complex tasks; keep each sub-agent's responsibility narrow.
- Persistence: a checkpointer (e.g. `MemorySaver` for dev, a durable store like Postgres/SQLite for production) so runs survive crashes and support multi-turn threads via `thread_id`.
- Human-in-the-loop: `interrupt()` at decision points that need approval (destructive actions, low-confidence outputs) rather than letting the agent act unsupervised.
- Robust tool calling: input validation on every tool, timeouts, retry/backoff for transient failures, and clear error messages fed back to the model so it can self-correct.
- Streaming: expose intermediate steps (`stream_mode="updates"` or `"messages"`) when the consumer is interactive, so users see progress rather than waiting on a single blocking call.
- Memory: short-term via the checkpointer/thread state; long-term via a separate store, summarized/pruned rather than appended forever.
- Structured output: validate node outputs against a schema (Pydantic) before passing them downstream, so malformed model output fails fast instead of propagating.

## Verification (mandatory before calling it done)

- Run the project's existing test suite; if none exists for the graph, add at least one test per conditional branch and one for the error/retry path.
- Run a lint/type-check pass if the repo has one configured.
- Manually trace at least one full run (happy path) and one forced-failure run (bad tool input, simulated API error) to confirm the fixes in Phase 2 actually hold.
- Summarize for the user: what was found (loopholes, by severity), what was fixed automatically, what needs their decision, and what was added in the Build phase.

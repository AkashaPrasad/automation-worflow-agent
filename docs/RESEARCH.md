# Research: why workflow agents fail, and what Adjutant does about it

Research was carried out on 2026-09-29 and 2026-09-30 using web search and primary sources (papers, engineering blogs, incident databases, GitHub). Some claims come from secondary sources; these are marked *(secondary)*.

## 1. Evidence: how autonomous workflow agents fail in production

| Failure | Evidence | Adjutant mechanism |
|---|---|---|
| **Long-horizon unreliability** | METR's 80% time horizon is several times shorter than its 50% horizon ([metr.org/time-horizons](https://metr.org/time-horizons/)). TheAgentCompany: the best agent completes 30% of tasks ([arXiv 2412.14161](https://arxiv.org/abs/2412.14161)). tau-bench pass^8 is about 25% vs a higher pass^1, so consistency is the real problem ([Sierra](https://sierra.ai/blog/benchmarking-ai-agents)). | Hierarchical plan tree as data. Bounded sub-goals, each with checkable success criteria. Subtree re-planning instead of restarting. |
| **False "done"** | MAST (NeurIPS 2025, 1,642 traces): about 24% of failures are verification failures (incorrect, missing, or premature termination) ([arXiv 2503.13657](https://arxiv.org/html/2503.13657v3)). Anthropic observed agents that "declare the job done" ([Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)). | **Proof-of-done.** Jev checks each goal's success criteria against observed tool outputs. The planner never grades its own work. |
| **Irreversible actions taken wrongly** | OpenClaw inbox incident (Feb 2026): context compaction dropped "confirm before acting", and stop messages did not halt deletions ([SF Standard](https://sfstandard.com/2026/02/25/openclaw-goes-rogue/)). Replit deleted a production DB during a freeze ([AIID #1152](https://incidentdatabase.ai/cite/1152)). An assistant sent email without checking ([AIID #1674](https://incidentdatabase.ai/cite/1674)). | **Policy outside the model.** Gate verdicts are computed in code. Approvals are bound to an **args hash**. An out-of-band **pause** (kill switch) is checked before every dispatch. |
| **Approval fatigue** | Users approve 93% of permission prompts ([Anthropic, auto mode](https://www.anthropic.com/engineering/claude-code-auto-mode)). "Approval fatigue" is named explicitly ([Anthropic, sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing)). n8n shipped an approval-bypass advisory (Sep 2026). | **Shadow run → one plan-diff review.** Only items that are risky under calibrated Jev signals ask. Low-risk reversible writes run automatically. |
| **Duplicate side effects on retry** | LangGraph re-runs the whole node on resume ([docs](https://docs.langchain.com/oss/python/langgraph/interrupts)). DBOS and Inngest are at-least-once and say "steps should be idempotent" ([DBOS](https://docs.dbos.dev/architecture), [Inngest](https://www.inngest.com/docs/learn/how-functions-are-executed)). Gmail, Slack and Notion writes have no native idempotency keys. | **Effect ledger = transactional outbox.** Intent is recorded before the call. Each call has an idempotency key per (run, node, args). Reconciliation reads back the world (for example `X-Adjutant-Key` in Sent) after a crash. |
| **Prompt injection via content** | EchoLeak (M365 Copilot, CVE-2025-32711) *(secondary)*. The "lethal trifecta" (private data + untrusted content + outbound channel) *(secondary)*. CaMeL separates control flow from untrusted data ([arXiv 2503.18813](https://arxiv.org/abs/2503.18813)). Adaptive attacks beat most classifier defences. | **Provenance/taint tracking (CaMeL-style) plus calibrated checks.** Untrusted outputs taint every downstream template and LLM step. A tainted write is never AUTO in balanced mode. Jev flags actions that follow instructions found only in untrusted content. |
| **Cost blowups and loops** | Step repetition is MAST's top failure (15.7%). Community reports of $50/day from resending full context ([HN](https://news.ycombinator.com/item?id=46864515)). | **Per-run budget** (LLM calls, tool calls, $, re-plans). **Loop detection** on repeated (tool, args hash). System-1 judgments cost about $0.04 per million tokens. |
| **Can't debug** | Destructive commands went unlogged ([claude-code #10077](https://github.com/anthropics/claude-code/issues/10077)). | Event-sourced run log with agent lanes. Every LLM and judgment call records model, tokens, latency and cost. An AG-UI-compatible stream is exposed. |

## 2. Open-source landscape: what we reused and why

| Layer | Options evaluated | Choice |
|---|---|---|
| Orchestration and durability | LangGraph (static compiled graph; node re-runs on resume), Temporal (cluster), DBOS (Postgres), Restate (BSL), Inngest (SSPL server), Hatchet, Pydantic AI, OpenAI Agents SDK, Google ADK, CrewAI | **Custom loop** (the PRD's preferred option). The plan tree must be *data* that changes at runtime, and none of these ships a dynamic hierarchical plan with per-node effects, gates and post-conditions. Durability is achieved with an event-sourced SQLite store plus the ledger. |
| Integrations | Composio (hosted platform; MIT SDKs), Arcade (hosted engine), Nango (Elastic License), Pipedream (hosted), ACI.dev and Klavis (stale since mid-2026), MCP servers | **Official Google, Notion, Slack and Fireflies SDKs** behind one Tool protocol, plus a **sandbox workspace** for zero-OAuth demos, plus an **MCP client** (official `mcp` SDK) for everything else. None of the brokers documents dry-run, idempotency or undo, so that layer is ours. |
| MCP tool annotations | `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` are untrusted hints. No framework we found gates on them at runtime. | **The annotations feed the gate.** When they are missing or contradictory, Jev infers the effect class, and the more conservative answer wins. |
| Decision model | Jev 1.13 (TypeSafe, hosted, calibrated typed judgments); Laya (Convai, Apache-2.0, 421M ModernBERT, 512-token context, needs recalibration) | **Jev as primary, Laya as open-weight fallback, then a conservative default** (route to a human). |
| Planner and writer | — | **Muse Spark 1.3 Contributor** (Meta Model API, OpenAI-compatible, 1M context, reasoning effort control, JSON-schema output). |
| Memory | Mem0, Graphiti/Zep, LangMem, Cognee. Benchmarks are disputed, and long-context baselines often match them ([AMA-Bench](https://arxiv.org/html/2602.22769)). | **Simple store plus Jev relevance reranking**, and **playbooks** (plans from successful runs reused as examples). |
| Observability | Langfuse (MIT core), Phoenix (ELv2), OpenLLMetry, Laminar | Built-in trace timeline. The event model maps to OTel/AG-UI for export. |
| UI protocol and rendering | AG-UI (MIT; STATE_SNAPSHOT/DELTA, no plan schema), CopilotKit (needs a Node runtime), assistant-ui (chat-centric), React Flow + dagre | **Native SSE plus an AG-UI adapter endpoint.** The plan tree is rendered with **@xyflow/react + dagre**. |

## 3. Gaps Adjutant targets (rated unsolved in open source)

1. An exactly-once-in-effect layer for SaaS writes (outbox + reconciliation + compensation catalogue).
2. Policy enforcement outside the model's context: args-bound approvals, batched and risk-tiered review, a real kill switch.
3. Verified completion: declarative post-conditions checked against real state.
4. Provenance-based prompt-injection defence for workspace agents.
5. A dynamic plan tree as durable data, with partial re-planning and a live UI.
6. Budget-aware execution with loop detection.

## 4. Regulatory and operational notes

- Gmail restricted scopes (`gmail.readonly`, `gmail.modify`) need a Google CASA security assessment (several weeks, repeated yearly) ([Google](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)). The sandbox workspace keeps demos independent of that.
- The Muse **Contributor** tier lets Meta train on prompts and completions. Production deployments handling real mail should switch `MODEL_NAME` to `muse-spark-1.3` (one env var).
- Fireflies API rate limits are 50 requests/day on Free, so results are cached.

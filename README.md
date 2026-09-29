# Adjutant: a Chief-of-Staff agent with calibrated autonomy

**Muse plans. Jev judges. Code decides.**

Adjutant takes a high-level request ("summarize the Northwind QBR, put it in Notion, email the attendees, book a follow-up") and turns it into a **hierarchical plan tree**. It then:

1. **Rehearses** the plan in a **shadow run**: reads are real, writes are simulated.
2. **Shows one plan diff.** Only the risky changes wait for you.
3. **Commits** through an idempotent **effect ledger**.
4. **Verifies** the result before it says it is done.

- Live app: **https://aiml.spacesdrive.cc**
- API: **https://api.aiml.spacesdrive.cc/api/health**

## Why

Today's workflow agents fail in predictable ways (evidence in [docs/RESEARCH.md](docs/RESEARCH.md)):

- They take irreversible actions wrongly. In the OpenClaw inbox incident, a "confirm first" rule was lost to context compaction.
- Users rubber-stamp 93% of per-action approval prompts.
- They claim "done" without checking; MAST attributes about 24% of multi-agent failures to verification.
- Retries duplicate side effects, because durable engines only give at-least-once delivery.
- Instructions hidden in an email can hijack them (prompt injection).

## What is new

| Mechanism | What it does |
|---|---|
| **System 1 / System 2 split** | Muse Spark 1.3 Contributor (Meta Model API) plans and writes. Jev 1.13 (TypeSafe) returns calibrated, typed judgments, with Laya as the open-weight fallback. Policy thresholds live in code, not prompts. |
| **Hierarchical plan tree as data** | Goals, sub-goals and tool actions with explicit dependencies. Failing subtrees are re-planned, and completed effects are kept. |
| **Shadow run → one plan diff** | Reads run for real and writes are simulated. You review one diff instead of N prompts. Low-risk reversible writes run automatically. |
| **Per-argument taint (CaMeL-style)** | Untrusted email, doc or transcript content may fill a body, but never choose a recipient, channel or target, unless the value matches a trusted contact. |
| **Effect ledger** | A transactional outbox: intent is recorded before each call, with an idempotency key per (run, step, args) and reconciliation after crashes. Rollback compensates reversible effects. |
| **Args-bound approvals and a kill switch** | An approval applies only to the exact arguments you saw. Pause is enforced by the scheduler, not by a chat message. |
| **Proof-of-done** | Jev checks each goal's success criteria against observed tool outputs. |
| **Failure recovery** | Jev classifies the failure. Code then retries, repairs arguments, switches tool, re-plans, or asks you. |
| **Budgets and loop detection** | Per-run caps on LLM calls, tool calls, dollars and re-plans. |

## Architecture

```
React SPA (Cloudflare Pages) ──HTTPS/SSE──▶ Caddy ─▶ FastAPI (AWS EC2)
                                                  │
                     ┌────────────────────────────┼─────────────────────────────┐
                     ▼                            ▼                             ▼
            Orchestrator (engine)       Judge (Jev → Laya → fail-safe)   Tool layer
  intent → plan → shadow → gate →       gate · verify · scan · recover    sandbox workspace
  approve → commit → verify → replan                                     Google · Notion · Slack
                     │                                                    Fireflies · MCP · web
                     ▼
        SQLite: runs · events · approvals · effect ledger · memory
```

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/core` | Shared contracts (models, interfaces, tracing) |
| `backend/app/engine` | Planner, DAG scheduler, shadow/commit, ledger, gate, recovery, verification |
| `backend/app/judgment` | Jev question batteries, calibrated policy, Laya fallback |
| `backend/app/tools` | Sandbox "Acme Robotics" workspace, live connectors, MCP adapter |
| `backend/app/store`, `backend/app/api` | SQLite event store, SSE, AG-UI endpoint, REST API |
| `frontend/` | React 19 + Vite + Tailwind 4 + @xyflow/react run console |
| `infra/` | EC2 + Caddy + systemd provisioning, Cloudflare DNS and Pages deploy |
| `e2e/` | Playwright suite (smoke, API, workflows, controls, mobile) |
| `docs/` | Spec, research, judgment design and results, integrations |

## Run locally

```bash
cp .env.example .env            # add MODEL_API_KEY (Meta Model API) and TYPESAFE_API_KEY
cd backend && uv sync && uv run uvicorn app.main:app --reload
cd frontend && npm install && npm run dev        # or: npm run dev:mock (no backend)
cd backend && uv run pytest -m "not live"        # offline suite; -m live hits real Jev
cd e2e && npx playwright test                    # E2E_BASE_URL / E2E_API_BASE to target prod
```

## Deploy

Run `infra/deploy-all.sh`. It provisions EC2, points DNS at it, deploys the backend and deploys Pages. See [infra/README.md](infra/README.md).

## Notes

- **Muse Contributor data use.** The Contributor tier lets Meta train on prompts and completions. For real mailboxes, set `MODEL_NAME=muse-spark-1.3`.
- **Test status of the live connectors.** They are tested against mocked APIs only. Gmail restricted scopes also require Google CASA verification. The sandbox workspace makes the demos independent of both.

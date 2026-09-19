# BUILD BRIEF — AgentCore Observability Demo (travel-preference assistant)

You are the single-writer engineering worker (Claude Code, Bedrock model `global.anthropic.claude-fable-5`).
Deadline pressure: customer console walkthrough Monday 2026-09-21. Work continuously until acceptance passes.

## Mission
Build, deploy, and thoroughly test a NEW demo agent on Amazon Bedrock AgentCore with real:
1. **AgentCore Runtime** (deployed container, real `InvokeAgentRuntime` calls)
2. **AgentCore Memory** (short-term events + async long-term user-preference extraction)
3. **Observability** (OTel traces w/ model+tool spans, Runtime application logs, Memory service delivery logs, metrics, safe slow/error injection)
4. **Simple local UX** (loopback web chat, SigV4 in backend only)

## AWS environment (VERIFIED LIVE 2026-09-20 09:20 AEST by coordinator)
- Profile: `platform-dev-takeover` — **set `AWS_PROFILE=platform-dev-takeover` (or `--profile`) on EVERY AWS call. NEVER use `default` or `agentic-platform-prod` profiles (different accounts).**
- Region: `us-west-2` only.
- Role: assumed-role/AgenticPlatform-Dev-CrossAccountAdmin — admin, but stay minimal-footprint.
- CloudWatch Transaction Search: **ACTIVE** (`aws xray get-trace-segment-destination` → CloudWatchLogs/ACTIVE). Log group `aws/spans` exists (30-day retention). DO NOT change account-level X-Ray/Transaction-Search settings.
- Bedrock model access verified live: `global.anthropic.claude-haiku-4-5-20251001-v1:0` and `global.anthropic.claude-fable-5` both respond to `converse`.
- Account already runs many AgentCore runtimes/memories (plato_*, deep_research_*, ecommerce workshop...). **Do not touch, modify, or delete ANY existing resource.** Existing memory log deliveries (e.g. `deep_research_agent_mem-*-logs-source` APPLICATION_LOGS + TRACES) are reference patterns you may READ.
- Docker 29.2.1 available locally (Apple Silicon arm64 — native linux/arm64 builds, which is what AgentCore Runtime requires).
- Never echo the full account ID into any file under `build/` or into docs meant for sharing. Full IDs allowed only in `work/.../build/local/` evidence files.

## Naming & isolation (hard requirement)
Prefix everything `obsdemo`: runtime `obsdemo_travel_agent`, memory `obsdemo_travel_memory`, ECR repo `obsdemo-travel-agent`, IAM role `obsdemo-agentcore-runtime-role`, delivery sources/destinations `obsdemo-*`. New log groups: set retention 7 days. Tag resources where supported: `project=obsdemo`, `owner=melanie-demo`, `env=dev-demo`.

## Directories
- Code (this repo, branch `demo/agentcore-observability`, commit as you go, local-only, no push):
  `/Users/peiyaoli/.openclaw/workspace/projects/agentcore-observability-demo/`
- Outputs/evidence: `/Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/`
  - `build/local/` = full-identifier evidence (never shared)
  - `build/share/` = sanitized (no account IDs, no creds, no raw deployment config)
- Do NOT edit any other workspace files. Prior research docs at `work/agentcore-observability-demo/*.md` are read-only context (STATUS/COMPARISON/EVIDENCE/RUNBOOK) — coordinator refreshes them later.

## Research-before-code (mandatory)
Before implementing each API surface, verify against official docs https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/ (web fetch) AND the installed CLI/SDK (`aws bedrock-agentcore-control help`, `aws bedrock-agentcore help`, `python3 -c "import boto3; ..."` shape checks). The local `~/.claude/skills/bedrock-agentcore` skill may be stale — trust live docs + installed SDK model over it. Record doc URLs you relied on in `build/share/SOURCES.md`.

## Product design (fixed — do not redesign)
**Synthetic travel-preference assistant.** English UI/customer-facing text.
- Agent: Python container using `bedrock_agentcore` SDK (`BedrockAgentCoreApp`) + Strands Agents (`strands-agents`) with Bedrock model `global.anthropic.claude-haiku-4-5-20251001-v1:0` (cheap, verified). If Strands proves problematic, fall back to plain boto3 converse + manual OTel spans — but try Strands first (auto gen_ai spans).
- One deterministic harmless tool `lookup_destination_info(city)`: returns canned weather/season/attraction data from a static in-code dict for ~6 fictional-safe cities (e.g. Queenstown, Kyoto, Reykjavik...). No network calls from the tool.
- Demo scenario injection (safe, explicit, demo-only): request payload field `scenario`:
  - `normal` (default)
  - `slow`: tool sleeps 3s before returning (identifiable latency in spans/metrics)
  - `error`: tool raises a deterministic `DemoToolError` once; agent handles/apologizes; produces identifiable error telemetry. No real outage, no infra faults.
- Memory wiring inside the agent (runtime execution role creds):
  - STM: `create_event` per turn (user + assistant messages) with `actorId` + `sessionId` from payload; conversation context within a session from `list_events`.
  - LTM: memory resource created with built-in **user preference strategy** (`userPreferenceMemoryConfiguration` / whatever the current SDK shape is — verify), namespace like `/preferences/{actorId}`. On each new session start, `retrieve_memory_records` for the actor's namespace and inject as context; agent response should mention remembered preferences naturally. Response JSON must include `memory_provenance`: which LTM records (id + snippet) were retrieved, count of STM events used — real values only, never invented.
- Response JSON also includes: `session_id`, `actor_id`, `scenario`, and trace correlation info if genuinely available from OTel context (`trace_id` from current span context is fine).

## Infrastructure steps
1. IAM execution role `obsdemo-agentcore-runtime-role`: trust `bedrock-agentcore.amazonaws.com` (verify exact documented trust policy incl. SourceAccount/SourceArn conditions in devguide "runtime permissions" page). Inline policy minimal: ECR pull on the one repo, CloudWatch Logs create/put scoped to `/aws/bedrock-agentcore/runtimes/*`, X-Ray put, cloudwatch PutMetricData scoped as documented, `bedrock:InvokeModel*` (haiku + its regional inference profile ARNs), `bedrock-agentcore` memory data-plane actions scoped to the new memory ARN. Get the documented baseline from the devguide, then scope down.
2. ECR repo `obsdemo-travel-agent`; docker buildx linux/arm64; push.
3. Memory: `create_memory` with STM (event expiry e.g. 7 days) + user-preference LTM strategy. Wait ACTIVE.
4. Memory service logs: vended log delivery — `put-delivery-source` (APPLICATION_LOGS and TRACES for the memory resource), `put-delivery-destination` to new log group e.g. `/aws/vendedlogs/bedrock-agentcore/obsdemo-travel-memory`, `create-delivery` linking them. Mirror the existing account pattern (read `aws logs describe-deliveries` / `describe-delivery-sources` / `describe-delivery-destinations` for the deep_research examples). These are NEW obsdemo-named sources/destinations only — never modify existing ones. This memory-delivery step is explicitly authorized.
5. Runtime: `create_agent_runtime` with container URI, execution role, network PUBLIC, env vars for memory id etc. Container must include `aws-opentelemetry-distro` and start via `opentelemetry-instrument` per devguide observability page so Runtime emits OTel to CloudWatch/X-Ray (Transaction Search already on). Wait READY.
6. Deployment scripting: idempotent Python (boto3) scripts under `infra/` — `deploy.py`, `teardown.py` (teardown NOT executed; just written and documented).

## Local UX (build it)
`ux/` FastAPI (or Flask) app bound to **127.0.0.1** only, e.g. port 8931:
- Backend holds AWS session (profile `platform-dev-takeover`) and calls `invoke_agent_runtime` (SigV4). Browser never sees AWS creds; no cloud ingress created.
- Static single-page chat UI (clean, simple, professional English): chat transcript, **New Session** button, **Actor** switch (two synthetic actors, e.g. "demo-actor-ava" / "demo-actor-ben" — UI labels "Traveler A (Ava)" / "Traveler B (Ben)"), **Scenario** selector (Normal / Slow tool / Tool error), and a collapsible "Telemetry" panel per response showing session_id, actor_id, runtime session id used, trace_id (if real), memory provenance (retrieved LTM records + STM event count). Never invent token counts/costs/spans in the UI — only show fields actually returned.
- `README.md` in `ux/` with exact run command.

## Acceptance tests (write `tests/run_acceptance.py`, output JSON + human log to `build/local/acceptance/`)
Each check records timestamp (UTC + AEST), region, request/response essentials, and PASS/FAIL/SKIPPED with reason. Bounded budget: ≤ 30 total runtime invocations, small prompts.
1. Real deployed runtime invocation returns valid agent answer (model actually called).
2. Multi-turn STM: turn1 states preferences ("I love hiking and hate humidity, I prefer boutique hotels"), turn2 same session asks follow-up requiring turn1 context; verify contextual answer + `list_events` shows the events.
3. Async LTM: after preference-rich session for actor A, poll `retrieve_memory_records` (max 15 polls × 20s; report elapsed). Then NEW session, same actor: agent retrieves and uses preferences; verify `memory_provenance` cites real record IDs. If extraction exceeds the bound, mark PENDING with explicit delay fallback note — do not fail the whole suite, and re-poll once more at the end.
4. Actor isolation: actor B, new session, ask "what do you know about my preferences" → must NOT contain actor A's preferences; `retrieve_memory_records` for B's namespace must not return A's records.
5. Telemetry retrieval (real, via APIs): find the trace for a specific invocation in `aws/spans` (CloudWatch Logs Insights query by trace id / session attribute), confirm model span (gen_ai attrs) + tool span; fetch Runtime log group events for same window; fetch Memory delivery log group events (extraction/retrieval activity) — label clearly which log group each came from. Don't confuse memory events/records (data) with memory service logs (telemetry).
6. Slow scenario: invoke with scenario=slow, verify measurably longer tool span/duration in telemetry. Error scenario: invoke with scenario=error, verify identifiable error in spans/logs.
7. Security: attempt unauthenticated/unsigned HTTP call to runtime endpoint (plain curl without SigV4) → expect rejection; record result. Confirm UX binds 127.0.0.1 only.
8. UX E2E: start server, `curl` the chat API path end-to-end (browser-level check left to coordinator; make sure API path works so browser test is trivial).

## Evidence & docs to produce
- `build/local/acceptance/results.json` + `run.log` (full IDs OK here)
- `build/local/evidence-manifest.json`: scenario → actorId/sessionId → runtime session → trace IDs → log groups → exact UTC time windows → Logs Insights query strings used → verification status (live-pass/pending/skipped)
- `build/share/ARCHITECTURE.md`: components + data flow + which telemetry surface shows what (sanitized)
- `build/share/CONSOLE-WALKTHROUGH.md`: exact console navigation for Monday: CloudWatch GenAI Observability page, Transaction Search, trace detail w/ spans, runtime log group, memory delivery log group, metrics, Memory console (STM events, LTM records) — mark any route you could not verify from CLI as "console route unverified"
- `build/share/SOURCES.md`: doc URLs used
- `build/share/COST-NOTES.md`: cost assumptions (runtime CPU-seconds, haiku tokens, memory events/records, logs ingestion; small numbers, clearly assumptions not a bill) + cleanup commands (teardown.py usage) NOT executed
- Update nothing in the parent work dir root; coordinator owns RUNBOOK/COMPARISON refresh.

## Rules
- Secrets: never print credentials, never put them in code/URLs/logs. Use profile only.
- No public/unauthenticated endpoints, no NAT/database/always-on compute besides the AgentCore runtime itself (serverless).
- If genuinely blocked (permission denied, service error you cannot fix), write `build/local/BLOCKER.md` with the exact command + error and continue with everything else safe.
- Commit to the local git branch at logical checkpoints with clear messages. NO push.
- Progress heartbeat: append one-line status to `build/local/PROGRESS.log` (with timestamp) at every major milestone so the coordinator can follow.
- When completely finished, write `build/local/WORKER-DONE.md` summarizing pass/fail per acceptance item, then run:
  `openclaw system event --text "Done: obsdemo build+acceptance finished, see build/local/WORKER-DONE.md" --mode now`

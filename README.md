# obsdemo — Amazon Bedrock AgentCore Observability Demo

A synthetic **travel-preference assistant** demonstrating three AgentCore
pillars end to end, with honest, verifiable telemetry:

1. **Runtime** — containerized agent (Strands Agents + ADOT auto-instrumentation)
   invoked via `InvokeAgentRuntime`
2. **Memory** — short-term conversation events (STM) + asynchronous long-term
   user-preference extraction (LTM) with per-actor namespaces
3. **Observability** — OTel model/tool spans in CloudWatch Transaction Search
   (`aws/spans`), runtime application logs, memory *service* logs via vended
   log delivery, plus safe slow/error scenario injection

Two UX modes: a local loopback chat (`ux/`) and a hosted CloudFront + Cognito
UX (`ux-cloud/`).

## Architecture

### Local mode

```
browser (127.0.0.1) ──> FastAPI ux/server.py (SigV4, profile creds)
                            │ InvokeAgentRuntime
                            ▼
                     AgentCore Runtime (container: Strands + ADOT)
                      │            │                  │
                      ▼            ▼                  ▼
              Bedrock model   AgentCore Memory   CloudWatch
              (Claude Haiku)  STM events +       aws/spans (traces)
                              LTM preference     runtime log group
                              records            memory vended logs
```

### Hosted mode

```
browser ──HTTPS──> CloudFront ──OAC──> private S3 (static shell)
   │ Cognito managed login (code + PKCE, public client)
   └──Bearer JWT──> API Gateway HTTP API (JWT authorizer on EVERY route)
                        │
                        ▼
                 Lambda obsdemo-ux-backend  (derives actor_id from JWT sub)
                  ├─ InvokeAgentRuntime ──> AgentCore Runtime (as above)
                  ├─ ListEvents / RetrieveMemoryRecords ──> Memory
                  └─ Logs Insights (allowlisted projections) ──> aws/spans,
                                                    memory vended log group
```

Key official docs:
- [Runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Configure observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [Get started with AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html)
- [Memory: create an event](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/short-term-create-event.html)
- [Memory observability data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-memory-metrics.html)

## Quick start

### Clone + install

```bash
git clone <this-repo> && cd agentcore-observability-demo
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python boto3 fastapi uvicorn
```

### Local test (offline, no AWS)

The span-redaction processor has a self-contained unit test against the OTel SDK:

```bash
uv pip install --python .venv/bin/python opentelemetry-sdk
.venv/bin/python tests/test_redaction_processor.py
```

### Local run (loopback UX)

Needs a deployed core stack (below) and its state file:

```bash
export AWS_PROFILE=<profile>
export OBSDEMO_STATE_FILE=./build/deploy-state.json
cd ux && ../.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8931
# open http://127.0.0.1:8931
```

## Full cloud deployment

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `OBSDEMO_AWS_PROFILE` | `platform-dev-takeover` | AWS profile for all infra scripts |
| `OBSDEMO_REGION` | `us-west-2` | Region |
| `OBSDEMO_STATE_FILE` | `./build/deploy-state.json` | Core-stack state (generated; keep out of git) |
| `OBSDEMO_UX_STATE_FILE` | `./build/cloud-ux-state.json` | Hosted-UX state (generated; keep out of git) |

### 1. Core stack

```bash
export AWS_PROFILE=<profile>
python infra/deploy.py --skip-runtime     # ECR repo, memory, log delivery, IAM
./infra/build_push.sh <ecr_uri> latest    # docker buildx linux/arm64 image
python infra/deploy.py                    # create runtime, wait READY
```

### 2. Hosted UX

```bash
python infra/deploy_ux.py                 # S3+CloudFront+Cognito+API+Lambda
```

See `ux-cloud/README.md` for the security model and `config.js` generation.

### 3. Auth setup (admin invite — no passwords in shells or files)

Self-signup is off. Invite a user; Cognito emails a temporary password and
forces a change at first login:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <pool-id> --username <email> \
  --user-attributes Name=email,Value=<email> Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Runtime image fails to start | ECR image must be **linux/arm64** (`docker buildx build --platform linux/arm64`) |
| Runtime stuck not-READY | Check the runtime log group for container crash loops; verify execution-role ECR pull + memory permissions |
| Trace queries return nothing | `aws/spans` ingestion lags **several minutes** — retry with backoff before concluding |
| API returns 401 with a token | Common causes: token expired (1h default), wrong client id in audience, `Bearer ` prefix missing, using the access token where the authorizer audience only matches the id token's `aud` |
| Browser CORS errors | The API allows exactly `https://<cloudfront-domain>`; opening the site via another origin (or http) will fail preflight |
| CloudFront 403 right after deploy | Distribution deployment takes 5–15 min; also confirm the bucket policy's `AWS:SourceArn` matches the distribution ARN |

### Teardown (preview-first)

Both teardown scripts **only list** what they would delete unless `--confirm`
is passed, use exact-name matching with paginated listings, and never delete
automatically:

```bash
python infra/teardown_ux.py            # preview UX-layer deletions
python infra/teardown_ux.py --confirm  # delete UX layer only
python infra/teardown.py               # preview core-stack deletions
python infra/teardown.py --confirm     # delete core stack
```

## Observability guide

### Memory *records* vs memory *service logs*

- **Records/events** are application **data** stored in Memory: STM events
  (`CreateEvent`/`ListEvents`) and LTM records (`RetrieveMemoryRecords`).
- **Service logs** are **telemetry** about Memory's async workers (extraction,
  consolidation). They are NOT enabled by default — this demo enables them via
  vended log delivery: `put-delivery-source` (APPLICATION_LOGS + TRACES on the
  memory resource) + `put-delivery-destination` + `create-delivery`.

### Log groups

| Group | Content |
|---|---|
| `aws/spans` | OTel spans (CloudWatch Transaction Search destination, account-wide) |
| `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` | Runtime application logs (stdout/stderr) |
| `/aws/vendedlogs/bedrock-agentcore/obsdemo-travel-memory` | Memory service logs (extraction/consolidation) |
| `/aws/lambda/obsdemo-ux-backend` | Hosted-UX backend logs |

### Spans and correlation

The container runs under `opentelemetry-instrument` with
`aws-opentelemetry-distro`; Strands emits `gen_ai.*` spans automatically.
Spans you should see per invocation:

- `invoke_agent Strands Agents` (`gen_ai.operation.name=invoke_agent`,
  `gen_ai.request.model=<model-id>`)
- `chat <model-id>` / `chat` — model calls with `gen_ai.request.model`,
  token usage attributes
- `execute_tool lookup_destination_info` — tool span; on scenario=error it has
  span `status.code=ERROR` and `gen_ai.tool.status=error`
- `execute_event_loop_cycle`, plus AWS SDK spans for `CreateEvent`,
  `ListEvents`, `RetrieveMemoryRecords`

Correlation: the agent stamps `obsdemo.actor_id`, `obsdemo.session_id`, and
`obsdemo.scenario` on its trace attributes; the same actor/session IDs appear
in runtime application logs and in memory service-log messages, so one
session can be followed across spans ↔ runtime logs ↔ memory logs.

### Logs Insights query recipes (copy-paste)

All spans for one trace (log group `aws/spans`):

```
fields @timestamp, name, status.code, durationNano,
       attributes.gen_ai.request.model, attributes.gen_ai.operation.name
| filter traceId = "<32-hex-trace-id>"
| sort startTimeUnixNano asc
```

Error tool spans in a window:

```
fields @timestamp, name, status.code, attributes.gen_ai.tool.status, traceId
| filter name like /execute_tool/ and status.code = "ERROR"
| sort @timestamp desc
```

Slow tool spans (> 2.5 s):

```
fields @timestamp, name, durationNano, traceId
| filter name like /lookup_destination_info/ and durationNano > 2500000000
```

All spans for one demo session:

```
fields @timestamp, name, traceId
| filter attributes.obsdemo.session_id = "<session-id>"
```

Memory extraction activity (memory vended log group):

```
fields @timestamp, @logStream, @message
| filter @message like /<actor-or-session-id>/
| sort @timestamp desc
```

### Span redaction (and why)

The ADOT Python botocore patch stamps the caller's **STS access-key ID** onto
every AWS-API span as `aws.auth.account.access_key` (plus a derived copy in
`aws.remote.resource.account.access_key`), with no configuration switch to
disable only that attribute. This demo registers a `SpanProcessor`
(`CredentialAttributeRedactor` in `agent/main.py`) that overwrites both
attributes with `REDACTED` at span start, using only the public OTel SDK API.
Key deletion isn't possible (span attributes are immutable mappings), so the
key remains present with a constant placeholder value. The hosted-UX telemetry
API additionally never projects any `aws.auth.*` attribute (defense in depth).

## Demo script

| Beat | Prompt | What to show |
|---|---|---|
| Normal | "I love hiking and hate humidity, and I prefer boutique hotels. Which of Kyoto or Queenstown suits me better?" | Answer + trace: model spans, tool span |
| STM follow-up (same session) | "Given what I just told you, should I pack for humidity?" | `stm_events_used >= 1` in the telemetry panel |
| Slow | "What's the weather like in Reykjavik?" (scenario = Slow tool) | Tool span duration > 3 s in the trace |
| Error | "What's the weather like in Banff?" (scenario = Tool error) | Graceful apology; tool span `status.code=ERROR`, `gen_ai.tool.status=error` |
| LTM recall (NEW session, same traveler, wait ~1–3 min after the preference turn) | "I'm planning my next trip. Any destination suggestions for me?" | Memory provenance cites real LTM record IDs; answer weaves in preferences |

## Caveats

- **Ingestion delay**: spans/logs land in `aws/spans` and log groups with a
  delay of up to several minutes. The UI/tests retry; the console needs patience.
- **Retention**: runtime, memory-vended, and Lambda log groups are set to
  7 days. `aws/spans` follows the account-wide Transaction Search setting.
  **LTM records persist until explicitly deleted** — they do not age out with
  STM event expiry.
- **Cost**: serverless components (runtime CPU-seconds, Haiku tokens, memory
  events/records, CloudWatch ingestion/queries, Lambda, CloudFront). Small for
  demo traffic, but not validated as a bill — no specific dollar claim is made.
- **Multi-tenancy**: hosted mode binds every actor to the Cognito `sub`
  server-side, so cross-user isolation is enforced by construction at the API.
  Memory namespacing remains an application-level convention within a single
  account/memory resource.
- **HoneyHive comparison**: see `docs/COMPARISON.md` — a docs-based capability
  matrix (not account-tested).

## Repository layout

- `agent/` — containerized agent (BedrockAgentCoreApp + Strands + redaction processor)
- `infra/` — idempotent deploy/teardown for core stack (`deploy.py`) and hosted UX (`deploy_ux.py`)
- `ux/` — local loopback FastAPI chat (see `ux/README.md`)
- `ux-cloud/` — hosted UX: static shell + Lambda backend (see `ux-cloud/README.md`)
- `tests/` — offline redaction unit test, live acceptance suites (core + cloud UX)
- `docs/` — comparison matrix and supplementary docs

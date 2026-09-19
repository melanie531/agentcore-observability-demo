# obsdemo — AgentCore Observability Demo

Synthetic travel-preference assistant on Amazon Bedrock AgentCore demonstrating
Runtime, Memory (STM + async user-preference LTM), and full observability
(OTel traces, runtime logs, memory vended delivery logs, metrics) with safe
slow/error scenario injection.

## Layout

- `agent/` — containerized agent (BedrockAgentCoreApp + Strands, ADOT instrumented)
- `infra/` — idempotent deploy/teardown (boto3) + image build script
- `ux/` — local loopback FastAPI chat UI (see `ux/README.md`)
- `tests/run_acceptance.py` — 8-part live acceptance suite

## Deploy

```bash
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python boto3 fastapi uvicorn requests
export AWS_PROFILE=platform-dev-takeover
cd infra && ../.venv/bin/python deploy.py --skip-runtime   # ECR, memory, log delivery, IAM
cd .. && ./infra/build_push.sh <ecr_uri> latest             # linux/arm64 image
cd infra && ../.venv/bin/python deploy.py                   # runtime, wait READY
```

## Test

```bash
AWS_PROFILE=platform-dev-takeover .venv/bin/python tests/run_acceptance.py
```

## Cleanup (after demo)

```bash
AWS_PROFILE=platform-dev-takeover .venv/bin/python infra/teardown.py --confirm
```

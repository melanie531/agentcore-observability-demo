"""Shared config for obsdemo infrastructure scripts."""

PROFILE = "platform-dev-takeover"
REGION = "us-west-2"

RUNTIME_NAME = "obsdemo_travel_agent"
MEMORY_NAME = "obsdemo_travel_memory"
ECR_REPO = "obsdemo-travel-agent"
ROLE_NAME = "obsdemo-agentcore-runtime-role"

MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
LTM_NAMESPACE = "/preferences/{actorId}"

MEMORY_LOG_GROUP = "/aws/vendedlogs/bedrock-agentcore/obsdemo-travel-memory"
LOG_RETENTION_DAYS = 7

TAGS = {"project": "obsdemo", "owner": "melanie-demo", "env": "dev-demo"}

STATE_FILE = (
    "/Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/"
    "build/local/deploy-state.json"
)

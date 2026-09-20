"""Shared config for obsdemo infrastructure scripts.

Env overrides: OBSDEMO_AWS_PROFILE, OBSDEMO_REGION, OBSDEMO_STATE_FILE,
OBSDEMO_UX_STATE_FILE.
"""

import os

PROFILE = os.environ.get("OBSDEMO_AWS_PROFILE", "platform-dev-takeover")
REGION = os.environ.get("OBSDEMO_REGION", "us-west-2")
# optional safety guard: if set, scripts abort unless the caller-identity
# account ends with this suffix (e.g. export OBSDEMO_ACCOUNT_SUFFIX=1234)
ACCOUNT_SUFFIX = os.environ.get("OBSDEMO_ACCOUNT_SUFFIX", "")

RUNTIME_NAME = "obsdemo_travel_agent"
MEMORY_NAME = "obsdemo_travel_memory"
ECR_REPO = "obsdemo-travel-agent"
ROLE_NAME = "obsdemo-agentcore-runtime-role"

MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
LTM_NAMESPACE = "/preferences/{actorId}"

MEMORY_LOG_GROUP = "/aws/vendedlogs/bedrock-agentcore/obsdemo-travel-memory"
LOG_RETENTION_DAYS = 7

TAGS = {"project": "obsdemo", "owner": "melanie-demo", "env": "dev-demo"}

STATE_FILE = os.environ.get("OBSDEMO_STATE_FILE", "./build/deploy-state.json")
UX_STATE_FILE = os.environ.get("OBSDEMO_UX_STATE_FILE", "./build/cloud-ux-state.json")

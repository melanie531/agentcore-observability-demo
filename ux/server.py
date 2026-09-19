"""Local loopback chat UX for the obsdemo travel agent.

Binds 127.0.0.1 only. AWS creds stay server-side (SigV4 via boto3 profile).
Run: ../.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8931
"""

import json
import os
import uuid

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

PROFILE = os.environ.get("OBSDEMO_PROFILE", "platform-dev-takeover")
REGION = "us-west-2"
STATE_FILE = os.environ.get(
    "OBSDEMO_STATE_FILE",
    "/Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/local/deploy-state.json",
)

with open(STATE_FILE) as f:
    STATE = json.load(f)
RUNTIME_ARN = STATE["runtime_arn"]

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
agentcore = session.client("bedrock-agentcore")

app = FastAPI(title="obsdemo travel assistant (local demo UX)")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ChatRequest(BaseModel):
    prompt: str
    actor_id: str
    session_id: str
    scenario: str = "normal"


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    if req.scenario not in ("normal", "slow", "error"):
        raise HTTPException(400, "scenario must be normal|slow|error")
    # AgentCore requires runtimeSessionId >= 33 chars
    runtime_session_id = f"obsdemo-{req.session_id}-{req.actor_id}".ljust(33, "x")
    payload = json.dumps({
        "prompt": req.prompt,
        "actor_id": req.actor_id,
        "session_id": req.session_id,
        "scenario": req.scenario,
    }).encode()
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=runtime_session_id,
            contentType="application/json",
            accept="application/json",
            payload=payload,
        )
        body = resp["response"].read()
        data = json.loads(body)
    except Exception as e:
        raise HTTPException(502, f"runtime invocation failed: {e}")
    data["runtime_session_id"] = runtime_session_id
    return data


@app.get("/api/health")
def health():
    return {"ok": True, "runtime": RUNTIME_ARN.split("/")[-1]}

"""obsdemo-ux-backend — Lambda behind API Gateway HTTP API (JWT-authorized).

Every route requires a Cognito JWT validated by the API Gateway authorizer
BEFORE this code runs. Identity is read ONLY from the validated claims:

    sub = event.requestContext.authorizer.jwt.claims["sub"]

Actor binding (security invariant):
    actor_id   = f"u-{sub}-{traveler}"      traveler from fixed allowlist
    session_id = f"{sub[:12]}-{label}"      label validated [a-z0-9-]{1,32}

Clients can never supply actor_id or address another sub's sessions.

Config via env vars: OBSDEMO_RUNTIME_ARN, OBSDEMO_MEMORY_ID,
OBSDEMO_MEMORY_LOG_GROUP, OBSDEMO_SPANS_LOG_GROUP (default aws/spans).
"""

import json
import os
import re
import time

import boto3

RUNTIME_ARN = os.environ["OBSDEMO_RUNTIME_ARN"]
MEMORY_ID = os.environ["OBSDEMO_MEMORY_ID"]
MEMORY_LOG_GROUP = os.environ["OBSDEMO_MEMORY_LOG_GROUP"]
SPANS_LOG_GROUP = os.environ.get("OBSDEMO_SPANS_LOG_GROUP", "aws/spans")
REGION = os.environ.get("AWS_REGION", "us-west-2")

TRAVELER_ALLOWLIST = ("ava", "blake")
SCENARIO_ALLOWLIST = ("normal", "slow", "error")
LABEL_RE = re.compile(r"^[a-z0-9-]{1,32}$")
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Only these span fields are ever returned to clients (defense in depth on
# top of the in-container span redaction). NEVER raw span JSON, NEVER aws.auth.*.
SPAN_FIELDS = [
    "name", "traceId", "spanId", "parentSpanId", "status.code",
    "durationNano", "startTimeUnixNano", "endTimeUnixNano",
    "attributes.gen_ai.request.model", "attributes.gen_ai.operation.name",
    "attributes.gen_ai.tool.status",
    "attributes.gen_ai.usage.input_tokens", "attributes.gen_ai.usage.output_tokens",
    "attributes.obsdemo.actor_id", "attributes.obsdemo.session_id",
    "attributes.obsdemo.scenario",
]
TRACE_QUERY_TEMPLATE = (
    "fields " + ", ".join(SPAN_FIELDS) +
    ' | filter traceId = "{trace_id}" | sort startTimeUnixNano asc | limit 100'
)
MAX_WINDOW_S = 3600  # bounded time window for all telemetry reads

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _bad(msg: str) -> dict:
    return _resp(400, {"error": msg})


def _identity(event: dict, params: dict, body: dict):
    """Derive (sub, traveler, actor_id) from the validated JWT claims only."""
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    sub = claims["sub"]
    traveler = str(body.get("traveler") or params.get("traveler") or "ava").lower()
    if traveler not in TRAVELER_ALLOWLIST:
        return None, None, None
    return sub, traveler, f"u-{sub}-{traveler}"


def _derive_session(sub: str, label: str):
    if not LABEL_RE.match(label or ""):
        return None, None
    session_id = f"{sub[:12]}-{label}"
    runtime_session_id = f"obsdemo-ux-{session_id}".ljust(33, "x")
    return session_id, runtime_session_id


def handle_chat(event: dict, body: dict) -> dict:
    if "actor_id" in body:
        return _bad("actor_id is server-derived and must not be supplied")
    sub, traveler, actor_id = _identity(event, {}, body)
    if actor_id is None:
        return _bad(f"traveler must be one of {list(TRAVELER_ALLOWLIST)}")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt or len(prompt) > 4000:
        return _bad("prompt must be a non-empty string (max 4000 chars)")
    scenario = str(body.get("scenario", "normal")).lower()
    if scenario not in SCENARIO_ALLOWLIST:
        return _bad(f"scenario must be one of {list(SCENARIO_ALLOWLIST)}")
    session_id, runtime_session_id = _derive_session(sub, str(body.get("session", "")))
    if session_id is None:
        return _bad("session must match [a-z0-9-]{1,32}")

    payload = json.dumps({
        "prompt": prompt, "actor_id": actor_id,
        "session_id": session_id, "scenario": scenario,
    }).encode()
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=runtime_session_id,
        contentType="application/json", accept="application/json", payload=payload,
    )
    data = json.loads(resp["response"].read())
    data["runtime_session_id"] = runtime_session_id
    data["traveler"] = traveler
    return _resp(200, data)


def handle_sessions(event: dict, params: dict) -> dict:
    sub, traveler, actor_id = _identity(event, params, {})
    if actor_id is None:
        return _bad(f"traveler must be one of {list(TRAVELER_ALLOWLIST)}")
    try:
        r = agentcore.list_sessions(memoryId=MEMORY_ID, actorId=actor_id, maxResults=50)
        sessions = [s.get("sessionId") for s in r.get("sessionSummaries", [])]
    except agentcore.exceptions.ResourceNotFoundException:
        sessions = []
    return _resp(200, {"actor_id": actor_id, "sessions": sessions})


def handle_trace(event: dict, params: dict) -> dict:
    sub, traveler, actor_id = _identity(event, params, {})
    if actor_id is None:
        return _bad(f"traveler must be one of {list(TRAVELER_ALLOWLIST)}")
    trace_id = str(params.get("trace_id", "")).lower()
    if not TRACE_ID_RE.match(trace_id):
        return _bad("trace_id must be 32 lowercase hex chars")

    end = int(time.time()) + 60
    start = end - MAX_WINDOW_S
    q = TRACE_QUERY_TEMPLATE.format(trace_id=trace_id)
    qid = logs.start_query(logGroupName=SPANS_LOG_GROUP, startTime=start,
                           endTime=end, queryString=q)["queryId"]
    rows = []
    for _ in range(12):
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            rows = r.get("results", [])
            break
        time.sleep(1.5)

    spans, owner_prefix = [], f"u-{sub}-"
    owned = foreign = False
    for row in rows:
        d = {c["field"]: c["value"] for c in row if c["field"] in SPAN_FIELDS}
        a = d.get("attributes.obsdemo.actor_id")
        if a:
            if a.startswith(owner_prefix):
                owned = True
            else:
                foreign = True
        spans.append(d)
    if foreign:
        return _resp(403, {"error": "trace does not belong to the authenticated user"})
    if spans and not owned:
        # spans ingest piecemeal: AWS-SDK spans (no actor attribute) can land
        # minutes before the attributed Strands spans — not yet attributable
        return _resp(200, {"trace_id": trace_id, "span_count": 0, "spans": [],
                           "note": "trace not yet attributable — spans still "
                                   "ingesting; retry in a minute"})
    return _resp(200, {"trace_id": trace_id, "span_count": len(spans),
                       "spans": spans, "query_status": r["status"] if rows or r else "Timeout",
                       "note": "spans ingest into aws/spans with delay; retry if empty"})


def handle_memory_logs(event: dict, params: dict) -> dict:
    sub, traveler, actor_id = _identity(event, params, {})
    if actor_id is None:
        return _bad(f"traveler must be one of {list(TRAVELER_ALLOWLIST)}")
    pattern_target = actor_id
    label = params.get("session")
    if label:
        session_id, _ = _derive_session(sub, label)
        if session_id is None:
            return _bad("session must match [a-z0-9-]{1,32}")
        pattern_target = session_id

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - MAX_WINDOW_S * 1000
    events = []
    try:
        r = logs.filter_log_events(
            logGroupName=MEMORY_LOG_GROUP, startTime=start_ms, endTime=end_ms,
            filterPattern=f'"{pattern_target}"', limit=50)
        for e in r.get("events", []):
            events.append({"timestamp": e.get("timestamp"),
                           "logStream": e.get("logStreamName"),
                           "message": e.get("message")})
    except logs.exceptions.ResourceNotFoundException:
        pass
    return _resp(200, {"filter": pattern_target, "event_count": len(events),
                       "events": events,
                       "note": "memory service logs (extraction/consolidation) are async"})


def lambda_handler(event: dict, context) -> dict:
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    params = event.get("queryStringParameters") or {}
    try:
        if method == "POST" and path == "/api/chat":
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError:
                return _bad("request body must be valid JSON")
            if not isinstance(body, dict):
                return _bad("request body must be a JSON object")
            return handle_chat(event, body)
        if method == "GET" and path == "/api/sessions":
            return handle_sessions(event, params)
        if method == "GET" and path == "/api/telemetry/trace":
            return handle_trace(event, params)
        if method == "GET" and path == "/api/telemetry/memory-logs":
            return handle_memory_logs(event, params)
        if method == "GET" and path == "/api/health":
            return _resp(200, {"ok": True})
        return _resp(404, {"error": "not found"})
    except Exception as e:  # never leak internals beyond the error class
        print(f"obsdemo-ux-backend error on {method} {path}: {type(e).__name__}: {e}")
        return _resp(502, {"error": f"upstream failure ({type(e).__name__})"})

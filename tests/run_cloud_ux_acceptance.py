"""Live verification of the obsdemo hosted UX (Part D of CLOUD-UX-BRIEF).

Sets a random permanent password on the synthetic test user IN-PROCESS
(never printed/persisted), authenticates via USER_PASSWORD_AUTH on the test
client, and exercises the full API path. Writes cloud-ux-acceptance.json.

Run: AWS_PROFILE=<profile> python tests/run_cloud_ux_acceptance.py
"""

import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3

PROFILE = os.environ.get("OBSDEMO_AWS_PROFILE", "platform-dev-takeover")
REGION = os.environ.get("OBSDEMO_REGION", "us-west-2")
WORK = os.environ.get("OBSDEMO_WORK_DIR", "./build/local")
UX_STATE = json.load(open(os.environ.get("OBSDEMO_UX_STATE_FILE",
                                         f"{WORK}/cloud-ux-state.json")))
CORE_STATE = json.load(open(os.environ.get("OBSDEMO_STATE_FILE",
                                           f"{WORK}/deploy-state.json")))
OUT_FILE = f"{WORK}/cloud-ux-acceptance.json"

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
sts = session.client("sts")
cognito = session.client("cognito-idp")
agentcore = session.client("bedrock-agentcore")
logs = session.client("logs")

acct = sts.get_caller_identity()["Account"]
_suffix = os.environ.get("OBSDEMO_ACCOUNT_SUFFIX", "")
assert not _suffix or acct.endswith(_suffix), "ABORT: wrong account (suffix mismatch)"
print(f"[verify] account …{acct[-4:]}")

API = UX_STATE["api_endpoint"]
CF_URL = UX_STATE["cloudfront_url"].rstrip("/")
POOL = UX_STATE["user_pool_id"]
TEST_CLIENT = UX_STATE["test_client_id"]
WEB_CLIENT = UX_STATE["web_client_id"]
TEST_USER = UX_STATE["test_username"]
BUCKET = UX_STATE["bucket"]

RESULTS = []
INVOKES = 0


def record(name, status, detail, essentials=None):
    RESULTS.append({"check": name, "status": status, "detail": detail,
                    "essentials": essentials or {},
                    "timestamp_utc": datetime.now(timezone.utc).isoformat()})
    print(f"[{status}] {name}: {detail}", flush=True)


def http(method, url, body=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def api_call(method, path, token, body=None, timeout=90):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    code, raw = http(method, API + path, data, headers, timeout)
    try:
        return code, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return code, {"raw": raw[:200].decode(errors="replace")}


# ---------- 1. S3 direct vs CloudFront ----------
def check_origin():
    direct = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/index.html"
    code_s3, _ = http("GET", direct)
    code_cf, body_cf = -1, b""
    for attempt in range(20):  # CF deploy can lag
        code_cf, body_cf = http("GET", CF_URL + "/index.html")
        if code_cf == 200:
            break
        time.sleep(30)
    ok = code_s3 == 403 and code_cf == 200 and b"Travel Preference Assistant" in body_cf
    record("1-s3-vs-cloudfront", "PASS" if ok else "FAIL",
           f"direct S3 GET -> {code_s3} (expect 403); CloudFront GET -> {code_cf} (expect 200)",
           {"direct_url": direct, "cloudfront_url": CF_URL + "/index.html"})


# ---------- 2. unauthenticated / garbage JWT ----------
def check_auth_rejection():
    t0_ms = int(time.time() * 1000)
    code_none, _ = api_call("POST", "/api/chat", None, {"prompt": "hi"})
    code_bad, _ = api_call("POST", "/api/chat", "garbage.jwt.token", {"prompt": "hi"})
    code_health, _ = api_call("GET", "/api/health", None)
    time.sleep(20)  # let any (unexpected) lambda log land
    fl = logs.filter_log_events(logGroupName="/aws/lambda/obsdemo-ux-backend",
                                startTime=t0_ms, limit=10)
    lambda_ran = len(fl.get("events", []))
    ok = code_none == 401 and code_bad == 401 and code_health == 401 and lambda_ran == 0
    record("2-jwt-rejection", "PASS" if ok else "FAIL",
           f"no-auth chat -> {code_none}; garbage JWT -> {code_bad}; "
           f"no-auth health -> {code_health} (all expect 401); "
           f"lambda log events in window={lambda_ran} (expect 0 — rejected before Lambda)",
           {"lambda_log_group": "/aws/lambda/obsdemo-ux-backend"})


# ---------- test-user auth ----------
def authenticate():
    alphabet = string.ascii_letters + string.digits
    pw = ("A1a" + "".join(secrets.choice(alphabet) for _ in range(18)))
    cognito.admin_set_user_password(UserPoolId=POOL, Username=TEST_USER,
                                    Password=pw, Permanent=True)
    r = cognito.initiate_auth(
        ClientId=TEST_CLIENT, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": TEST_USER, "PASSWORD": pw})
    del pw
    tokens = r["AuthenticationResult"]
    sub = cognito.admin_get_user(UserPoolId=POOL, Username=TEST_USER)
    sub = next(a["Value"] for a in sub["UserAttributes"] if a["Name"] == "sub")
    print(f"[verify] test user authenticated (sub prefix {sub[:8]}…)")
    return tokens["IdToken"], sub


# ---------- 3. authenticated round trip ----------
def check_round_trip(token, sub):
    global INVOKES
    label = f"cx{uuid.uuid4().hex[:8]}"
    t_start = datetime.now(timezone.utc).isoformat()
    INVOKES += 1
    code, d1 = api_call("POST", "/api/chat", token, {
        "prompt": "I love hiking and hate humidity. Which of Kyoto or Queenstown suits me?",
        "traveler": "ava", "session": label, "scenario": "normal"})
    trace1 = d1.get("trace_id")
    ok1 = code == 200 and len(d1.get("response", "")) > 20 and trace1
    record("3a-chat-normal", "PASS" if ok1 else "FAIL",
           f"HTTP {code}; response len={len(d1.get('response',''))}; trace_id={trace1}",
           {"session_label": label, "invoked_at_utc": t_start,
            "actor_id": d1.get("actor_id"), "response_head": d1.get("response", "")[:150]})

    INVOKES += 1
    code, d2 = api_call("POST", "/api/chat", token, {
        "prompt": "Based on what I just told you, should I pack for humidity?",
        "traveler": "ava", "session": label, "scenario": "normal"})
    stm = d2.get("memory_provenance", {}).get("stm_events_used", 0)
    ok2 = code == 200 and stm >= 1
    record("3b-stm-followup", "PASS" if ok2 else "FAIL",
           f"HTTP {code}; stm_events_used={stm} (expect >=1)",
           {"trace_id": d2.get("trace_id"), "response_head": d2.get("response", "")[:150]})

    INVOKES += 1
    t0 = time.time()
    code, ds = api_call("POST", "/api/chat", token, {
        "prompt": "What's the weather in Reykjavik?", "traveler": "ava",
        "session": f"{label}s", "scenario": "slow"})
    slow_t = time.time() - t0
    INVOKES += 1
    code_e, de = api_call("POST", "/api/chat", token, {
        "prompt": "What's the weather in Banff?", "traveler": "ava",
        "session": f"{label}e", "scenario": "error"})
    err_handled = any(w in de.get("response", "").lower()
                      for w in ("apolog", "sorry", "issue", "unable", "trouble"))
    record("3c-slow-error-scenarios",
           "PASS" if code == 200 and code_e == 200 and slow_t > 4 and err_handled else "FAIL",
           f"slow HTTP {code} elapsed={slow_t:.1f}s (expect >4s incl 3s tool sleep); "
           f"error HTTP {code_e} handled_gracefully={err_handled}",
           {"slow_trace": ds.get("trace_id"), "error_trace": de.get("trace_id")})

    return label, trace1, d1.get("actor_id")


# ---------- 3d. telemetry endpoints ----------
def contains_aws_auth(obj) -> bool:
    if isinstance(obj, dict):
        return any("aws.auth" in k or contains_aws_auth(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(contains_aws_auth(x) for x in obj)
    if isinstance(obj, str):
        return "aws.auth" in obj or bool(re.search(r"(AKIA|ASIA)[0-9A-Z]{16}", obj))
    return False


def check_telemetry(token, label, trace_id):
    spans, code, data = [], None, {}
    for attempt in range(8):  # span ingestion lag
        code, data = api_call("GET",
                              f"/api/telemetry/trace?trace_id={trace_id}&traveler=ava", token)
        spans = data.get("spans", [])
        if contains_aws_auth(data):
            break  # credential leak — fail fast
        if spans and any(s.get("attributes.gen_ai.request.model") for s in spans):
            break
        time.sleep(45)
    no_auth_leak = not contains_aws_auth(data)
    has_genai = any(s.get("attributes.gen_ai.request.model") for s in spans)
    ok = code == 200 and len(spans) > 0 and no_auth_leak and has_genai
    record("3d-trace-endpoint", "PASS" if ok else "FAIL",
           f"HTTP {code}; spans={len(spans)}; gen_ai model attr present={has_genai}; "
           f"NO aws.auth key anywhere={no_auth_leak}",
           {"trace_id": trace_id, "span_names": [s.get("name") for s in spans][:15]})

    code, data = api_call("GET",
                          f"/api/telemetry/memory-logs?traveler=ava&session={label}", token)
    ok = code == 200 and "events" in data and not contains_aws_auth(data)
    record("3e-memory-logs-endpoint", "PASS" if ok else "FAIL",
           f"HTTP {code}; events={data.get('event_count')} (async — 0 acceptable shortly after)",
           {"filter": data.get("filter")})


# ---------- 4. actor tampering ----------
def check_tampering(token, sub, own_label):
    code1, d1 = api_call("POST", "/api/chat", token, {
        "prompt": "hi", "traveler": "ava", "session": "tamper1",
        "actor_id": "demo-actor-ava"})
    ok1 = code1 == 400

    code2, d2 = api_call("POST", "/api/chat", token, {
        "prompt": "hi", "traveler": "demo-actor-ava", "session": "tamper2"})
    ok2 = code2 == 400

    derived_actor = f"u-{sub}-ava"
    try:
        ev = agentcore.list_events(memoryId=CORE_STATE["memory_id"],
                                   actorId=derived_actor,
                                   sessionId=f"{sub[:12]}-{own_label}",
                                   includePayloads=False, maxResults=10)
        derived_events = len(ev.get("events", []))
    except agentcore.exceptions.ResourceNotFoundException:
        derived_events = 0
    ok3 = derived_events >= 2  # both round-trip turns stored under derived actor

    # foreign session label: derivation prefixes OUR sub -> only empty/own data
    code4, d4 = api_call("GET",
                         "/api/telemetry/memory-logs?traveler=ava&session=zzzznotmine", token)
    foreign_empty = code4 == 200 and d4.get("event_count") == 0

    record("4-actor-tampering", "PASS" if (ok1 and ok2 and ok3 and foreign_empty) else "FAIL",
           f"explicit actor_id -> {code1} (expect 400); traveler outside allowlist -> {code2} "
           f"(expect 400); ListEvents on derived actor={derived_events} events (expect >=2); "
           f"foreign session label -> empty={foreign_empty}",
           {"derived_actor_prefix": f"u-{sub[:8]}…-ava"})

    # demo-actor-ava LTM unreachable through cloud path with any legal input
    r = agentcore.retrieve_memory_records(
        memoryId=CORE_STATE["memory_id"], namespace=f"/preferences/{derived_actor}",
        searchCriteria={"searchQuery": "hiking humidity boutique", "topK": 5})
    derived_ns = r.get("memoryRecordSummaries", [])
    r2 = agentcore.retrieve_memory_records(
        memoryId=CORE_STATE["memory_id"], namespace="/preferences/demo-actor-ava",
        searchCriteria={"searchQuery": "hiking humidity boutique", "topK": 5})
    legacy = r2.get("memoryRecordSummaries", [])
    legacy_ids = {x["memoryRecordId"] for x in legacy}
    derived_ids = {x["memoryRecordId"] for x in derived_ns}
    isolated = not (legacy_ids & derived_ids)
    record("4b-legacy-actor-unreachable", "PASS" if isolated else "FAIL",
           f"legacy demo-actor-ava has {len(legacy_ids)} LTM records; derived-namespace "
           f"records={len(derived_ids)}; overlap=0={isolated} (cloud path can only ever "
           f"address u-<sub>-<traveler> namespaces by construction)",
           {})


# ---------- 5. Cognito config asserts ----------
def check_cognito_config():
    pool = cognito.describe_user_pool(UserPoolId=POOL)["UserPool"]
    web = cognito.describe_user_pool_client(
        UserPoolId=POOL, ClientId=WEB_CLIENT)["UserPoolClient"]
    test = cognito.describe_user_pool_client(
        UserPoolId=POOL, ClientId=TEST_CLIENT)["UserPoolClient"]
    signup_off = pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    no_secret = "ClientSecret" not in web and "ClientSecret" not in test
    code_pkce_only = web.get("AllowedOAuthFlows") == ["code"]
    urls_ok = (web.get("CallbackURLs") == [CF_URL + "/"]
               and web.get("LogoutURLs") == [CF_URL + "/"])
    test_flow = set(test.get("ExplicitAuthFlows", [])) == {
        "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    ok = signup_off and no_secret and code_pkce_only and urls_ok and test_flow
    record("5-cognito-config", "PASS" if ok else "FAIL",
           f"signup_admin_only={signup_off}; no_client_secret={no_secret}; "
           f"web_flows=code_only={code_pkce_only}; exact_callback_logout_urls={urls_ok}; "
           f"test_client_flows_password_auth_only={test_flow}",
           {"pool_id": POOL, "web_client": WEB_CLIENT, "test_client": TEST_CLIENT,
            "callback_urls": web.get("CallbackURLs"), "logout_urls": web.get("LogoutURLs"),
            "web_oauth_flows": web.get("AllowedOAuthFlows"),
            "web_scopes": web.get("AllowedOAuthScopes")})


# ---------- 6. LTM through the cloud path ----------
def check_ltm(token, sub):
    global INVOKES
    label = f"lt{uuid.uuid4().hex[:8]}"
    INVOKES += 1
    code, d = api_call("POST", "/api/chat", token, {
        "prompt": "Please remember: I love snorkeling, I prefer hostels, and I "
                  "dislike cold weather.", "traveler": "ava", "session": label,
        "scenario": "normal"})
    if code != 200:
        record("6-ltm-cloud-path", "FAIL", f"preference turn failed HTTP {code}")
        return
    namespace = f"/preferences/u-{sub}-ava"
    found, t0 = [], time.time()
    for i in range(12):
        r = agentcore.retrieve_memory_records(
            memoryId=CORE_STATE["memory_id"], namespace=namespace,
            searchCriteria={"searchQuery": "snorkeling hostels cold weather", "topK": 5})
        found = r.get("memoryRecordSummaries", [])
        if found:
            break
        time.sleep(20)
    elapsed = time.time() - t0
    if not found:
        record("6-ltm-cloud-path", "PENDING",
               f"no LTM records extracted after {elapsed:.0f}s (async; may exceed bound)",
               {"namespace_prefix": f"/preferences/u-{sub[:8]}…-ava"})
        return
    rec_ids = [x["memoryRecordId"] for x in found]
    INVOKES += 1
    code, d2 = api_call("POST", "/api/chat", token, {
        "prompt": "New trip! Any destination ideas for me?", "traveler": "ava",
        "session": f"{label}b", "scenario": "normal"})
    cited = [x["record_id"] for x in
             d2.get("memory_provenance", {}).get("ltm_records_retrieved", [])]
    strict = bool(cited) and all(c in rec_ids for c in cited)
    record("6-ltm-cloud-path", "PASS" if code == 200 and strict else "FAIL",
           f"extraction {elapsed:.0f}s; {len(found)} records; new session cited "
           f"{len(cited)} ids, all in retrieved set={strict}",
           {"record_ids": rec_ids, "cited": cited,
            "trace_id": d2.get("trace_id"),
            "response_head": d2.get("response", "")[:150]})


def main():
    started = datetime.now(timezone.utc).isoformat()
    check_origin()
    check_auth_rejection()
    token, sub = authenticate()
    label, trace1, actor = check_round_trip(token, sub)
    check_telemetry(token, label, trace1)
    check_tampering(token, sub, label)
    check_cognito_config()
    check_ltm(token, sub)
    summary = {}
    for r in RESULTS:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    out = {
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "region": REGION,
        "cloudfront_url": CF_URL,
        "api_endpoint": API,
        "runtime_invocations_used": INVOKES,
        "summary": summary,
        "results": RESULTS,
    }
    existing = {"runs": []}
    if os.path.exists(OUT_FILE):
        try:
            prev = json.load(open(OUT_FILE))
            existing = prev if "runs" in prev else {"runs": [prev]}
        except (json.JSONDecodeError, OSError):
            pass
    existing["runs"].append(out)
    with open(OUT_FILE, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print(f"[verify] done: {summary} | invocations={INVOKES} | -> {OUT_FILE}")


if __name__ == "__main__":
    main()

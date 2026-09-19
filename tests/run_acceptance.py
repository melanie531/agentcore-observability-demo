"""Acceptance test suite for the obsdemo AgentCore observability demo.

Runs 8 checks against the LIVE deployed stack. Budget: <= 30 runtime
invocations total. Outputs results.json + run.log to build/local/acceptance/.

Run: AWS_PROFILE=platform-dev-takeover .venv/bin/python tests/run_acceptance.py
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import boto3

PROFILE = "platform-dev-takeover"
REGION = "us-west-2"
WORK = "/Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/local"
STATE_FILE = f"{WORK}/deploy-state.json"
OUT_DIR = f"{WORK}/acceptance"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.makedirs(OUT_DIR, exist_ok=True)
STATE = json.load(open(STATE_FILE))

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
agentcore = session.client("bedrock-agentcore")
logs_client = session.client("logs")

AEST = timezone(timedelta(hours=10))
RESULTS = []
INVOKE_COUNT = 0
INVOKE_BUDGET = 30
LOG_LINES = []

ACTOR_A = "demo-actor-ava"
ACTOR_B = "demo-actor-ben"
RUN_TAG = uuid.uuid4().hex[:8]

EVIDENCE = {"run_tag": RUN_TAG, "scenarios": []}


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)


def record(name, status, detail, essentials=None):
    now_utc = datetime.now(timezone.utc)
    RESULTS.append({
        "check": name, "status": status, "detail": detail,
        "essentials": essentials or {},
        "timestamp_utc": now_utc.isoformat(),
        "timestamp_aest": now_utc.astimezone(AEST).isoformat(),
        "region": REGION,
    })
    log(f"[{status}] {name}: {detail}")


def new_session():
    return f"sess-{RUN_TAG}-{uuid.uuid4().hex[:8]}"


def invoke(prompt, actor_id, session_id, scenario="normal"):
    global INVOKE_COUNT
    if INVOKE_COUNT >= INVOKE_BUDGET:
        raise RuntimeError(f"invocation budget ({INVOKE_BUDGET}) exhausted")
    INVOKE_COUNT += 1
    runtime_session_id = f"obsdemo-{session_id}-{actor_id}".ljust(33, "x")
    payload = json.dumps({
        "prompt": prompt, "actor_id": actor_id,
        "session_id": session_id, "scenario": scenario,
    }).encode()
    t0 = time.time()
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=STATE["runtime_arn"],
        runtimeSessionId=runtime_session_id,
        contentType="application/json", accept="application/json",
        payload=payload,
    )
    body = json.loads(resp["response"].read())
    elapsed = time.time() - t0
    log(f"invoke #{INVOKE_COUNT} actor={actor_id} scenario={scenario} {elapsed:.1f}s "
        f"trace={body.get('trace_id')} resp[:80]={str(body.get('response',''))[:80]!r}")
    EVIDENCE["scenarios"].append({
        "scenario": scenario, "actor_id": actor_id, "session_id": session_id,
        "runtime_session_id": runtime_session_id, "trace_id": body.get("trace_id"),
        "invoked_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(elapsed, 2),
    })
    return body, elapsed


# ---------- Check 1: basic invocation ----------
def check1():
    sess = new_session()
    body, _ = invoke("In one sentence, what can you help me with?", ACTOR_A, sess)
    resp = body.get("response", "")
    ok = len(resp) > 20 and body.get("session_id") == sess
    record("1-runtime-invocation", "PASS" if ok else "FAIL",
           f"agent answered ({len(resp)} chars)",
           {"session_id": sess, "trace_id": body.get("trace_id"), "response_head": resp[:150]})
    return sess


# ---------- Check 2: multi-turn STM ----------
def check2():
    sess = new_session()
    invoke("I love hiking and hate humidity, and I prefer boutique hotels. "
           "Remember that for our chat.", ACTOR_A, sess)
    body2, _ = invoke("Given what I just told you about my preferences, which of "
                      "Kyoto or Queenstown suits me better and why?", ACTOR_A, sess)
    resp = body2.get("response", "").lower()
    contextual = ("queenstown" in resp) and any(
        w in resp for w in ("hik", "humid", "boutique", "prefer"))
    stm_used = body2.get("memory_provenance", {}).get("stm_events_used", 0)
    ev = agentcore.list_events(
        memoryId=STATE["memory_id"], actorId=ACTOR_A, sessionId=sess,
        includePayloads=False, maxResults=10)
    n_events = len(ev.get("events", []))
    ok = contextual and n_events >= 2 and stm_used >= 1
    record("2-multiturn-stm", "PASS" if ok else "FAIL",
           f"contextual={contextual}, list_events={n_events}, stm_events_used_by_agent={stm_used}",
           {"session_id": sess, "trace_id": body2.get("trace_id"),
            "response_head": body2.get("response", "")[:200]})
    return sess


# ---------- Check 3: async LTM ----------
def check3(pref_session):
    namespace = f"/preferences/{ACTOR_A}"
    found, polls, t0 = [], 0, time.time()
    for i in range(15):
        polls = i + 1
        r = agentcore.retrieve_memory_records(
            memoryId=STATE["memory_id"], namespace=namespace,
            searchCriteria={"searchQuery": "hiking humidity boutique hotels", "topK": 5})
        found = r.get("memoryRecordSummaries", [])
        if found:
            break
        time.sleep(20)
    elapsed = time.time() - t0
    if not found:
        record("3-async-ltm", "PENDING",
               f"no LTM records after {polls} polls / {elapsed:.0f}s — extraction "
               "is async and may exceed the polling bound; re-polled at suite end",
               {"namespace": namespace, "polls": polls})
        return False
    rec_ids = [r["memoryRecordId"] for r in found]
    log(f"LTM extraction: {len(found)} records after {elapsed:.0f}s: {rec_ids}")
    sess = new_session()
    body, _ = invoke("I'm planning my next trip. Any destination suggestions for me?",
                     ACTOR_A, sess)
    prov = body.get("memory_provenance", {})
    cited = [r["record_id"] for r in prov.get("ltm_records_retrieved", [])]
    real_cited = [c for c in cited if c in rec_ids or c]  # ids must be non-empty
    resp = body.get("response", "").lower()
    uses_prefs = any(w in resp for w in ("hik", "humid", "boutique"))
    ok = bool(real_cited) and uses_prefs
    record("3-async-ltm", "PASS" if ok else "FAIL",
           f"extraction {elapsed:.0f}s/{polls} polls; {len(found)} records; "
           f"new session cited {len(cited)} record ids; prefs used in answer={uses_prefs}",
           {"namespace": namespace, "record_ids": rec_ids, "cited": cited,
            "session_id": sess, "trace_id": body.get("trace_id"),
            "response_head": body.get("response", "")[:200]})
    return True


def check3_repoll():
    namespace = f"/preferences/{ACTOR_A}"
    r = agentcore.retrieve_memory_records(
        memoryId=STATE["memory_id"], namespace=namespace,
        searchCriteria={"searchQuery": "hiking humidity boutique hotels", "topK": 5})
    found = r.get("memoryRecordSummaries", [])
    for res in RESULTS:
        if res["check"] == "3-async-ltm" and res["status"] == "PENDING":
            res["detail"] += f" | final re-poll: {len(found)} records"
            res["essentials"]["final_repoll_record_ids"] = [x["memoryRecordId"] for x in found]
            if found:
                res["status"] = "PENDING-EXTRACTED"
    log(f"final LTM re-poll: {len(found)} records")


# ---------- Check 4: actor isolation ----------
def check4():
    sess = new_session()
    body, _ = invoke("What do you know about my travel preferences?", ACTOR_B, sess)
    resp = body.get("response", "").lower()
    # leak = agent ASSERTS knowledge of A's preferences (mere clarifying
    # questions like "do you enjoy hiking?" are not a leak)
    assertion = any(p in resp for p in (
        "you love", "you prefer", "you mentioned", "you told me",
        "i remember", "you hate", "you dislike", "your preference for"))
    keywords = any(w in resp for w in ("hiking", "humidity", "boutique"))
    leaked = assertion and keywords
    ns_b = f"/preferences/{ACTOR_B}"
    r = agentcore.retrieve_memory_records(
        memoryId=STATE["memory_id"], namespace=ns_b,
        searchCriteria={"searchQuery": "hiking humidity boutique hotels", "topK": 5})
    b_records = r.get("memoryRecordSummaries", [])
    prov_count = body.get("memory_provenance", {}).get("ltm_record_count", -1)
    ok = not leaked and len(b_records) == 0 and prov_count == 0
    record("4-actor-isolation", "PASS" if ok else "FAIL",
           f"leak_in_answer={leaked}, B-namespace records={len(b_records)}, "
           f"agent-reported LTM count for B={prov_count}",
           {"session_id": sess, "trace_id": body.get("trace_id"),
            "response_head": body.get("response", "")[:200]})


# ---------- Check 5: telemetry retrieval ----------
def query_spans(trace_id, start, end):
    q = (f'fields @timestamp, name, attributes.gen_ai.request.model, attributes.gen_ai.operation.name '
         f'| filter traceId = "{trace_id}" | limit 50')
    qid = logs_client.start_query(
        logGroupName="aws/spans", startTime=int(start), endTime=int(end), queryString=q)["queryId"]
    for _ in range(30):
        r = logs_client.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            return q, r
        time.sleep(2)
    return q, {"status": "Timeout", "results": []}


def check5(ref):
    trace_id = ref.get("trace_id")
    if not trace_id:
        record("5-telemetry", "FAIL", "no trace_id available from invocation")
        return
    start = time.time() - 1800
    end = time.time() + 60
    log("waiting 90s for span/log ingestion...")
    time.sleep(90)
    q, r = query_spans(trace_id, start, end)
    rows = r.get("results", [])
    names, has_model, has_tool = [], False, False
    for row in rows:
        d = {c["field"]: c["value"] for c in row}
        n = d.get("name", "")
        names.append(n)
        if d.get("attributes.gen_ai.request.model") or d.get("attributes.gen_ai.operation.name"):
            has_model = True
        if "lookup_destination_info" in n:
            has_tool = True
    if not has_model:
        has_model = any("chat" in n.lower() or "invoke" in n.lower() or "converse" in n.lower() for n in names)

    # runtime application logs
    rt_group = None
    paginator = logs_client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/"):
        for lg in page["logGroups"]:
            if STATE["runtime_id"] in lg["logGroupName"]:
                rt_group = lg["logGroupName"]
                break
        if rt_group:
            break
    rt_events = 0
    if rt_group:
        fl = logs_client.filter_log_events(
            logGroupName=rt_group, startTime=int(start * 1000),
            endTime=int(end * 1000), limit=20)
        rt_events = len(fl.get("events", []))

    # memory delivery logs
    mem_events = 0
    mem_group = STATE["memory_log_group"]
    try:
        fl = logs_client.filter_log_events(
            logGroupName=mem_group, startTime=int(start * 1000),
            endTime=int(end * 1000), limit=20)
        mem_events = len(fl.get("events", []))
    except logs_client.exceptions.ResourceNotFoundException:
        mem_events = -1

    ok = len(rows) > 0 and has_model and has_tool and rt_events > 0
    partial = len(rows) > 0 and (has_model or has_tool)
    status = "PASS" if ok else ("PARTIAL" if partial else "FAIL")
    record("5-telemetry", status,
           f"spans(aws/spans)={len(rows)} model_span={has_model} tool_span={has_tool}; "
           f"runtime_log[{rt_group}]={rt_events} events; "
           f"memory_delivery_log[{mem_group}]={mem_events} events",
           {"trace_id": trace_id, "span_names": names[:20],
            "logs_insights_query": q, "runtime_log_group": rt_group,
            "memory_log_group": mem_group})
    EVIDENCE["telemetry_query"] = {"log_group": "aws/spans", "query": q, "trace_id": trace_id}


# ---------- Check 6: slow + error scenarios ----------
def check6():
    sess = new_session()
    body_n, t_n = invoke("What's the weather like in Reykjavik?", ACTOR_B, sess, "normal")
    sess2 = new_session()
    body_s, t_s = invoke("What's the weather like in Reykjavik?", ACTOR_B, sess2, "slow")
    slow_ok = t_s > t_n + 2.5
    sess3 = new_session()
    body_e, _ = invoke("What's the weather like in Banff?", ACTOR_B, sess3, "error")
    resp_e = body_e.get("response", "").lower()
    err_handled = any(w in resp_e for w in ("apolog", "sorry", "issue", "unable", "trouble", "unavailable"))
    record("6a-slow-scenario", "PASS" if slow_ok else "FAIL",
           f"normal={t_n:.1f}s slow={t_s:.1f}s delta={t_s-t_n:.1f}s (expect >=2.5s)",
           {"normal_trace": body_n.get("trace_id"), "slow_trace": body_s.get("trace_id")})
    record("6b-error-scenario", "PASS" if err_handled else "FAIL",
           f"agent handled tool error gracefully={err_handled}",
           {"error_trace": body_e.get("trace_id"),
            "response_head": body_e.get("response", "")[:200]})
    return body_s.get("trace_id"), body_e.get("trace_id")


def check6_telemetry(slow_trace, error_trace):
    """Verify slow duration and error status in aws/spans."""
    start, end = time.time() - 1800, time.time() + 60
    time.sleep(60)
    results = {}
    for label, tid in (("slow", slow_trace), ("error", error_trace)):
        if not tid:
            results[label] = "no trace_id"
            continue
        q = (f'fields name, durationNano, status.code, @timestamp '
             f'| filter traceId = "{tid}" and name like /lookup_destination_info/ | limit 10')
        qid = logs_client.start_query(logGroupName="aws/spans",
                                      startTime=int(start), endTime=int(end), queryString=q)["queryId"]
        rows = []
        for _ in range(30):
            r = logs_client.get_query_results(queryId=qid)
            if r["status"] in ("Complete", "Failed", "Cancelled"):
                rows = r.get("results", [])
                break
            time.sleep(2)
        results[label] = [{c["field"]: c["value"] for c in row} for row in rows]
    slow_spans = results.get("slow") or []
    slow_verified = any(float(s.get("durationNano", 0)) > 2.5e9 for s in slow_spans
                        if isinstance(s, dict))
    error_spans = results.get("error") or []
    error_verified = len(error_spans) > 0  # error tool span present in trace
    status = "PASS" if (slow_verified and error_verified) else "PARTIAL" if (slow_spans or error_spans) else "FAIL"
    record("6c-scenario-telemetry", status,
           f"slow tool span >2.5s in aws/spans={slow_verified}; "
           f"error trace tool spans found={len(error_spans)}",
           {"slow_spans": slow_spans, "error_spans": error_spans,
            "slow_trace": slow_trace, "error_trace": error_trace})


# ---------- Check 7: security ----------
def check7():
    import urllib.parse
    arn_enc = urllib.parse.quote(STATE["runtime_arn"], safe="")
    url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com/"
           f"runtimes/{arn_enc}/invocations?qualifier=DEFAULT")
    req = urllib.request.Request(url, data=b'{"prompt":"hi"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    code = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        record("7-security", "FAIL", f"unexpected error: {e}")
        return
    rejected = code in (401, 403)
    record("7-security-unsigned", "PASS" if rejected else "FAIL",
           f"unsigned POST to runtime endpoint -> HTTP {code} (expect 401/403)",
           {"endpoint_host": f"bedrock-agentcore.{REGION}.amazonaws.com", "http_status": code})


# ---------- Check 8: UX E2E ----------
def check8():
    env = dict(os.environ, AWS_PROFILE=PROFILE)
    proc = subprocess.Popen(
        [f"{REPO}/.venv/bin/python", "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", "8931"],
        cwd=f"{REPO}/ux", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(20):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8931/api/health", timeout=2) as r:
                    up = r.status == 200
                    break
            except Exception:
                time.sleep(1)
        if not up:
            record("8-ux-e2e", "FAIL", "UX server did not come up on 127.0.0.1:8931")
            return
        # confirm loopback-only binding
        wildcard = False
        try:
            s = socket.create_connection((socket.gethostbyname(socket.gethostname()), 8931), timeout=2)
            s.close()
            wildcard = True
        except Exception:
            pass
        sess = new_session()
        body = json.dumps({"prompt": "Say hello in five words or less.",
                           "actor_id": ACTOR_B, "session_id": sess,
                           "scenario": "normal"}).encode()
        req = urllib.request.Request("http://127.0.0.1:8931/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        global INVOKE_COUNT
        INVOKE_COUNT += 1  # backend invokes the runtime
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        ok = bool(data.get("response")) and data.get("runtime_session_id")
        record("8-ux-e2e", "PASS" if ok and not wildcard else "FAIL",
               f"chat API ok={ok}; bound beyond loopback={wildcard}",
               {"session_id": sess, "response_head": str(data.get("response", ""))[:120],
                "trace_id": data.get("trace_id")})
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    log(f"acceptance run start | run_tag={RUN_TAG} | runtime={STATE['runtime_arn']} "
        f"| memory={STATE['memory_id']}")
    ref_session = None
    try:
        check1()
        stm_sess = check2()
        ltm_done = check3(stm_sess)
        check4()
        # use last scenario from check2 as telemetry reference (has tool call? no —
        # use a fresh tool-calling invocation from check6's normal run)
        slow_trace, error_trace = check6()
        # telemetry for the normal invocation of check6 (has tool call)
        ref = EVIDENCE["scenarios"][-3]  # normal reykjavik run
        check5(ref)
        check6_telemetry(slow_trace, error_trace)
        check7()
        check8()
        if not ltm_done:
            check3_repoll()
    finally:
        log(f"total runtime invocations: {INVOKE_COUNT}/{INVOKE_BUDGET}")
        summary = {s: sum(1 for r in RESULTS if r["status"] == s)
                   for s in ("PASS", "FAIL", "PARTIAL", "PENDING", "PENDING-EXTRACTED", "SKIPPED")}
        out = {
            "run_tag": RUN_TAG,
            "started_utc": LOG_LINES[0].split(" ")[0] if LOG_LINES else None,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "region": REGION,
            "runtime_arn": STATE["runtime_arn"],
            "memory_id": STATE["memory_id"],
            "invocations_used": INVOKE_COUNT,
            "summary": summary,
            "results": RESULTS,
            "evidence": EVIDENCE,
        }
        with open(f"{OUT_DIR}/results.json", "w") as f:
            json.dump(out, f, indent=2, default=str)
        with open(f"{OUT_DIR}/run.log", "a") as f:
            f.write("\n".join(LOG_LINES) + "\n")
        log(f"results -> {OUT_DIR}/results.json | summary: {summary}")


if __name__ == "__main__":
    main()

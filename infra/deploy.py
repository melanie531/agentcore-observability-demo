"""Idempotent deployment of the obsdemo AgentCore stack.

Steps: IAM role -> ECR repo -> memory (+ wait ACTIVE) -> memory vended log
delivery -> agent runtime (+ wait READY). Image build/push is a separate shell
step (see infra/build_push.sh) because it needs docker buildx.

Run: .venv/bin/python infra/deploy.py [--skip-runtime]
State (ids/arns) is written to STATE_FILE for tests and the UX backend.
"""

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

from config import (
    ECR_REPO, LOG_RETENTION_DAYS, LTM_NAMESPACE, MEMORY_LOG_GROUP, MEMORY_NAME,
    MODEL_ID, PROFILE, REGION, ROLE_NAME, RUNTIME_NAME, STATE_FILE, TAGS,
)

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
iam = session.client("iam")
ecr = session.client("ecr")
logs = session.client("logs")
ctl = session.client("bedrock-agentcore-control")
sts = session.client("sts")

ACCOUNT_ID = sts.get_caller_identity()["Account"]
TAG_LIST = [{"Key": k, "Value": v} for k, v in TAGS.items()]


def log(msg):
    print(f"[deploy] {msg}", flush=True)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def ensure_role(memory_arn):
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AssumeRolePolicy",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:*"},
            },
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ECRImageAccess",
                "Effect": "Allow",
                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                "Resource": [f"arn:aws:ecr:{REGION}:{ACCOUNT_ID}:repository/{ECR_REPO}"],
            },
            {
                "Sid": "ECRTokenAccess",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Sid": "RuntimeLogsCreate",
                "Effect": "Allow",
                "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                "Resource": [f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"],
            },
            {
                "Sid": "RuntimeLogsDescribe",
                "Effect": "Allow",
                "Action": ["logs:DescribeLogGroups"],
                "Resource": [f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:*"],
            },
            {
                "Sid": "RuntimeLogsPut",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"],
            },
            {
                "Sid": "XRayTraces",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules", "xray:GetSamplingTargets",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "AgentCoreMetrics",
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            },
            {
                "Sid": "GetAgentAccessToken",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:workload-identity-directory/default/workload-identity/{RUNTIME_NAME}-*",
                ],
            },
            {
                "Sid": "BedrockModelInvocation",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
                    f"arn:aws:bedrock:*:{ACCOUNT_ID}:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0",
                ],
            },
            {
                "Sid": "MemoryDataPlane",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                "Resource": [memory_arn],
            },
        ],
    }
    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for obsdemo AgentCore travel agent (demo)",
            Tags=TAG_LIST,
        )
        log(f"created role {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        log(f"role {ROLE_NAME} exists; trust policy refreshed")
    iam.put_role_policy(
        RoleName=ROLE_NAME, PolicyName="obsdemo-runtime-inline",
        PolicyDocument=json.dumps(policy),
    )
    log("inline policy attached")
    return iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]


def ensure_ecr():
    try:
        r = ecr.create_repository(repositoryName=ECR_REPO, tags=TAG_LIST)
        uri = r["repository"]["repositoryUri"]
        log(f"created ECR repo {ECR_REPO}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise
        uri = ecr.describe_repositories(repositoryNames=[ECR_REPO])["repositories"][0]["repositoryUri"]
        log(f"ECR repo {ECR_REPO} exists")
    return uri


def ensure_memory():
    for m in ctl.list_memories(maxResults=100).get("memories", []):
        if m["id"].startswith(MEMORY_NAME + "-"):
            log(f"memory {m['id']} exists ({m['status']})")
            return wait_memory_active(m["id"])
    r = ctl.create_memory(
        name=MEMORY_NAME,
        description="obsdemo travel assistant memory (STM events + user-preference LTM)",
        eventExpiryDuration=7,
        memoryStrategies=[{
            "userPreferenceMemoryStrategy": {
                "name": "TravelerPreferences",
                "description": "Extracts traveler preferences (activities, climate, lodging)",
                "namespaces": [LTM_NAMESPACE],
            }
        }],
        tags=TAGS,
    )
    mem_id = r["memory"]["id"]
    log(f"created memory {mem_id}; waiting ACTIVE...")
    return wait_memory_active(mem_id)


def wait_memory_active(mem_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = ctl.get_memory(memoryId=mem_id)["memory"]
        status = m["status"]
        strat_statuses = [s.get("status") for s in m.get("strategies", [])]
        if status == "ACTIVE" and all(s == "ACTIVE" for s in strat_statuses):
            log(f"memory {mem_id} ACTIVE (strategies: {strat_statuses})")
            return m
        if status == "FAILED":
            raise RuntimeError(f"memory {mem_id} FAILED: {m.get('failureReason')}")
        time.sleep(10)
    raise TimeoutError(f"memory {mem_id} not ACTIVE within {timeout}s")


def ensure_log_group(name):
    try:
        logs.create_log_group(logGroupName=name, tags=TAGS)
        log(f"created log group {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        log(f"log group {name} exists")
    logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)


def ensure_memory_log_delivery(memory_arn, mem_id):
    """Vended log delivery: APPLICATION_LOGS -> CWL log group, TRACES -> X-Ray."""
    ensure_log_group(MEMORY_LOG_GROUP)
    results = {}
    specs = [
        ("obsdemo-travel-memory-logs", "APPLICATION_LOGS", "CWL",
         f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:{MEMORY_LOG_GROUP}"),
        ("obsdemo-travel-memory-traces", "TRACES", "XRAY", ""),
    ]
    for base, log_type, dst_type, dst_arn in specs:
        src_name = f"{base}-source"
        dst_name = f"{base}-destination"
        try:
            logs.put_delivery_source(
                name=src_name, resourceArn=memory_arn, logType=log_type, tags=TAGS)
            log(f"delivery source {src_name} ({log_type})")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ConflictException", "ResourceAlreadyExistsException"):
                raise
            log(f"delivery source {src_name} exists")
        kwargs = {"name": dst_name, "deliveryDestinationType": dst_type, "tags": TAGS}
        if dst_arn:
            kwargs["deliveryDestinationConfiguration"] = {"destinationResourceArn": dst_arn}
        try:
            d = logs.put_delivery_destination(**kwargs)
            dst_full_arn = d["deliveryDestination"]["arn"]
            log(f"delivery destination {dst_name} ({dst_type})")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ConflictException", "ResourceAlreadyExistsException"):
                raise
            dst_full_arn = f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:delivery-destination:{dst_name}"
            log(f"delivery destination {dst_name} exists")
        try:
            r = logs.create_delivery(
                deliverySourceName=src_name, deliveryDestinationArn=dst_full_arn, tags=TAGS)
            log(f"delivery created: {src_name} -> {dst_name} ({r['delivery']['id']})")
            results[log_type] = r["delivery"]["id"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                raise
            log(f"delivery {src_name} -> {dst_name} exists")
            results[log_type] = "existing"
    return results


def ensure_runtime(image_uri, role_arn, mem_id):
    existing = None
    for r in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if r["agentRuntimeName"] == RUNTIME_NAME:
            existing = r
            break
    env = {
        "OBSDEMO_MEMORY_ID": mem_id,
        "OBSDEMO_MODEL_ID": MODEL_ID,
        "OBSDEMO_LTM_NAMESPACE": LTM_NAMESPACE,
        "AWS_REGION": REGION,
    }
    if existing:
        log(f"runtime {RUNTIME_NAME} exists ({existing['status']}); updating")
        ctl.update_agent_runtime(
            agentRuntimeId=existing["agentRuntimeId"],
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env,
        )
        rt_id = existing["agentRuntimeId"]
    else:
        r = ctl.create_agent_runtime(
            agentRuntimeName=RUNTIME_NAME,
            description="obsdemo travel-preference assistant (observability demo)",
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env,
            tags=TAGS,
        )
        rt_id = r["agentRuntimeId"]
        log(f"created runtime {RUNTIME_NAME} ({rt_id}); waiting READY...")
    return wait_runtime_ready(rt_id)


def wait_runtime_ready(rt_id, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = ctl.get_agent_runtime(agentRuntimeId=rt_id)
        status = r["status"]
        if status == "READY":
            log(f"runtime {rt_id} READY")
            return r
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED"):
            raise RuntimeError(f"runtime {rt_id} {status}: {r.get('failureReason', r)}")
        time.sleep(15)
    raise TimeoutError(f"runtime {rt_id} not READY within {timeout}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-runtime", action="store_true",
                    help="stop after memory + log delivery (before image exists)")
    ap.add_argument("--image-tag", default="latest")
    args = ap.parse_args()

    state = load_state()
    state["account_id"] = ACCOUNT_ID
    state["region"] = REGION

    ecr_uri = ensure_ecr()
    state["ecr_uri"] = ecr_uri
    save_state(state)

    mem = ensure_memory()
    state["memory_id"] = mem["id"]
    state["memory_arn"] = mem["arn"]
    save_state(state)

    role_arn = ensure_role(mem["arn"])
    state["role_arn"] = role_arn
    save_state(state)

    state["deliveries"] = ensure_memory_log_delivery(mem["arn"], mem["id"])
    state["memory_log_group"] = MEMORY_LOG_GROUP
    save_state(state)

    if args.skip_runtime:
        log("--skip-runtime: done (build/push image, then rerun without flag)")
        return

    image_uri = f"{ecr_uri}:{args.image_tag}"
    rt = ensure_runtime(image_uri, role_arn, mem["id"])
    state["runtime_id"] = rt["agentRuntimeId"]
    state["runtime_arn"] = rt["agentRuntimeArn"]
    state["runtime_version"] = rt.get("agentRuntimeVersion")
    state["runtime_log_group_hint"] = f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT"
    save_state(state)
    log(f"DONE. state -> {STATE_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)

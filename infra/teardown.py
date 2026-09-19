"""Teardown for the obsdemo stack. NOT executed automatically — run manually
after the demo: .venv/bin/python infra/teardown.py --confirm

Deletes ONLY obsdemo-prefixed resources: runtime, memory, deliveries,
delivery sources/destinations, log groups, IAM role, ECR repo.
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

from config import (
    ECR_REPO, MEMORY_LOG_GROUP, MEMORY_NAME, PROFILE, REGION, ROLE_NAME,
    RUNTIME_NAME,
)

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
iam = session.client("iam")
ecr = session.client("ecr")
logs = session.client("logs")
ctl = session.client("bedrock-agentcore-control")


def log(msg):
    print(f"[teardown] {msg}", flush=True)


def safe(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return True
    except ClientError as e:
        log(f"skip ({e.response['Error']['Code']}): {getattr(fn, '__name__', fn)}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    if not args.confirm:
        print("Refusing to run without --confirm. This deletes the obsdemo stack.")
        sys.exit(1)

    # runtime
    for r in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if r["agentRuntimeName"] == RUNTIME_NAME:
            log(f"deleting runtime {r['agentRuntimeId']}")
            safe(ctl.delete_agent_runtime, agentRuntimeId=r["agentRuntimeId"])

    # deliveries referencing obsdemo sources
    for d in logs.describe_deliveries().get("deliveries", []):
        if d["deliverySourceName"].startswith("obsdemo-"):
            log(f"deleting delivery {d['id']}")
            safe(logs.delete_delivery, id=d["id"])
    for s in logs.describe_delivery_sources().get("deliverySources", []):
        if s["name"].startswith("obsdemo-"):
            log(f"deleting delivery source {s['name']}")
            safe(logs.delete_delivery_source, name=s["name"])
    for d in logs.describe_delivery_destinations().get("deliveryDestinations", []):
        if d["name"].startswith("obsdemo-"):
            log(f"deleting delivery destination {d['name']}")
            safe(logs.delete_delivery_destination, name=d["name"])

    # memory
    for m in ctl.list_memories(maxResults=100).get("memories", []):
        if m["id"].startswith(MEMORY_NAME + "-"):
            log(f"deleting memory {m['id']}")
            safe(ctl.delete_memory, memoryId=m["id"])

    # log groups (memory delivery + runtime log groups)
    for lg in [MEMORY_LOG_GROUP]:
        log(f"deleting log group {lg}")
        safe(logs.delete_log_group, logGroupName=lg)
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/"):
        for lg in page["logGroups"]:
            if RUNTIME_NAME in lg["logGroupName"]:
                log(f"deleting log group {lg['logGroupName']}")
                safe(logs.delete_log_group, logGroupName=lg["logGroupName"])

    # IAM role
    try:
        for p in iam.list_role_policies(RoleName=ROLE_NAME)["PolicyNames"]:
            safe(iam.delete_role_policy, RoleName=ROLE_NAME, PolicyName=p)
        log(f"deleting role {ROLE_NAME}")
        safe(iam.delete_role, RoleName=ROLE_NAME)
    except ClientError:
        log("role already gone")

    # ECR repo
    log(f"deleting ECR repo {ECR_REPO}")
    safe(ecr.delete_repository, repositoryName=ECR_REPO, force=True)

    log("teardown complete")


if __name__ == "__main__":
    main()

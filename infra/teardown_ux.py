"""Teardown for the obsdemo hosted UX ONLY (never the core runtime/memory stack).

PREVIEW BY DEFAULT: lists exactly what it would delete (exact-name matching,
paginated listings) and exits. Destructive only with --confirm.

Run:  AWS_PROFILE=<profile> python infra/teardown_ux.py [--confirm]
"""

import json
import os
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ACCOUNT_SUFFIX, PROFILE, REGION

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
sts = session.client("sts")
s3 = session.client("s3")
cf = session.client("cloudfront")
cognito = session.client("cognito-idp")
apigw = session.client("apigatewayv2")
lam = session.client("lambda")
iam = session.client("iam")
logs = session.client("logs")

CONFIRM = "--confirm" in sys.argv

# exact names / prefixes owned by the UX layer only
LAMBDA_NAME = "obsdemo-ux-backend"
ROLE_NAME = "obsdemo-ux-backend-role"
API_NAME = "obsdemo-ux-api"
POOL_NAME = "obsdemo-ux-users"
OAC_NAME = "obsdemo-ux-oac"
DIST_COMMENT = "obsdemo-ux-site"
BUCKET_PREFIX = "obsdemo-ux-site-"
LOG_GROUP = "/aws/lambda/obsdemo-ux-backend"


def log(msg: str) -> None:
    print(f"[teardown_ux] {msg}", flush=True)


def guard_account() -> None:
    acct = sts.get_caller_identity()["Account"]
    if ACCOUNT_SUFFIX and not acct.endswith(ACCOUNT_SUFFIX):
        raise SystemExit("ABORT: wrong AWS account (suffix mismatch with "
                         "OBSDEMO_ACCOUNT_SUFFIX)")
    log(f"account verified: …{acct[-4:]} | mode: {'DELETE' if CONFIRM else 'PREVIEW'}")


def find_targets() -> dict:
    t = {}

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]
               if b["Name"].startswith(BUCKET_PREFIX)]
    t["buckets"] = buckets

    dists = []
    for page in cf.get_paginator("list_distributions").paginate():
        for d in page.get("DistributionList", {}).get("Items", []) or []:
            if d.get("Comment") == DIST_COMMENT:
                dists.append({"id": d["Id"], "domain": d["DomainName"],
                              "enabled": d["Enabled"]})
    t["distributions"] = dists

    oacs = [{"id": o["Id"], "name": o["Name"]}
            for o in (cf.list_origin_access_controls()
                      .get("OriginAccessControlList", {}).get("Items", []) or [])
            if o["Name"] == OAC_NAME]
    t["oacs"] = oacs

    pools = []
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == POOL_NAME:
                desc = cognito.describe_user_pool(UserPoolId=p["Id"])["UserPool"]
                pools.append({"id": p["Id"], "domain": desc.get("Domain")})
    t["user_pools"] = pools

    apis = []
    for page in apigw.get_paginator("get_apis").paginate():
        apis += [{"id": a["ApiId"], "name": a["Name"]}
                 for a in page["Items"] if a["Name"] == API_NAME]
    t["apis"] = apis

    try:
        lam.get_function(FunctionName=LAMBDA_NAME)
        t["lambdas"] = [LAMBDA_NAME]
    except lam.exceptions.ResourceNotFoundException:
        t["lambdas"] = []

    try:
        iam.get_role(RoleName=ROLE_NAME)
        t["roles"] = [ROLE_NAME]
    except iam.exceptions.NoSuchEntityException:
        t["roles"] = []

    groups = []
    for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix=LOG_GROUP):
        groups += [g["logGroupName"] for g in page["logGroups"]
                   if g["logGroupName"] == LOG_GROUP]
    t["log_groups"] = groups
    return t


def preview(t: dict) -> None:
    log("would delete (UX layer ONLY — core runtime/memory untouched):")
    for k, v in t.items():
        log(f"  {k}: {v if v else '(none)'}")
    log("re-run with --confirm to delete.")


def delete(t: dict) -> None:
    for api in t["apis"]:
        apigw.delete_api(ApiId=api["id"])
        log(f"deleted api {api['id']}")
    for fn in t["lambdas"]:
        lam.delete_function(FunctionName=fn)
        log(f"deleted lambda {fn}")
    for role in t["roles"]:
        for pn in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=pn)
        iam.delete_role(RoleName=role)
        log(f"deleted role {role}")
    for g in t["log_groups"]:
        logs.delete_log_group(logGroupName=g)
        log(f"deleted log group {g}")
    for p in t["user_pools"]:
        if p.get("domain"):
            cognito.delete_user_pool_domain(Domain=p["domain"], UserPoolId=p["id"])
            log(f"deleted pool domain {p['domain']}")
        cognito.delete_user_pool(UserPoolId=p["id"])
        log(f"deleted user pool {p['id']}")
    for d in t["distributions"]:
        cfg = cf.get_distribution_config(Id=d["id"])
        etag, dc = cfg["ETag"], cfg["DistributionConfig"]
        if dc["Enabled"]:
            dc["Enabled"] = False
            etag = cf.update_distribution(Id=d["id"], IfMatch=etag,
                                          DistributionConfig=dc)["ETag"]
            log(f"disabled distribution {d['id']} — waiting for Deployed")
        for _ in range(60):
            st = cf.get_distribution(Id=d["id"])["Distribution"]["Status"]
            if st == "Deployed":
                break
            time.sleep(30)
        cf.delete_distribution(Id=d["id"], IfMatch=cf.get_distribution_config(
            Id=d["id"])["ETag"])
        log(f"deleted distribution {d['id']}")
    for o in t["oacs"]:
        etag = cf.get_origin_access_control(Id=o["id"])["ETag"]
        cf.delete_origin_access_control(Id=o["id"], IfMatch=etag)
        log(f"deleted OAC {o['name']}")
    for b in t["buckets"]:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=b):
            objs = ([{"Key": v["Key"], "VersionId": v["VersionId"]}
                     for v in page.get("Versions", [])] +
                    [{"Key": v["Key"], "VersionId": v["VersionId"]}
                     for v in page.get("DeleteMarkers", [])])
            if objs:
                s3.delete_objects(Bucket=b, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=b)
        log(f"deleted bucket {b}")


def main() -> None:
    guard_account()
    t = find_targets()
    if not CONFIRM:
        preview(t)
        return
    print("Deleting the resources listed above in 5s — Ctrl-C to abort.")
    time.sleep(5)
    delete(t)
    log("teardown complete (UX layer only).")


if __name__ == "__main__":
    main()

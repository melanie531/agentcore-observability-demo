"""Deploy the obsdemo hosted UX: S3+CloudFront(OAC) + Cognito + HTTP API + Lambda.

Idempotent: every resource is looked up by exact obsdemo-ux-* name before
creation; policies are merged by statement Sid, never blind-overwritten.

Run:  AWS_PROFILE=<profile> python infra/deploy_ux.py
State is written to OBSDEMO_UX_STATE_FILE (default ./build/cloud-ux-state.json,
keep it OUT of the repo). Prints a masked summary only.

Creates ONE synthetic test user (no real email, MessageAction=SUPPRESS,
no password set here — verification scripts set one in-process).
"""

import hashlib
import json
import os
import secrets
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (ACCOUNT_SUFFIX, LOG_RETENTION_DAYS, PROFILE, REGION,
                    STATE_FILE, TAGS, UX_STATE_FILE)

SITE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ux-cloud", "site")
BACKEND_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ux-cloud", "backend", "handler.py")

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
sts = session.client("sts")
s3 = session.client("s3")
cf = session.client("cloudfront")
cognito = session.client("cognito-idp")
apigw = session.client("apigatewayv2")
lam = session.client("lambda")
iam = session.client("iam")
logs = session.client("logs")

TAG_LIST = [{"Key": k, "Value": v} for k, v in TAGS.items()]


def log(msg: str) -> None:
    print(f"[deploy_ux] {msg}", flush=True)


def guard_account() -> str:
    acct = sts.get_caller_identity()["Account"]
    if ACCOUNT_SUFFIX and not acct.endswith(ACCOUNT_SUFFIX):
        raise SystemExit("ABORT: wrong AWS account (suffix mismatch with "
                         "OBSDEMO_ACCOUNT_SUFFIX)")
    log(f"account verified: …{acct[-4:]}")
    return acct


def load_state() -> dict:
    if os.path.exists(UX_STATE_FILE):
        with open(UX_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(UX_STATE_FILE)), exist_ok=True)
    with open(UX_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def suffix_for(acct: str) -> str:
    # deterministic, non-reversible 8-char suffix (no account digits leaked)
    return hashlib.sha256(f"obsdemo-ux-{acct}".encode()).hexdigest()[:8]


# ---------------- S3 site bucket ----------------
def ensure_bucket(name: str) -> None:
    try:
        s3.head_bucket(Bucket=name)
        log(f"bucket exists: {name}")
    except s3.exceptions.ClientError:
        log(f"creating bucket {name}")
        s3.create_bucket(Bucket=name,
                         CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        })
    s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": TAG_LIST})


def merge_bucket_policy(bucket: str, dist_arn: str) -> None:
    stmt = {
        "Sid": "obsdemo-ux-allow-cloudfront-oac",
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket}/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}},
    }
    try:
        existing = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except s3.exceptions.ClientError:
        existing = {"Version": "2012-10-17", "Statement": []}
    others = [s for s in existing["Statement"] if s.get("Sid") != stmt["Sid"]]
    existing["Statement"] = others + [stmt]
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(existing))
    log(f"bucket policy merged on {bucket} (OAC statement, {len(others)} other stmts kept)")


# ---------------- CloudFront ----------------
def ensure_oac(name: str) -> str:
    items = cf.list_origin_access_controls().get(
        "OriginAccessControlList", {}).get("Items", []) or []
    for it in items:
        if it["Name"] == name:
            log(f"OAC exists: {name}")
            return it["Id"]
    r = cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": name, "Description": "obsdemo hosted UX site OAC",
        "SigningProtocol": "sigv4", "SigningBehavior": "always",
        "OriginAccessControlOriginType": "s3",
    })
    log(f"OAC created: {name}")
    return r["OriginAccessControl"]["Id"]


def ensure_distribution(bucket: str, oac_id: str, acct: str) -> dict:
    comment = "obsdemo-ux-site"
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for d in page.get("DistributionList", {}).get("Items", []) or []:
            if d.get("Comment") == comment:
                log(f"distribution exists: {d['Id']} ({d['DomainName']})")
                return {"id": d["Id"], "domain": d["DomainName"], "arn": d["ARN"]}
    origin_domain = f"{bucket}.s3.{REGION}.amazonaws.com"
    cfg = {
        "CallerReference": f"obsdemo-ux-{int(time.time())}",
        "Comment": comment,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "PriceClass": "PriceClass_100",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "site-s3", "DomainName": origin_domain,
            "OriginAccessControlId": oac_id,
            "S3OriginConfig": {"OriginAccessIdentity": ""},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "site-s3",
            "ViewerProtocolPolicy": "redirect-to-https",
            # managed CachingOptimized policy
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "Compress": True,
        },
    }
    r = cf.create_distribution_with_tags(DistributionConfigWithTags={
        "DistributionConfig": cfg, "Tags": {"Items": TAG_LIST}})
    d = r["Distribution"]
    log(f"distribution created: {d['Id']} ({d['DomainName']}) — deploying (takes minutes)")
    return {"id": d["Id"], "domain": d["DomainName"], "arn": d["ARN"]}


# ---------------- Cognito ----------------
def ensure_user_pool() -> str:
    name = "obsdemo-ux-users"
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        for p in page["UserPools"]:
            if p["Name"] == name:
                log(f"user pool exists: {p['Id']}")
                return p["Id"]
    r = cognito.create_user_pool(
        PoolName=name,
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        AliasAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        MfaConfiguration="OFF",
        DeletionProtection="INACTIVE",
        UserPoolTags=TAGS,
        Policies={"PasswordPolicy": {
            "MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
            "RequireNumbers": True, "RequireSymbols": False,
        }},
    )
    pool_id = r["UserPool"]["Id"]
    log(f"user pool created: {pool_id}")
    return pool_id


def ensure_pool_domain(pool_id: str, domain_prefix: str) -> str:
    try:
        d = cognito.describe_user_pool_domain(Domain=domain_prefix)
        desc = d.get("DomainDescription") or {}
        if desc.get("UserPoolId") == pool_id:
            log(f"pool domain exists: {domain_prefix}")
            return domain_prefix
        if desc.get("UserPoolId"):
            raise SystemExit(f"ABORT: domain {domain_prefix} owned by another pool")
    except cognito.exceptions.ClientError:
        pass
    cognito.create_user_pool_domain(Domain=domain_prefix, UserPoolId=pool_id)
    log(f"pool domain created: {domain_prefix}")
    return domain_prefix


def ensure_clients(pool_id: str, cf_domain: str) -> tuple[str, str]:
    url = f"https://{cf_domain}/"
    web_id = test_id = None
    for page in cognito.get_paginator("list_user_pool_clients").paginate(
            UserPoolId=pool_id, MaxResults=60):
        for c in page["UserPoolClients"]:
            if c["ClientName"] == "obsdemo-ux-web":
                web_id = c["ClientId"]
            elif c["ClientName"] == "obsdemo-ux-test":
                test_id = c["ClientId"]
    if web_id:
        log(f"web client exists: {web_id} — updating URLs to {url}")
        cognito.update_user_pool_client(
            UserPoolId=pool_id, ClientId=web_id, ClientName="obsdemo-ux-web",
            CallbackURLs=[url], LogoutURLs=[url],
            AllowedOAuthFlows=["code"], AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=["openid", "email"],
            SupportedIdentityProviders=["COGNITO"],
            ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
        )
    else:
        r = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName="obsdemo-ux-web", GenerateSecret=False,
            CallbackURLs=[url], LogoutURLs=[url],
            AllowedOAuthFlows=["code"], AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=["openid", "email"],
            SupportedIdentityProviders=["COGNITO"],
            ExplicitAuthFlows=["ALLOW_REFRESH_TOKEN_AUTH"],
            PreventUserExistenceErrors="ENABLED",
        )
        web_id = r["UserPoolClient"]["ClientId"]
        log(f"web client created: {web_id}")
    if test_id:
        log(f"test client exists: {test_id}")
    else:
        r = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName="obsdemo-ux-test", GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            PreventUserExistenceErrors="ENABLED",
        )
        test_id = r["UserPoolClient"]["ClientId"]
        log(f"test client created: {test_id} (USER_PASSWORD_AUTH, for headless tests)")
    return web_id, test_id


def ensure_test_user(pool_id: str, state: dict) -> str:
    username = state.get("test_username")
    if username:
        try:
            cognito.admin_get_user(UserPoolId=pool_id, Username=username)
            log(f"test user exists: {username}")
            return username
        except cognito.exceptions.UserNotFoundException:
            pass
    username = f"obsdemo-tester-{secrets.token_hex(4)}"
    cognito.admin_create_user(
        UserPoolId=pool_id, Username=username,
        MessageAction="SUPPRESS",  # synthetic user: no email, no invite
    )
    log(f"test user created: {username} (no password set; tests set one in-process)")
    return username


# ---------------- Lambda ----------------
ASSUME_ROLE_DOC = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}],
}


def lambda_policy_doc(acct: str, core: dict) -> dict:
    runtime_arn = core["runtime_arn"]
    memory_arn = core["memory_arn"]
    rt_group = core["runtime_log_group_hint"]
    mem_group = core["memory_log_group"]
    lg = lambda g: f"arn:aws:logs:{REGION}:{acct}:log-group:{g}"
    own_group = "/aws/lambda/obsdemo-ux-backend"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "InvokeRuntime", "Effect": "Allow",
             "Action": "bedrock-agentcore:InvokeAgentRuntime",
             "Resource": [runtime_arn, f"{runtime_arn}/runtime-endpoint/*"]},
            {"Sid": "MemoryRead", "Effect": "Allow",
             "Action": ["bedrock-agentcore:ListEvents",
                        "bedrock-agentcore:ListSessions",
                        "bedrock-agentcore:RetrieveMemoryRecords"],
             "Resource": [memory_arn]},
            {"Sid": "InsightsStart", "Effect": "Allow",
             "Action": ["logs:StartQuery", "logs:FilterLogEvents"],
             "Resource": [lg("aws/spans"), lg("aws/spans") + ":*",
                          lg(rt_group), lg(rt_group) + ":*",
                          lg(mem_group), lg(mem_group) + ":*"]},
            {"Sid": "InsightsResults", "Effect": "Allow",
             "Action": ["logs:GetQueryResults", "logs:StopQuery"],
             "Resource": "*"},
            {"Sid": "OwnLogs", "Effect": "Allow",
             "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": [lg(own_group), lg(own_group) + ":*"]},
        ],
    }


def ensure_lambda_role(acct: str, core: dict) -> str:
    role_name = "obsdemo-ux-backend-role"
    try:
        r = iam.get_role(RoleName=role_name)
        log(f"lambda role exists: {role_name}")
    except iam.exceptions.NoSuchEntityException:
        r = iam.create_role(RoleName=role_name,
                            AssumeRolePolicyDocument=json.dumps(ASSUME_ROLE_DOC),
                            Tags=TAG_LIST)
        log(f"lambda role created: {role_name}")
        time.sleep(10)  # IAM propagation
    iam.put_role_policy(RoleName=role_name, PolicyName="obsdemo-ux-backend-policy",
                        PolicyDocument=json.dumps(lambda_policy_doc(acct, core)))
    log("lambda inline policy set (least privilege)")
    return r["Role"]["Arn"]


def ensure_log_group(name: str) -> None:
    pages = logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name)
    for page in pages:
        for g in page["logGroups"]:
            if g["logGroupName"] == name:
                logs.put_retention_policy(logGroupName=name,
                                          retentionInDays=LOG_RETENTION_DAYS)
                log(f"log group exists: {name} (retention enforced)")
                return
    logs.create_log_group(logGroupName=name, tags=TAGS)
    logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)
    log(f"log group created: {name} (retention {LOG_RETENTION_DAYS}d)")


def build_lambda_zip() -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("handler.py")
        info.external_attr = 0o644 << 16
        with open(BACKEND_FILE) as f:
            z.writestr(info, f.read())
    return buf.getvalue()


def ensure_lambda(role_arn: str, core: dict) -> str:
    fn = "obsdemo-ux-backend"
    env = {"Variables": {
        "OBSDEMO_RUNTIME_ARN": core["runtime_arn"],
        "OBSDEMO_MEMORY_ID": core["memory_id"],
        "OBSDEMO_MEMORY_LOG_GROUP": core["memory_log_group"],
        "OBSDEMO_SPANS_LOG_GROUP": "aws/spans",
    }}
    code = build_lambda_zip()
    try:
        lam.get_function(FunctionName=fn)
        log(f"lambda exists: {fn} — updating code + config")
        lam.update_function_code(FunctionName=fn, ZipFile=code)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=fn)
        lam.update_function_configuration(
            FunctionName=fn, Runtime="python3.12", Handler="handler.lambda_handler",
            Role=role_arn, Timeout=30, MemorySize=256, Environment=env)
        waiter.wait(FunctionName=fn)
    except lam.exceptions.ResourceNotFoundException:
        r = lam.create_function(
            FunctionName=fn, Runtime="python3.12", Handler="handler.lambda_handler",
            Role=role_arn, Code={"ZipFile": code}, Timeout=30, MemorySize=256,
            Architectures=["arm64"], Environment=env, Tags=TAGS,
            LoggingConfig={"LogGroup": "/aws/lambda/obsdemo-ux-backend"},
        )
        log(f"lambda created: {fn}")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    return lam.get_function(FunctionName=fn)["Configuration"]["FunctionArn"]


# ---------------- API Gateway HTTP API ----------------
ROUTES = [
    ("POST", "/api/chat"),
    ("GET", "/api/sessions"),
    ("GET", "/api/telemetry/trace"),
    ("GET", "/api/telemetry/memory-logs"),
    ("GET", "/api/health"),
]


def ensure_api(acct: str, cf_domain: str, pool_id: str,
               audiences: list, lambda_arn: str) -> dict:
    name = "obsdemo-ux-api"
    api_id = None
    for page in apigw.get_paginator("get_apis").paginate():
        for a in page["Items"]:
            if a["Name"] == name:
                api_id = a["ApiId"]
    cors = {
        "AllowOrigins": [f"https://{cf_domain}"],
        "AllowMethods": ["GET", "POST", "OPTIONS"],
        "AllowHeaders": ["authorization", "content-type"],
        "MaxAge": 600,
    }
    if api_id:
        log(f"api exists: {api_id} — updating CORS")
        apigw.update_api(ApiId=api_id, CorsConfiguration=cors)
    else:
        r = apigw.create_api(Name=name, ProtocolType="HTTP",
                             CorsConfiguration=cors, Tags=TAGS)
        api_id = r["ApiId"]
        log(f"api created: {api_id}")

    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"
    auth_id = None
    for a in apigw.get_authorizers(ApiId=api_id)["Items"]:
        if a["Name"] == "obsdemo-ux-jwt":
            auth_id = a["AuthorizerId"]
    jwt_cfg = {"Audience": audiences, "Issuer": issuer}
    if auth_id:
        apigw.update_authorizer(ApiId=api_id, AuthorizerId=auth_id,
                                JwtConfiguration=jwt_cfg)
        log(f"jwt authorizer updated: {auth_id}")
    else:
        r = apigw.create_authorizer(
            ApiId=api_id, Name="obsdemo-ux-jwt", AuthorizerType="JWT",
            IdentitySource=["$request.header.Authorization"],
            JwtConfiguration=jwt_cfg)
        auth_id = r["AuthorizerId"]
        log(f"jwt authorizer created: {auth_id}")

    int_id = None
    for i in apigw.get_integrations(ApiId=api_id)["Items"]:
        if i.get("IntegrationUri", "").endswith(lambda_arn.split(":")[-1]) or \
           i.get("IntegrationUri") == lambda_arn:
            int_id = i["IntegrationId"]
    if not int_id:
        r = apigw.create_integration(
            ApiId=api_id, IntegrationType="AWS_PROXY",
            IntegrationUri=lambda_arn, PayloadFormatVersion="2.0")
        int_id = r["IntegrationId"]
        log(f"lambda integration created: {int_id}")

    existing_routes = {r["RouteKey"]: r for r in apigw.get_routes(ApiId=api_id)["Items"]}
    for method, path in ROUTES:
        key = f"{method} {path}"
        kwargs = dict(ApiId=api_id, RouteKey=key,
                      Target=f"integrations/{int_id}",
                      AuthorizationType="JWT", AuthorizerId=auth_id)
        if key in existing_routes:
            apigw.update_route(RouteId=existing_routes[key]["RouteId"], **kwargs)
        else:
            apigw.create_route(**kwargs)
    log(f"routes ensured (JWT on every route): {[f'{m} {p}' for m, p in ROUTES]}")

    stages = apigw.get_stages(ApiId=api_id)["Items"]
    if not any(s["StageName"] == "$default" for s in stages):
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True, Tags=TAGS)
        log("default stage created (auto-deploy)")

    try:
        lam.add_permission(
            FunctionName="obsdemo-ux-backend", StatementId="obsdemo-ux-apigw-invoke",
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{acct}:{api_id}/*/*/api/*")
        log("lambda invoke permission added for API GW")
    except lam.exceptions.ResourceConflictException:
        log("lambda invoke permission already present")

    endpoint = f"https://{api_id}.execute-api.{REGION}.amazonaws.com"
    return {"api_id": api_id, "endpoint": endpoint, "authorizer_id": auth_id}


# ---------------- Site upload ----------------
CONTENT_TYPES = {".html": "text/html", ".js": "application/javascript",
                 ".css": "text/css", ".json": "application/json"}


def upload_site(bucket: str, cfg: dict) -> None:
    config_js = (
        "// generated by infra/deploy_ux.py — public identifiers only\n"
        "window.OBSDEMO_CONFIG = " + json.dumps({
            "cognitoDomain": cfg["cognito_domain_host"],
            "clientId": cfg["web_client_id"],
            "apiUrl": cfg["api_endpoint"],
        }, indent=2) + ";\n")
    s3.put_object(Bucket=bucket, Key="config.js", Body=config_js.encode(),
                  ContentType="application/javascript", CacheControl="no-cache")
    for fname in os.listdir(SITE_DIR):
        path = os.path.join(SITE_DIR, fname)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(fname)[1]
        with open(path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=fname, Body=f.read(),
                          ContentType=CONTENT_TYPES.get(ext, "application/octet-stream"),
                          CacheControl="no-cache")
    log(f"site uploaded to s3://{bucket} (index.html, app.js, config.js)")


def invalidate(dist_id: str) -> None:
    cf.create_invalidation(DistributionId=dist_id, InvalidationBatch={
        "CallerReference": f"obsdemo-ux-{int(time.time())}",
        "Paths": {"Quantity": 1, "Items": ["/*"]}})
    log("cloudfront invalidation issued (/*)")


def main() -> None:
    acct = guard_account()
    core = json.load(open(STATE_FILE))
    state = load_state()
    sfx = suffix_for(acct)

    bucket = f"obsdemo-ux-site-{sfx}"
    ensure_bucket(bucket)

    oac_id = ensure_oac("obsdemo-ux-oac")
    dist = ensure_distribution(bucket, oac_id, acct)
    merge_bucket_policy(bucket, dist["arn"])

    pool_id = ensure_user_pool()
    domain_prefix = f"obsdemo-ux-{sfx}"
    ensure_pool_domain(pool_id, domain_prefix)
    web_id, test_id = ensure_clients(pool_id, dist["domain"])
    test_username = ensure_test_user(pool_id, state)

    ensure_log_group("/aws/lambda/obsdemo-ux-backend")
    role_arn = ensure_lambda_role(acct, core)
    lambda_arn = ensure_lambda(role_arn, core)

    api = ensure_api(acct, dist["domain"], pool_id, [web_id, test_id], lambda_arn)

    cognito_domain_host = f"{domain_prefix}.auth.{REGION}.amazoncognito.com"
    cfg = {
        "bucket": bucket,
        "distribution_id": dist["id"],
        "distribution_domain": dist["domain"],
        "distribution_arn": dist["arn"],
        "cloudfront_url": f"https://{dist['domain']}/",
        "user_pool_id": pool_id,
        "cognito_domain_host": cognito_domain_host,
        "web_client_id": web_id,
        "test_client_id": test_id,
        "test_username": test_username,
        "api_id": api["api_id"],
        "api_endpoint": api["endpoint"],
        "authorizer_id": api["authorizer_id"],
        "lambda_arn": lambda_arn,
        "region": REGION,
    }
    upload_site(bucket, cfg)
    invalidate(dist["id"])
    state.update(cfg)
    save_state(state)

    log("---- summary (masked) ----")
    log(f"site:    https://{dist['domain']}/")
    log(f"api:     {api['endpoint']}")
    log(f"cognito: {cognito_domain_host} pool={pool_id} web_client={web_id}")
    log(f"test:    client={test_id} user={test_username}")
    log(f"state -> {UX_STATE_FILE}")
    log("NOTE: CloudFront deployment takes ~5-15 min after first create.")


if __name__ == "__main__":
    main()

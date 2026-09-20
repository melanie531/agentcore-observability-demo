"""Provision (idempotently) a dedicated username/password demo login — NO email.

Creates a Cognito user with AdminCreateUser MessageAction=SUPPRESS and zero
attributes: no email attribute, no invitation or verification email, and no
dependence on any personal account. A strong random password is generated
in-process, applied with AdminSetUserPassword (permanent), persisted ONLY to
an AWS Secrets Manager secret via the SDK, and verified with a real
USER_PASSWORD_AUTH call. Nothing secret is ever printed, written to disk, or
passed on a command line — output is statuses only.

Retrieve the password yourself (never share it in chat/logs):
    AWS Console -> Secrets Manager -> <region> -> obsdemo/<username>-login
    -> "Retrieve secret value"

Run:  AWS_PROFILE=<profile> python infra/provision_demo_user.py [username]
Reads pool/client ids from the UX state file written by deploy_ux.py.
Idempotent: an existing user/secret pair is verified, never rotated.
"""

import json
import secrets
import string
import sys

import boto3

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from config import PROFILE, REGION, UX_STATE_FILE  # noqa: E402

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "demo-presenter"
SECRET_NAME = f"obsdemo/{USERNAME}-login"

state = json.load(open(UX_STATE_FILE))
POOL_ID = state["user_pool_id"]
TEST_CLIENT_ID = state["test_client_id"]

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
idp = session.client("cognito-idp")
sm = session.client("secretsmanager")


def user_exists() -> bool:
    try:
        idp.admin_get_user(UserPoolId=POOL_ID, Username=USERNAME)
        return True
    except idp.exceptions.UserNotFoundException:
        return False


def secret_exists() -> bool:
    try:
        sm.describe_secret(SecretId=SECRET_NAME)
        return True
    except sm.exceptions.ResourceNotFoundException:
        return False


def generate_password() -> str:
    pol = idp.describe_user_pool(UserPoolId=POOL_ID)["UserPool"]["Policies"]["PasswordPolicy"]
    length = max(int(pol.get("MinimumLength", 12)) + 8, 20)
    symbols = "!@#$%^*-_+=."
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice(symbols),
    ]
    chars += [rng.choice(string.ascii_letters + string.digits + symbols)
              for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def verify_auth(username: str, password: str) -> None:
    r = idp.initiate_auth(
        ClientId=TEST_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    ar = r.get("AuthenticationResult", {})
    got = {k: bool(ar.get(k)) for k in ("IdToken", "AccessToken", "RefreshToken")}
    print(f"initiate_auth: challenge={r.get('ChallengeName') or 'none'} tokens_present={got}")


def main() -> None:
    have_user, have_secret = user_exists(), secret_exists()

    if have_user and have_secret:
        # Reuse, never churn: verify the stored credential still authenticates.
        cred = json.loads(sm.get_secret_value(SecretId=SECRET_NAME)["SecretString"])
        print(f"existing user + secret found for {USERNAME}; verifying without rotation")
        verify_auth(cred["username"], cred["password"])
        del cred
    elif have_user or have_secret:
        raise SystemExit(
            f"inconsistent state: user_exists={have_user} secret_exists={have_secret} — "
            f"resolve manually (never auto-delete)")
    else:
        password = generate_password()
        idp.admin_create_user(UserPoolId=POOL_ID, Username=USERNAME,
                              MessageAction="SUPPRESS")
        print(f"admin_create_user: OK username={USERNAME} (SUPPRESS, no attributes, no email)")
        idp.admin_set_user_password(UserPoolId=POOL_ID, Username=USERNAME,
                                    Permanent=True, Password=password)
        print("admin_set_user_password: OK (permanent)")
        sm.create_secret(
            Name=SECRET_NAME,
            Description=f"obsdemo UX login for {USERNAME} (CloudFront + Cognito hosted UI)",
            SecretString=json.dumps({"username": USERNAME, "password": password}),
            Tags=[{"Key": "project", "Value": "agentcore-observability-demo"}],
        )
        print(f"create_secret: OK name={SECRET_NAME}")
        verify_auth(USERNAME, password)
        del password

    u = idp.admin_get_user(UserPoolId=POOL_ID, Username=USERNAME)
    attrs = [a["Name"] for a in u.get("UserAttributes", [])]
    print(f"user: status={u['UserStatus']} enabled={u['Enabled']} attributes={attrs}")
    print(f"password retrieval: Secrets Manager ({REGION}) secret '{SECRET_NAME}'")


if __name__ == "__main__":
    main()

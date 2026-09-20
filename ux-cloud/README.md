# obsdemo hosted UX (CloudFront + Cognito + API Gateway + Lambda)

Cloud-hosted variant of the local `ux/` chat. The static shell is served from a
private S3 bucket through CloudFront (OAC); all data flows through an API
Gateway HTTP API where **every route** requires a Cognito JWT.

```
browser ──HTTPS──> CloudFront ──OAC──> S3 (index.html, app.js, config.js)
   │
   └──Bearer JWT──> API Gateway (JWT authorizer) ──> Lambda obsdemo-ux-backend
                                                        ├─ InvokeAgentRuntime (AgentCore Runtime)
                                                        ├─ ListEvents / RetrieveMemoryRecords (Memory)
                                                        └─ Logs Insights (aws/spans, memory vended logs)
```

## Security model

- **Login**: Cognito managed login, authorization-code grant + PKCE only
  (public client, no secret, no implicit flow). Tokens live in
  `sessionStorage` only; Logout hits the Cognito logout endpoint.
- **Actor binding**: the Lambda reads the `sub` claim from the JWT validated
  by the API Gateway authorizer and derives `actor_id = u-{sub}-{traveler}`
  server-side. `traveler` comes from a fixed allowlist (`ava`, `blake`).
  A client-supplied `actor_id` is rejected with 400. Sessions are derived as
  `{sub[:12]}-{label}`, so no caller can address another user's sessions or
  the pre-existing local demo actors.
- **Telemetry projection**: the trace endpoint runs a fixed Logs Insights
  query template with an allowlisted field projection (span name/ids/status/
  duration + `gen_ai.*` + `obsdemo.*`). Raw span JSON and `aws.auth.*`
  attributes are never returned. Trace ownership is checked against the
  caller's derived actor prefix.

## How config.js is generated

`infra/deploy_ux.py` writes `config.js` into the site bucket at deploy time:

```js
window.OBSDEMO_CONFIG = {
  "cognitoDomain": "<domain-prefix>.auth.<region>.amazoncognito.com",
  "clientId": "<web app client id>",
  "apiUrl": "https://<api-id>.execute-api.<region>.amazonaws.com"
};
```

These are public identifiers (safe to serve to any browser). `config.js` is
not tracked in git — only generated at deploy time.

## Deploy / teardown

```bash
export AWS_PROFILE=<your-profile>
python infra/deploy_ux.py               # idempotent, prints masked summary
python infra/teardown_ux.py             # PREVIEW: lists what would be deleted
python infra/teardown_ux.py --confirm   # actually deletes the UX layer only
```

The deploy needs the core stack state file (`OBSDEMO_STATE_FILE`, default
`./build/deploy-state.json`) produced by `infra/deploy.py`. UX state is written
to `OBSDEMO_UX_STATE_FILE` (default `./build/cloud-ux-state.json`). Keep both
out of git.

## Creating a real user (admin invite)

Self-signup is disabled. Invite users from the CLI — Cognito emails a
temporary password and forces a change on first login; no password ever
touches your shell or files:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <pool-id> \
  --username <email> \
  --user-attributes Name=email,Value=<email> Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

## Files

- `site/index.html` — static shell (chat + telemetry panel)
- `site/app.js` — PKCE auth, chat, trace/memory-log viewers (all rendering via
  `textContent`; nothing is inserted as HTML)
- `backend/handler.py` — Lambda handler (single file, stdlib + boto3 only)

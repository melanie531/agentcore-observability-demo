# REDACTION BRIEF — remove AWS auth identifiers from demo spans (single builder, bounded)

Context: independent review confirmed the obsdemo stack works live, but flagged that exported spans
in `aws/spans` contain the attribute `attributes.aws.auth.account.access_key` holding real temporary
STS access-key IDs (identifiers, NOT secret keys/session tokens). This must be suppressed before the
customer console walkthrough. Treat as presentation/redaction fix, not credential leak response:
**no rotation/revocation, no deletion of historical spans, no account-level changes.**

Hard rules (same as BRIEF.md): AWS profile `platform-dev-takeover` + us-west-2 on every call; touch
ONLY obsdemo resources; never print the access-key values anywhere (logs, evidence, report — refer to
the attribute name only); never echo the full account ID into share docs.

## Steps (bounded — aim < 40 min, ≤ 8 runtime invocations)

1. **Research the official source of the attribute** before coding. Check, in order:
   - aws-opentelemetry-distro (ADOT Python) GitHub source/docs (aws-otel-python-instrumentation repo)
     for `aws.auth.account.access_key` — find which component emits it and whether a documented
     config/env var disables it.
   - OpenTelemetry Python docs for officially supported attribute-filtering hooks (e.g. botocore
     instrumentation `request_hook/response_hook`, `OTEL_*` env vars, SpanProcessor API).
   Record exact URLs/file references in `build/share/SOURCES.md` (append a "Redaction" section).
   Do NOT invent config options. If no pure-config switch exists, implement redaction in agent code
   using the **public OTel SDK API** (e.g. a SpanProcessor registered on the tracer provider, or the
   documented botocore instrumentation hooks) that removes/overwrites the sensitive attribute(s)
   before export. Redact at minimum `aws.auth.account.access_key`; also scan one raw span locally for
   any other `aws.auth.*` credential-identifier attributes and redact those too.
2. Implement minimally in `agent/` (surgical; keep everything else identical). Commit.
3. Rebuild the container (linux/arm64), push to the existing `obsdemo-travel-agent` ECR repo with a
   new tag (e.g. `redacted-1`), and `update_agent_runtime` on `obsdemo_travel_agent-r7rze150yd` ONLY.
   Wait READY. Record the new runtime version.
4. Generate fresh verification traffic: 3–5 invocations (normal + one slow + one error) with fresh
   session IDs, note exact UTC start/end of this **clean window**.
5. Verify via Logs Insights on `aws/spans` restricted to the clean window + your new trace IDs:
   - count of spans containing `aws.auth.account.access_key` == 0 (query for presence; do not project values)
   - model/tool spans still present with gen_ai attrs (redaction must not break telemetry)
   - agent still answers correctly (STM/memory wiring unbroken; check memory_provenance still real)
6. Write `build/local/REDACTION-RESULT.md`: what emits the attribute (with source link), the official
   mechanism used, new runtime version, clean-window UTC start, verification query strings + result
   counts, and the statement that historical spans before <clean-window-start> still contain the
   identifiers (retention on `aws/spans` is account-wide 30d; no deletion attempted).
7. Append one line per milestone to `build/local/PROGRESS.log`. When done run:
   `openclaw system event --text "Done: span redaction deployed+verified, see build/local/REDACTION-RESULT.md" --mode now`

If genuinely blocked, write `build/local/REDACTION-BLOCKER.md` with exact error and stop safely.

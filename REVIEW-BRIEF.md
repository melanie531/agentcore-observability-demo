# REVIEW BRIEF — Independent read-only acceptance rerun (Codex, global.openai.gpt-6-astra)

You are the independent read-only reviewer for the `obsdemo` AgentCore observability demo.
The builder (a separate Claude Code worker) has finished; your job is to independently verify, not to fix.

## Hard rules
- **READ-ONLY on code and infrastructure**: do not edit/create/delete files in the project (except your own report), do not create/modify/delete any AWS resource, IAM policy, log group, or delivery. No teardown.
- Allowed cloud writes: ONLY invoking the deployed demo runtime (`InvokeAgentRuntime` on runtime names prefixed `obsdemo`), memory data-plane READS (list_events / retrieve_memory_records on memory prefixed `obsdemo`), and CloudWatch Logs Insights queries. Budget: max 10 runtime invocations, small prompts.
- AWS: `AWS_PROFILE=platform-dev-takeover`, region `us-west-2`, on every call. Verify `aws sts get-caller-identity` shows an account ending **8809** before any other AWS call; abort and report if not.
- Never print credentials; never write the full account ID into your report (mask to …8809).

## Inputs
- Project: /Users/peiyaoli/.openclaw/workspace/projects/agentcore-observability-demo/ (BRIEF.md = build spec, code, infra/, ux/, tests/)
- Builder evidence: /Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/local/ (acceptance results, evidence-manifest.json, WORKER-DONE.md)
- Sanitized docs: /Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/share/

## What to verify (rerun live where allowed, don't trust builder claims)
1. Live re-invoke the deployed runtime (new session, fresh reviewer actor id like `demo-actor-reviewer-<rand>`): valid model-generated answer?
2. Multi-turn STM in one session works?
3. LTM: retrieve_memory_records for the builder's demo actor namespace returns real records? A new session for that actor shows recall? (Do not wait for new extraction beyond ~3 min.)
4. Actor isolation: your fresh reviewer actor's namespace returns no other actor's records; agent doesn't leak other actors' preferences.
5. Telemetry: pick one of YOUR invocations, find its trace in `aws/spans` via Logs Insights (model span with gen_ai attributes + tool span), check the runtime log group and the obsdemo memory delivery log group have entries in your time window. Distinguish memory records (data) vs memory service logs.
6. Slow + error scenarios produce identifiable telemetry.
7. Security review (static + live):
   - IAM execution role: scoping quality, wildcards, trust policy conditions (confusion-deputy protection)?
   - Unauthenticated invoke rejected? (plain curl, no SigV4)
   - UX binds 127.0.0.1 only? Any creds in browser-served code? Any secrets committed to git?
   - Log payloads: any sensitive/non-synthetic data?
8. Evidence quality: does build/local/evidence-manifest.json match what you can actually retrieve? Are share/ docs free of account IDs and secrets? Flag any fabricated/unsupported claims.

## Output
Write EXACTLY ONE file: /Users/peiyaoli/.openclaw/workspace/work/agentcore-observability-demo/build/local/REVIEW-REPORT.md
- Verdict per item: CONFIRMED-LIVE / CONFIRMED-STATIC / FAILED / COULD-NOT-VERIFY (+why)
- Your own evidence: timestamps (UTC), session/trace IDs you generated, queries you ran
- Security findings ranked (blocker/major/minor)
- Explicit statement of your model identity from your own session metadata.
When done, also run: openclaw system event --text "Done: obsdemo review complete, see build/local/REVIEW-REPORT.md" --mode now

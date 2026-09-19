# obsdemo local chat UX

Loopback-only web chat for the `obsdemo_travel_agent` AgentCore runtime.
AWS credentials never reach the browser — the FastAPI backend signs
`InvokeAgentRuntime` calls (SigV4) using the `platform-dev-takeover` profile.

## Run

```bash
cd ux
AWS_PROFILE=platform-dev-takeover ../.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8931
```

Then open http://127.0.0.1:8931

## Features

- Chat transcript with the deployed agent (real `InvokeAgentRuntime` calls)
- **New Session** button (new STM session; LTM preferences persist per traveler)
- **Traveler** switch: Traveler A (Ava) / Traveler B (Ben) — demonstrates actor isolation
- **Scenario** selector: Normal / Slow tool (3s tool delay) / Tool error (deterministic demo failure)
- Collapsible **Telemetry** panel per response: session_id, actor_id, runtime session id,
  trace_id (from the agent's live OTel span context), and memory provenance
  (retrieved LTM record IDs + snippets, STM event count). Only fields actually
  returned by the agent are shown — nothing is invented client-side.

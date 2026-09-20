"""obsdemo travel-preference assistant for Amazon Bedrock AgentCore Runtime.

Payload: {"prompt": str, "actor_id": str, "session_id": str, "scenario": "normal"|"slow"|"error"}
Returns JSON with response text, memory provenance, and trace correlation info.
"""

import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor
from strands import Agent, tool
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("obsdemo.travel_agent")

# ADOT's botocore patch stamps the caller's STS access-key ID onto every AWS API span
# (aws-otel-python-instrumentation _botocore_patches.py) with no config switch to disable it.
_REDACTED_SPAN_ATTRIBUTES = (
    "aws.auth.account.access_key",
    "aws.remote.resource.account.access_key",
)


class CredentialAttributeRedactor(SpanProcessor):
    def on_start(self, span, parent_context=None) -> None:
        for key in _REDACTED_SPAN_ATTRIBUTES:
            if span.attributes is not None and key in span.attributes:
                span.set_attribute(key, "REDACTED")


def _install_redactor() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(CredentialAttributeRedactor())
        logger.info("obsdemo: credential-attribute redactor installed")
    else:
        logger.warning("obsdemo: tracer provider has no add_span_processor; redactor NOT installed")


_install_redactor()

REGION = os.environ.get("AWS_REGION", "us-west-2")
MEMORY_ID = os.environ["OBSDEMO_MEMORY_ID"]
MODEL_ID = os.environ.get("OBSDEMO_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
LTM_NAMESPACE_TEMPLATE = os.environ.get("OBSDEMO_LTM_NAMESPACE", "/preferences/{actorId}")

memory_client = boto3.client("bedrock-agentcore", region_name=REGION)

DESTINATIONS = {
    "queenstown": {
        "country": "New Zealand", "best_season": "December-February (summer)",
        "weather": "Mild summers around 22C, crisp winters near 8C, low humidity year-round",
        "attractions": ["Remarkables alpine hikes", "Lake Wakatipu trails", "Boutique lodges in Arrowtown"],
        "vibe": "Adventure hub with hiking, dry alpine air, and small luxury lodges",
    },
    "kyoto": {
        "country": "Japan", "best_season": "March-May and October-November",
        "weather": "Humid summers up to 33C, pleasant dry autumns around 18C",
        "attractions": ["Philosopher's Path walk", "Fushimi Inari trail hike", "Machiya boutique ryokans"],
        "vibe": "Temple city; summer is very humid, autumn is ideal for walkers",
    },
    "reykjavik": {
        "country": "Iceland", "best_season": "June-August for hiking, February for auroras",
        "weather": "Cool and dry-ish; summer highs around 14C, almost no humidity discomfort",
        "attractions": ["Laugavegur trail", "Golden Circle", "Design-focused boutique hotels"],
        "vibe": "Cool-climate hiking paradise with compact boutique hotel scene",
    },
    "banff": {
        "country": "Canada", "best_season": "June-September",
        "weather": "Dry mountain air, summer highs around 21C",
        "attractions": ["Sulphur Mountain trail", "Lake Louise shoreline hike", "Chalet-style boutique stays"],
        "vibe": "Rocky Mountain hiking with dry air and cozy boutique chalets",
    },
    "marrakech": {
        "country": "Morocco", "best_season": "October-April",
        "weather": "Hot dry summers up to 38C, warm dry winters around 20C",
        "attractions": ["Atlas foothills day hikes", "Medina souks", "Riad boutique guesthouses"],
        "vibe": "Dry desert-edge city; riads are quintessential boutique lodging",
    },
    "hobart": {
        "country": "Australia", "best_season": "December-March",
        "weather": "Mild and temperate, summer highs around 22C, moderate humidity",
        "attractions": ["kunanyi/Mt Wellington summit hike", "MONA", "Harbourside boutique hotels"],
        "vibe": "Compact harbour city with serious mountain hiking on its doorstep",
    },
}

_scenario: ContextVar[str] = ContextVar("scenario", default="normal")
_error_fired: ContextVar[bool] = ContextVar("error_fired", default=False)


class DemoToolError(Exception):
    """Deterministic demo-only tool failure (scenario=error)."""


@tool
def lookup_destination_info(city: str) -> str:
    """Look up canned travel information (weather, best season, attractions) for a destination city.

    Args:
        city: Destination city name, e.g. Queenstown, Kyoto, Reykjavik, Banff, Marrakech, Hobart.
    """
    scenario = _scenario.get()
    if scenario == "slow":
        logger.info("obsdemo scenario=slow: sleeping 3s in lookup_destination_info")
        time.sleep(3)
    if scenario == "error" and not _error_fired.get():
        _error_fired.set(True)
        logger.error("obsdemo scenario=error: raising DemoToolError in lookup_destination_info")
        raise DemoToolError(
            "DemoToolError: destination info service unavailable (synthetic demo failure)"
        )
    key = city.strip().lower()
    if key in DESTINATIONS:
        return json.dumps({"city": city.title(), **DESTINATIONS[key]})
    return json.dumps({
        "city": city, "error": "unknown destination",
        "known_destinations": sorted(d.title() for d in DESTINATIONS),
    })


def _fetch_ltm_preferences(actor_id: str) -> list[dict]:
    namespace = LTM_NAMESPACE_TEMPLATE.replace("{actorId}", actor_id)
    try:
        resp = memory_client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=namespace,
            searchCriteria={"searchQuery": "travel preferences likes dislikes hotels climate", "topK": 5},
        )
        records = []
        for r in resp.get("memoryRecordSummaries", []):
            text = r.get("content", {}).get("text", "")
            records.append({"record_id": r.get("memoryRecordId", ""), "snippet": text[:300]})
        return records
    except Exception:
        logger.exception("LTM retrieval failed for namespace %s", namespace)
        return []


def _fetch_stm_events(actor_id: str, session_id: str) -> list[dict]:
    try:
        resp = memory_client.list_events(
            memoryId=MEMORY_ID, actorId=actor_id, sessionId=session_id,
            includePayloads=True, maxResults=50,
        )
        events = resp.get("events", [])
        events.sort(key=lambda e: e.get("eventTimestamp", datetime.min.replace(tzinfo=timezone.utc)))
        return events
    except Exception:
        logger.exception("STM list_events failed for session %s", session_id)
        return []


def _events_to_messages(events: list[dict]) -> list[dict]:
    messages = []
    for ev in events:
        for item in ev.get("payload", []):
            conv = item.get("conversational")
            if not conv:
                continue
            role = "user" if conv.get("role") == "USER" else "assistant"
            text = conv.get("content", {}).get("text", "")
            if text:
                messages.append({"role": role, "content": [{"text": text}]})
    return messages


def _store_turn(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    try:
        memory_client.create_event(
            memoryId=MEMORY_ID, actorId=actor_id, sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {"conversational": {"role": "USER", "content": {"text": user_text}}},
                {"conversational": {"role": "ASSISTANT", "content": {"text": assistant_text}}},
            ],
        )
    except Exception:
        logger.exception("create_event failed for session %s", session_id)


SYSTEM_PROMPT_BASE = """You are a friendly travel-preference assistant for a demo travel agency.
You help travelers pick destinations. Use the lookup_destination_info tool when a specific city
is discussed. Known demo cities: Queenstown, Kyoto, Reykjavik, Banff, Marrakech, Hobart.
Keep answers concise (2-4 sentences unless asked for detail).
If the tool fails, apologize briefly and answer from general knowledge instead.
If you know the traveler's remembered preferences, weave them in naturally
(e.g. "since you love hiking...") without listing them robotically."""

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    prompt = str(payload.get("prompt", "")).strip()
    actor_id = str(payload.get("actor_id", "demo-actor-unknown")).strip()
    session_id = str(payload.get("session_id", "")).strip() or getattr(context, "session_id", "no-session")
    scenario = str(payload.get("scenario", "normal")).strip().lower()
    if scenario not in ("normal", "slow", "error"):
        scenario = "normal"
    _scenario.set(scenario)
    _error_fired.set(False)

    logger.info("obsdemo invoke actor=%s session=%s scenario=%s", actor_id, session_id, scenario)

    if not prompt:
        return {"error": "payload must include a non-empty 'prompt'"}

    ltm_records = _fetch_ltm_preferences(actor_id)
    stm_events = _fetch_stm_events(actor_id, session_id)
    prior_messages = _events_to_messages(stm_events)

    system_prompt = SYSTEM_PROMPT_BASE
    if ltm_records:
        prefs = "\n".join(f"- {r['snippet']}" for r in ltm_records)
        system_prompt += f"\n\nRemembered preferences for this traveler (from long-term memory):\n{prefs}"

    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    agent = Agent(
        model=model,
        tools=[lookup_destination_info],
        system_prompt=system_prompt,
        messages=prior_messages,
        trace_attributes={
            "obsdemo.actor_id": actor_id,
            "obsdemo.session_id": session_id,
            "obsdemo.scenario": scenario,
        },
    )
    result = agent(prompt)
    answer = str(result)

    _store_turn(actor_id, session_id, prompt, answer)

    trace_id = None
    span_ctx = trace.get_current_span().get_span_context()
    if span_ctx.is_valid and span_ctx.trace_id:
        trace_id = format(span_ctx.trace_id, "032x")

    return {
        "response": answer,
        "session_id": session_id,
        "actor_id": actor_id,
        "scenario": scenario,
        "trace_id": trace_id,
        "memory_provenance": {
            "ltm_records_retrieved": ltm_records,
            "ltm_record_count": len(ltm_records),
            "stm_events_used": len(stm_events),
        },
    }


if __name__ == "__main__":
    app.run()

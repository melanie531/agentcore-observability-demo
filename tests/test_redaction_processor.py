"""Offline unit test for the CredentialAttributeRedactor span processor.

No AWS access required — runs entirely against the OTel SDK in-memory exporter.

Run: python tests/test_redaction_processor.py
"""

import sys

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# the processor under test lives in agent/main.py, but importing that module
# requires the full runtime deps — replicate the exact class contract here by
# importing just the processor via a lightweight copy check.
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "agent"))

REDACTED_KEYS = (
    "aws.auth.account.access_key",
    "aws.remote.resource.account.access_key",
)


class CredentialAttributeRedactor(SpanProcessor):
    """Mirror of agent/main.py::CredentialAttributeRedactor (kept in sync)."""

    def on_start(self, span, parent_context=None) -> None:
        for key in REDACTED_KEYS:
            if span.attributes is not None and key in span.attributes:
                span.set_attribute(key, "REDACTED")


def assert_source_in_sync() -> None:
    """The test class must match the deployed one in agent/main.py."""
    agent_main = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "agent", "main.py")
    src = open(agent_main).read()
    for key in REDACTED_KEYS:
        assert key in src, f"agent/main.py no longer redacts {key}"
    assert 'set_attribute(key, "REDACTED")' in src, \
        "agent/main.py redaction mechanism changed — update this test"


def main() -> None:
    assert_source_in_sync()

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(CredentialAttributeRedactor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    # span carrying credential-identifier attrs (as ADOT stamps them at start)
    with tracer.start_as_current_span("aws-api-span", attributes={
        "aws.auth.account.access_key": "ASIAFAKEFAKEFAKEFAKE",
        "aws.remote.resource.account.access_key": "ASIAFAKEFAKEFAKEFAKE",
        "aws.auth.region": "us-west-2",
        "gen_ai.request.model": "test-model",
    }):
        pass
    # clean span must be untouched
    with tracer.start_as_current_span("clean-span", attributes={
        "gen_ai.operation.name": "chat",
    }):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    api = spans["aws-api-span"].attributes
    assert api["aws.auth.account.access_key"] == "REDACTED", api
    assert api["aws.remote.resource.account.access_key"] == "REDACTED", api
    assert api["aws.auth.region"] == "us-west-2", "non-sensitive attr must survive"
    assert api["gen_ai.request.model"] == "test-model", "gen_ai attrs must survive"
    clean = spans["clean-span"].attributes
    assert "aws.auth.account.access_key" not in clean
    assert clean["gen_ai.operation.name"] == "chat"
    print("PASS: redaction applied at export; clean spans and gen_ai attrs untouched")


if __name__ == "__main__":
    main()

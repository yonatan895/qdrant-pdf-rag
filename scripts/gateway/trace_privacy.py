"""Privacy-allowlisted OTLP export for the local gateway (issue #636).

The pinned LiteLLM's otel callback stamps request/response content
(gen_ai.input/output.messages, raw provider request/response bodies under
llm.<provider>.*), every request-metadata key (metadata.user_api_key_hash and
the rest), hidden_params, key-management records (response.*), exception text
and log events onto exported spans. No configuration flag of the pinned image
covers all of them, so filtering happens at the export boundary instead: each
finished span is rebuilt from an allowlist of operational attributes — what
ran, against which model, how long, how many tokens, which status — before it
leaves the process. Anything not listed, including attributes a future image
adds, is dropped. Events, links and status descriptions never leave.

Export is batched in a background thread with a bounded queue: LiteLLM wraps
a custom exporter in a synchronous SimpleSpanProcessor, which would otherwise
block every gateway request on an OTLP POST, and the bound caps gateway memory
when the collector is slow or down. The endpoint comes from the standard
OTEL_EXPORTER_OTLP_ENDPOINT the launcher sets. Local/CI hook only.
"""
from collections.abc import Sequence

from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status

SAFE_ATTRIBUTES = frozenset(
    {
        "call_type",
        "db.operation.name",
        "db.system",
        "db.system.name",
        "error.code",
        "error.type",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.response.model",
        "gen_ai.system",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.total_tokens",
        "http.response.status_code",
        "http.route",
        "litellm.model_group",
        "litellm.preprocessing.duration_ms",
        "litellm.provider.model",
        "llm.is_streaming",
        "llm.request.type",
        "server.address",
        "server.port",
    }
)
MAX_QUEUED_SPANS = 2048


def scrub(span: ReadableSpan) -> ReadableSpan:
    """The span with only allowlisted attributes, no events or links, and a
    bare status code (descriptions can carry upstream error text)."""
    attributes = {k: v for k, v in (span.attributes or {}).items() if k in SAFE_ATTRIBUTES}
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=(),
        links=(),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class AllowlistExporter(SpanExporter):
    """Scrub, then hand off to a bounded background batch exporter."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._batch = BatchSpanProcessor(delegate, max_queue_size=MAX_QUEUED_SPANS)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            self._batch.on_end(scrub(span))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._batch.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._batch.force_flush(timeout_millis)


otel = OpenTelemetry(
    config=OpenTelemetryConfig(
        exporter=AllowlistExporter(OTLPSpanExporter()),
        capture_message_content="NO_CONTENT",
    )
)

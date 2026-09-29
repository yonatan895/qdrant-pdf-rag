"""Metric/trace contract pins for docs/metric-contract.md (#529 OBS-3 slice).

The contract map was captured from the live dev stack; these tests pin its
source-of-truth side in CI so a silent instrument rename, unit change, label
addition, or bucket edit fails here before the shipped PromQL in
observability/queries/request-investigation.promql goes stale. Jaeger
lookups cannot execute in CI (no Jaeger); their URL templates are pinned
against the operation/tag vocabulary instead — live execution is recorded in
the contract doc.
"""

import re
from pathlib import Path

from mainframe_rag.agent import metrics as metrics_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMQL_FILE = REPO_ROOT / "observability/queries/request-investigation.promql"

# Wire rendering (OTel Prometheus exporter rule, pinned explicitly because it
# is the contract): dots become underscores; counters gain `_total`;
# histograms gain `_unit` except unitless (`1`) instruments, which gain
# nothing. A unit change in metrics.py therefore renames the wire series.
INSTRUMENTS = {
    "rag.requests.total": ("rag_requests_total", "1"),
    "rag.request.duration": ("rag_request_duration_seconds", "s"),
    "rag.retrieval.hits": ("rag_retrieval_hits", "1"),
    "rag.llm.ttft": ("rag_llm_ttft_milliseconds", "ms"),
}

LABEL_KEYS = {
    "rag.requests.total": {"endpoint", "query_class", "outcome"},
    "rag.request.duration": {"endpoint", "query_class", "outcome"},
    "rag.retrieval.hits": {"endpoint", "query_class"},
    "rag.llm.ttft": {"model"},
}
# verification_state attaches only when known; the exporter still emits the
# key with an empty value on every requests/duration series.
OPTIONAL_LABEL = "verification_state"

BUCKETS = {
    "duration": (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    "ttft": (10.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, 30000.0, 60000.0),
    "hits": (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 40.0),
}


def _hermetic(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod, "_instruments",
        metrics_mod.create_instruments(provider.get_meter("contract")),
    )
    return reader


def _metrics(reader):
    out = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


def test_contract_instruments_pinned(monkeypatch):
    reader = _hermetic(monkeypatch)
    metrics_mod.record_request(
        "search", "ok", elapsed_s=0.1, hits=1, ttft_ms=50, llm_model="m",
    )
    found = _metrics(reader)
    assert set(INSTRUMENTS) <= set(found)
    for otel_name, (wire, unit) in INSTRUMENTS.items():
        assert found[otel_name].unit == unit


def test_contract_label_keys(monkeypatch):
    reader = _hermetic(monkeypatch)
    metrics_mod.record_request(
        "answer", "ok", query_class="nl", elapsed_s=0.5, hits=8,
        ttft_ms=162, llm_model="m", verification_state="accepted",
    )
    metrics_mod.record_request("search", "ok", query_class="nl", elapsed_s=0.2, hits=8)
    found = _metrics(reader)
    for otel_name, keys in LABEL_KEYS.items():
        got = {frozenset(dict(p.attributes)) for p in found[otel_name].data.data_points}
        assert got, otel_name
        for keyset in got:
            assert set(keyset) <= keys | {OPTIONAL_LABEL}, (otel_name, keyset)
    # The known state lands on the labeled series; the unknown record stays
    # on the unlabeled one.
    counts = _metrics(reader)["rag.requests.total"].data.data_points
    states = sorted(dict(p.attributes).get(OPTIONAL_LABEL, "") for p in counts)
    assert states == ["", "accepted"]


def test_contract_buckets_pinned():
    assert tuple(metrics_mod.DURATION_BOUNDARIES_S) == BUCKETS["duration"]
    assert tuple(metrics_mod.TTFT_BOUNDARIES_MS) == BUCKETS["ttft"]
    assert tuple(metrics_mod.HITS_BOUNDARIES) == BUCKETS["hits"]


def test_promql_references_real_contract():
    text = PROMQL_FILE.read_text(encoding="utf-8")
    assert "M1" in text and "M6" in text  # the documented query set is intact
    wire_names = {wire for _, (wire, _) in INSTRUMENTS.items()}
    for token in set(re.findall(r"rag_[a-z_]+", text)):
        base = re.sub(r"_(bucket|count|sum)$", "", token)
        assert base in wire_names, token
    allowed_labels = set().union(*LABEL_KEYS.values()) | {OPTIONAL_LABEL, "le"}
    for selector in set(re.findall(r"\{([^{}]*)\}", text)):
        for matcher in selector.split(","):
            key = re.split(r"!?=~?", matcher, maxsplit=1)[0].strip()
            assert key in allowed_labels, matcher


# ------------------------------------------------------- Jaeger lookups (pinned templates)

JAEGER_SERVICE = "mainframe-rag-agent"
# The 12 operations captured live (OBS-1B tree); lookups below use roots.
JAEGER_ROOT_OPERATIONS = {"v1.search", "v1.answer", "v1.chat", "ui.chat"}
JAEGER_TAG_KEYS = {"http.request_id", "otel.status_code"}

# J1 request→trace pivot (executed live: resolves exactly one trace).
J1_TRACE_BY_REQUEST = (
    "/api/traces?service={service}&tags={tags}"
)
# J2 slow-answer hunt (executed live with minDuration=500ms: 2 hits).
J2_SLOW_ANSWERS = (
    "/api/traces?service={service}&operation=v1.answer&minDuration={floor}&limit={n}"
)
# J3 server-error hunt (contract-derived: 5xx roots end ERROR; no 5xx
# occurred on the dev stack, so this lookup is pinned but not live-tested).
J3_ERROR_ROOTS = (
    "/api/traces?service={service}&tags={tags}&limit={n}"
)


def test_jaeger_lookups_documented():
    assert JAEGER_SERVICE == "mainframe-rag-agent"
    assert JAEGER_ROOT_OPERATIONS <= {
        "v1.search", "v1.answer", "v1.chat", "ui.chat",
        "retrieve.embed", "retrieve.prefetch", "retrieve.rerank", "llm.chat",
        "retrieve.search", "retrieve.rrf", "retrieve.diversify", "prompt.build",
    }
    j1 = J1_TRACE_BY_REQUEST.format(
        service=JAEGER_SERVICE, tags='{"http.request_id":"<id>"}',
    )
    assert "http.request_id" in j1 and JAEGER_SERVICE in j1
    j2 = J2_SLOW_ANSWERS.format(service=JAEGER_SERVICE, floor="500ms", n=5)
    assert "operation=v1.answer" in j2 and "minDuration=500ms" in j2
    j3 = J3_ERROR_ROOTS.format(
        service=JAEGER_SERVICE, tags='{"otel.status_code":"ERROR"}', n=5,
    )
    assert "otel.status_code" in j3
    for key in ("http.request_id", "otel.status_code"):
        assert key in JAEGER_TAG_KEYS

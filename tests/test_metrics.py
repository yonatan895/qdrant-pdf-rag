"""Unit tests for agent/metrics.py (issue #187): provider lifecycle only.

No network, no lifespan: setup_metrics touches the in-process OTel globals,
so these tests pin idempotency (double setup never double-registers a
collector) and the disabled path. Route behavior lives in test_agent_api.py.
"""

import pytest
from fastapi.testclient import TestClient

from mainframe_rag.agent import app as app_mod
from mainframe_rag.agent import metrics as metrics_mod
from mainframe_rag.retrieve.query import SearchHit


def test_setup_metrics_disabled_returns_none():
    # Order-independent: the disabled path returns before touching globals,
    # so earlier test files may already have enabled the provider.
    assert metrics_mod.setup_metrics(False) is None


def test_setup_metrics_idempotent():
    first = metrics_mod.setup_metrics(True)
    assert first is not None
    assert metrics_mod.setup_metrics(True) is first


# ------------------------------------------------------- endpoint mapping


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("/v1/search", "search"),
        ("/v1/search/", "search"),
        ("/v1/answer", "answer"),
        ("/v1/answer?stream=true", "answer"),
        ("/metrics", None),
        ("/healthz", None),
        ("/v1/nope", None),
        ("/", None),
    ],
)
def test_endpoint_for_path(path, want):
    from urllib.parse import urlsplit

    assert metrics_mod.endpoint_for_path(urlsplit(path).path) == want


# ------------------------------------------------------- hermetic instruments


def _hermetic_instruments(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod, "_instruments",
        metrics_mod.create_instruments(provider.get_meter("test")),
    )
    return reader


def _points(reader, name):
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return list(m.data.data_points)
    raise AssertionError(f"instrument {name} not found")


def test_record_request_counter_and_histograms(monkeypatch):
    reader = _hermetic_instruments(monkeypatch)
    metrics_mod.record_request(
        "search", "ok", query_class="identifier",
        elapsed_s=0.5, hits=3, ttft_ms=120, llm_model="m",
    )
    metrics_mod.record_request("search", "ok", query_class="identifier", elapsed_s=0.25)
    metrics_mod.record_request("answer", "upstream_error", query_class="nl", elapsed_s=1.0, hits=0)
    metrics_mod.record_request("nope", "ok", elapsed_s=9.0)  # unknown endpoint ignored

    counts = _points(reader, "rag.requests.total")
    assert sum(p.value for p in counts) == 3
    by_outcome = {(dict(p.attributes)["endpoint"], dict(p.attributes)["outcome"]): p.value for p in counts}
    assert by_outcome == {("search", "ok"): 2, ("answer", "upstream_error"): 1}

    durations = _points(reader, "rag.request.duration")
    assert sum(p.count for p in durations) == 3
    assert sum(p.sum for p in durations) == pytest.approx(1.75)

    hits = _points(reader, "rag.retrieval.hits")
    assert sum(p.count for p in hits) == 2  # the hitless record adds no point
    assert sorted(p.sum for p in hits) == [0, 3]

    ttft = _points(reader, "rag.llm.ttft")
    assert len(ttft) == 1  # only the measured leg records
    assert ttft[0].count == 1 and ttft[0].sum == 120
    assert dict(ttft[0].attributes) == {"model": "m"}


def test_record_request_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(metrics_mod, "_instruments", None)
    metrics_mod.record_request("search", "ok", query_class="identifier", elapsed_s=1.0, hits=5)


def test_record_request_never_raises(monkeypatch):
    reader = _hermetic_instruments(monkeypatch)
    metrics_mod.record_request("search", "ok", elapsed_s=-1.0, hits=-2)  # clamps, not raises
    assert sum(p.count for p in _points(reader, "rag.request.duration")) == 1


# ------------------------------------------------------- endpoint behavior


def _hit():
    return SearchHit(
        chunk_id="abc123", score=0.42,
        cite="SA22-0000-00 Synthetic Reference, Chapter 2 > IEA500I, p. 1-6",
        heading="Chapter 2 > IEA500I", text="IEA500I synthetic text",
        doc_id="SA22-0000-00", title="Synthetic Reference", page_label="1-6",
        chunk_type="message", product="z/OS", version="9.9", message_ids=("IEA500I",),
    )


class _Search:
    def __init__(self, exc=None):
        self.exc = exc

    def __call__(self, *args, **kwargs):
        if self.exc:
            raise self.exc
        return [_hit()], "identifier", {"embed_ms": 1, "qdrant_ms": 2}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("EMBED_MODE", "hash")
    monkeypatch.setenv("ALLOW_HASH_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL_REASONING", "test-reasoning-model")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setattr(app_mod, "retrieve_search", _Search())
    with TestClient(app_mod.app) as c:
        yield c


def _series(body, name, **labels):
    # Subset match: the OTel exporter appends otel_scope_* labels to every
    # series, so pin our labels as all present rather than exactly equal.
    for line in body.splitlines():
        if not line.startswith(name + "{"):
            continue
        attrs, _, value = line[len(name) + 1:].partition("} ")
        pairs = dict(p.split("=", 1) for p in attrs.split(","))
        if all(pairs.get(k) == f'"{v}"' for k, v in labels.items()):
            return float(value)
    raise AssertionError(f"series {name} with {labels} missing")


def test_search_success_records_ok_series(client):
    resp = client.post("/v1/search", json={"query": "IEA500I SECRETXYZ"})
    assert resp.status_code == 200
    body = client.get("/metrics").text
    assert _series(body, "rag_requests_total", endpoint="search", outcome="ok", query_class="identifier") == 1.0
    # Cardinality law: the query text that triggered the request must never
    # become a label value in the exposition.
    assert "SECRETXYZ" not in body


def test_search_failure_records_outcome_series(client, monkeypatch):
    monkeypatch.setattr(app_mod, "retrieve_search", _Search(exc=RuntimeError("qdrant down")))
    resp = client.post("/v1/search", json={"query": "IEA500I"})
    assert resp.status_code == 502
    body = client.get("/metrics").text
    assert _series(body, "rag_requests_total", endpoint="search", outcome="upstream_error",
                   query_class="unknown") == 1.0


def test_overlong_query_records_invalid_request(client):
    resp = client.post("/v1/search", json={"query": "Q" * 2001})
    assert resp.status_code == 422
    body = client.get("/metrics").text
    assert _series(body, "rag_requests_total", endpoint="search", outcome="invalid_request",
                   query_class="unknown") == 1.0

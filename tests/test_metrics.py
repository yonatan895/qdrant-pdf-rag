"""Unit tests for agent/metrics.py (issue #187): provider lifecycle only.

No network, no lifespan: setup_metrics touches the in-process OTel globals,
so these tests pin idempotency (double setup never double-registers a
collector) and the disabled path. Route behavior lives in test_agent_api.py.
"""

from mainframe_rag.agent import metrics as metrics_mod


def test_setup_metrics_disabled_returns_none():
    # Order-independent: the disabled path returns before touching globals,
    # so earlier test files may already have enabled the provider.
    assert metrics_mod.setup_metrics(False) is None


def test_setup_metrics_idempotent():
    first = metrics_mod.setup_metrics(True)
    assert first is not None
    assert metrics_mod.setup_metrics(True) is first

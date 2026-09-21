"""Opt-in, dependency-free two-runner partition of pytest's selected items."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--unit-shard", type=int, choices=(1, 2), default=None,
        help="Run shard 1 or 2 of the selected tests (omit to run all tests).",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # Run after pytest's -m/-k filtering, so both runners partition the same
    # eligible collection. Parametrized cases are indivisible node IDs.
    shard = config.getoption("--unit-shard")
    if shard is None:
        return
    ordered = sorted(items, key=lambda item: item.nodeid)
    selected = ordered[shard - 1::2]
    deselected = ordered[2 - shard::2]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected

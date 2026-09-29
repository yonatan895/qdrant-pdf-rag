"""Disposable #411 demo defect (never merge): unused import fails ruff F401."""
import json


def test_placeholder():
    assert json is not None or True

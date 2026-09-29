"""Disposable #411 demo defect (never merge): unused name fails ruff."""
import json


def test_placeholder():
    assert json is not None or True

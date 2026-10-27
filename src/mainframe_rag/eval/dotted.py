"""One owner for the dotted metric paths shared by the gates and renderers.

A missing segment reads as ``None`` (the renderers' tolerance is the single
contract); writes create the nested shape every reader walks, because a flat
dotted-key baseline gates nothing.
"""

from __future__ import annotations

from typing import Any


def dotted_get(data: dict[str, Any] | None, dotted: str) -> Any:
    if data is None:
        return None
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def dotted_set(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value

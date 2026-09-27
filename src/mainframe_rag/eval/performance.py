"""L3 performance verdicts, reports and explicit-root baseline selection.

Live load orchestration and environment capture remain in the L3 command.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_ENV_GATE_KEYS = ("cpu_count", "embed_mode", "qdrant_image", "concurrency")


def default_baseline_path(workspace_root: Path, embed_mode: str) -> Path:
    """Resolve the existing mode-keyed filename from explicit invocation inputs."""
    name = "harness-l3-vllm.json" if embed_mode.lower() == "vllm" else "harness-l3.json"
    return workspace_root / "benchmarks" / name


def _get_nested(data: dict[str, Any] | None, dotted: str) -> Any:
    if data is None:
        return None
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def gate_verdict_l3(
    report: dict[str, Any],
    baseline: dict[str, Any] | None,
    tolerance: float = 3.0,
) -> tuple[str, list[str]]:
    """Evaluate L3 performance gate: fails on request errors, missing Server-Timing
    headers, environment mismatches, or p95 regressions beyond tolerance."""
    reasons: list[str] = []

    # 1. Request errors and missing headers under load
    for ep in ("search", "answer"):
        ep_data = report.get(ep, {})
        errors = ep_data.get("errors", 0)
        if errors > 0:
            reasons.append(f"{ep}: {errors} request error(s) under load")
        missing_timings = ep_data.get("missing_timings", 0)
        if missing_timings > 0:
            reasons.append(f"{ep}: {missing_timings} response(s) missing Server-Timing header")

    # 2. Baseline missing check: accumulated errors/header regressions MUST NOT be dropped
    if baseline is None:
        if reasons:
            return ("hold", reasons)
        return ("baseline", ["no baseline recorded"])

    # 3. Environment mismatch check (PR 71 failure class prevention)
    recorded_env = (baseline.get("_meta") or {}).get("env") or {}
    live_env = report.get("env") or {}
    for key in _ENV_GATE_KEYS:
        recorded = recorded_env.get(key)
        live = live_env.get(key)
        if recorded is not None and live is not None and recorded != live:
            reasons.append(
                f"baseline env mismatch: {key} {recorded!r} != runner {live!r} — "
                "the baseline was captured in a different environment; re-baseline in the gate's own environment"
            )
    recorded_gpu = recorded_env.get("gpu_name")
    live_gpu = live_env.get("gpu_name")
    if recorded_gpu and live_gpu and recorded_gpu != live_gpu:
        reasons.append(
            f"baseline env mismatch: gpu_name {recorded_gpu!r} != runner {live_gpu!r} — "
            "the baseline was captured on a different GPU; re-baseline on this hardware"
        )
    if any("baseline env mismatch" in r for r in reasons):
        return ("hold", reasons)

    # 4. Check total latency p95
    for ep in ("search", "answer"):
        cur_p95 = _get_nested(report, f"{ep}.latency_ms.p95")
        base_p95 = _get_nested(baseline, f"agent.{ep}.latency_ms.p95")
        if cur_p95 is not None and base_p95 is not None:
            limit = base_p95 * tolerance
            if cur_p95 > limit:
                reasons.append(
                    f"{ep}.latency_ms.p95: {cur_p95}ms > {base_p95}ms x{tolerance} (limit {round(limit, 2)}ms)"
                )

        # 5. Check stage latencies p95 and missing stages (header regression check)
        cur_stages = report.get(ep, {}).get("stages", {})
        base_stages = _get_nested(baseline, f"agent.{ep}.stages") or {}
        for sname, base_metrics in base_stages.items():
            if sname not in cur_stages:
                reasons.append(f"{ep}.stages: expected stage {sname} missing from Server-Timing")
                continue
            cur_sp95 = cur_stages[sname].get("p95")
            base_sp95 = base_metrics.get("p95")
            if cur_sp95 is not None and base_sp95 is not None:
                limit = base_sp95 * tolerance
                if cur_sp95 > limit:
                    reasons.append(
                        f"{ep}.stages.{sname}.p95: {cur_sp95}ms > {base_sp95}ms x{tolerance} (limit {round(limit, 2)}ms)"
                    )

    if reasons:
        return ("hold", reasons)
    return ("pass", [])


def summary_markdown_l3(report: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    lines = [
        "# Harness L3 — performance & latency report",
        "",
        "## Request Latencies",
        "",
        "| endpoint | rps | p50 (ms) | p95 (ms) | baseline p95 | errors | missing timings |",
        "|---|---|---|---|---|---|---|",
    ]
    for ep in ("search", "answer"):
        ep_data = report.get(ep, {})
        lat = ep_data.get("latency_ms", {})
        cur_p95 = lat.get("p95")
        base_p95 = _get_nested(baseline, f"agent.{ep}.latency_ms.p95")
        base_str = f"{base_p95}ms" if base_p95 is not None else "n/a"
        lines.append(
            f"| {ep} | {ep_data.get('rps', 0.0)} | {lat.get('p50', 0.0)} | {cur_p95} | {base_str} | "
            f"{ep_data.get('errors', 0)} | {ep_data.get('missing_timings', 0)} |"
        )

    lines += [
        "",
        "## Per-Stage Latencies (p50 / p95)",
        "",
        "| endpoint | stage | p50 (ms) | p95 (ms) | baseline p95 | max (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for ep in ("search", "answer"):
        stages = report.get(ep, {}).get("stages", {})
        for sname, smetrics in sorted(stages.items()):
            cur_sp95 = smetrics.get("p95")
            base_sp95 = _get_nested(baseline, f"agent.{ep}.stages.{sname}.p95")
            base_str = f"{base_sp95}ms" if base_sp95 is not None else "n/a"
            lines.append(
                f"| {ep} | {sname} | {smetrics.get('p50', 0.0)} | {cur_sp95} | {base_str} | {smetrics.get('max', 0.0)} |"
            )

    vram = report.get("vram")
    if vram:
        lines += [
            "",
            "## VRAM Footprint (trend data; not gated)",
            "",
            f"- used: {vram.get('used_mb')} MB",
            f"- total: {vram.get('total_mb')} MB",
        ]

    return "\n".join(lines) + "\n"

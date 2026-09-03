"""Portable vLLM serving budgets (serving-budget track, PR-A).

Sizes per-server vLLM launch arguments (gpu-memory-utilization, max-model-len,
runner/convert, eager vs compiled) from DECLARED model facts plus generic
rules — the same resolve path for a local 8GB card and the air-gapped
OpenShift hosts. Nothing is probed: VRAM totals and model facts are inputs,
so every path is hermetic and unit-testable with no GPU, no network, and no
model files.

Semantics: footprints are conservative UPPER bounds (weights + KV pool at the
full window + engine margin), calibrated against the 8GB co-residency soak
(PR #100 artifact: 6977 MiB measured peak). The arithmetic proves fit on
paper; per-profile soak runs (PR-D load tier) prove it empirically. When the
two disagree, the soak wins and the declared table gets re-calibrated.

This module never deploys anything: this repo does not install or operate
vLLM (platform team's job). PR-B wires `resolve` into scripts/run_local_vllm.sh;
the OPENSHIFT_PROD profile emits sizing REQUIREMENTS for handoff, not deployments.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MIB = 1024 * 1024

# Driver + CUDA-context floor kept off-limits on every host. The 8GB LOCAL
# pack is validated-tight (0.64 + 0.33 + reserve ~= total): its fit is proven
# by the measured 6977 MiB soak peak, not by slack in this arithmetic.
DEFAULT_RESERVE_MIB = 256.0

# Engine workspace upper bounds (re-calibrate via soak when adding models):
# - compiled generate: CUDA-graph capture + fragmentation. Floor 2 GiB, plus
#   5% of weights so the bound grows with model size (a flat constant fit the
#   4B local model but would under-claim a 31B server).
# - eager generate fallback: no graph capture, fragmentation only.
# - pooling eager: single-shot prefill workspace (logit/activation buffers
#   scale with the window, which is why batched tokens are capped at the
#   window). Calibrated at a 4096-token window; re-calibrate above that.
COMPILED_MARGIN_FLOOR_MIB = 2000.0
COMPILED_MARGIN_WEIGHT_FRACTION = 0.05
EAGER_GENERATE_MARGIN_MIB = 400.0
POOLING_EAGER_MARGIN_MIB = 1500.0

# vLLM flag quantum: utils are ceiled to 2dp so the allocation always covers
# the computed footprint (flooring could under-allocate by up to 1%).
UTIL_QUANTUM = 0.01

# A single server claiming more than this leaves no room for kernels and
# driver growth; fail closed instead of launching into it.
MAX_UTIL = 0.99


class BudgetDeficitError(RuntimeError):
    """Fail-closed sizing refusal. `remedies` lists concrete operator actions;
    callers surface them, never a bare traceback."""

    def __init__(self, message: str, remedies: Sequence[str]) -> None:
        super().__init__(message)
        self.remedies: list[str] = list(remedies)


class ModelSpec(BaseModel):
    """Declared serving facts for one model. `weight_mib` is the RESIDENT
    size post-quantization (e.g. QAT weights, not parameter count x dtype);
    replace estimates with measured nvidia-smi deltas when available."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    role: Literal["reasoning", "embed", "rerank"]
    runner: Literal["generate", "pooling"]
    convert: Literal["none", "embed"] = "none"
    weight_mib: float = Field(gt=0.0)
    # KV-cache bytes per context token from arch facts
    # (2 x layers x kv_heads x head_dim x dtype_bytes); 0 for pooling models,
    # which hold no KV cache.
    kv_bytes_per_token: float = Field(ge=0.0)
    # Required --max-model-len. Never silently shrunk by resolve (issue #99
    # review rule): to change a window, change the declared spec.
    context_need: int = Field(gt=0, le=131072)
    max_num_seqs: int = Field(default=1, ge=1)
    # vLLM prefix caching (KV reuse across shared prompt prefixes). A launch
    # flag like runner/convert/eager, not sizing: off unless the profile
    # enables it; issue #80 measures the hit rate before enabling anywhere.
    prefix_cache: bool = False


class HostSpec(BaseModel):
    """One serving host. Totals are declared (nvidia-smi), never probed."""

    model_config = ConfigDict(frozen=True)

    total_vram_mib: float = Field(gt=0.0)
    reserve_mib: float = Field(default=DEFAULT_RESERVE_MIB, ge=0.0)


class ServerPlan(BaseModel):
    """Resolved launch arguments for one vLLM server."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    role: str
    runner: str
    convert: str
    gpu_memory_utilization: float = Field(gt=0.0, le=1.0)
    max_model_len: int = Field(gt=0)
    # Pooling prefill memory scales with in-flight tokens, so it is capped at
    # the window; generate leaves the server default (None).
    max_num_batched_tokens: int | None = Field(default=None, gt=0)
    max_num_seqs: int = Field(ge=1)
    enforce_eager: bool
    enable_prefix_caching: bool = False
    footprint_mib: float = Field(gt=0.0)
    notes: list[str] = Field(default_factory=list)


class ProfileBundle(BaseModel):
    """A named host + ordered server list. Order is allocation order:
    earlier servers claim VRAM first."""

    model_config = ConfigDict(frozen=True)

    name: str
    host: HostSpec
    servers: list[ModelSpec] = Field(min_length=1)


class DeploymentPlan(BaseModel):
    """A resolved bundle: per-server flags plus the fit accounting."""

    model_config = ConfigDict(frozen=True)

    profile_name: str
    host_total_vram_mib: float
    reserve_mib: float
    servers: list[ServerPlan] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)

    @property
    def total_footprint_mib(self) -> float:
        return sum(s.footprint_mib for s in self.servers)

    @property
    def slack_mib(self) -> float:
        return self.host_total_vram_mib - self.reserve_mib - self.total_footprint_mib

    def explain(self) -> str:
        """Sizing math for PR evidence and platform-team handoff."""
        lines = [
            (
                f"Budget {self.profile_name}: "
                f"host {self.host_total_vram_mib:.0f} MiB, reserve {self.reserve_mib:.0f} MiB"
            )
        ]
        for s in self.servers:
            flags = (
                f"--runner {s.runner}"
                + (f" --convert {s.convert}" if s.convert != "none" else "")
                + (" --enforce-eager" if s.enforce_eager else "")
                + (" --enable-prefix-caching" if s.enable_prefix_caching else "")
            )
            lines.append(
                f"  {s.role} {s.model_id}: footprint {s.footprint_mib:.0f} MiB "
                f"-> gpu-memory-utilization {s.gpu_memory_utilization:.2f}, "
                f"max-model-len {s.max_model_len} ({flags})"
            )
            lines.extend(f"    note: {n}" for n in s.notes)
        lines.append(
            f"Total {self.total_footprint_mib:.0f} MiB + reserve "
            f"{self.reserve_mib:.0f} = "
            f"{self.total_footprint_mib + self.reserve_mib:.0f} / "
            f"{self.host_total_vram_mib:.0f} MiB host "
            f"-> FIT ({self.slack_mib:.0f} MiB slack)"
        )
        lines.extend(f"warning: {w}" for w in self.warnings)
        return "\n".join(lines)


def _compiled_margin_mib(weight_mib: float) -> float:
    return max(COMPILED_MARGIN_FLOOR_MIB, COMPILED_MARGIN_WEIGHT_FRACTION * weight_mib)


def _ceil_util(footprint_mib: float, total_mib: float) -> float:
    return math.ceil(footprint_mib / total_mib / UTIL_QUANTUM) * UTIL_QUANTUM


def _remedies(spec: ModelSpec, host: HostSpec, reason: str) -> list[str]:
    return [
        f"{reason} for {spec.model_id} on a {host.total_vram_mib:.0f} MiB host.",
        "Use a larger host (raise total_vram_mib) or serve fewer models on this host.",
        f"Lower context_need ({spec.context_need}) for {spec.model_id}.",
        (
            "For generate runners, an eager fallback is tried automatically; "
            "if this deficit names a pooling runner, eager is already assumed."
        ),
    ]


def resolve(profile: ProfileBundle) -> DeploymentPlan:
    """Resolve a profile to per-server launch arguments. Raises
    BudgetDeficitError (with remedies) instead of emitting an unsafe plan."""
    free = profile.host.total_vram_mib - profile.host.reserve_mib
    if free <= 0:
        raise BudgetDeficitError(
            f"Host {profile.host.total_vram_mib:.0f} MiB leaves no VRAM "
            f"after the {profile.host.reserve_mib:.0f} MiB reserve.",
            remedies=[
                "Raise total_vram_mib or lower reserve_mib.",
                "Serve fewer models on this host.",
            ],
        )
    plans: list[ServerPlan] = []
    warnings: list[str] = []
    for spec in profile.servers:
        kv_pool_mib = spec.kv_bytes_per_token * spec.context_need / MIB
        notes: list[str] = []
        if spec.runner == "pooling":
            # No decode phase: torch.compile + CUDA-graph workspace is pure
            # overhead on single-shot prefill (issue #99 embed branch).
            eager = True
            margin = POOLING_EAGER_MARGIN_MIB
            batched: int | None = spec.context_need
        else:
            compiled_footprint = spec.weight_mib + kv_pool_mib + _compiled_margin_mib(
                spec.weight_mib
            )
            if compiled_footprint <= free:
                eager = False
                margin = _compiled_margin_mib(spec.weight_mib)
                batched = None
            else:
                eager_footprint = spec.weight_mib + kv_pool_mib + EAGER_GENERATE_MARGIN_MIB
                if eager_footprint > free:
                    raise BudgetDeficitError(
                        f"Deficit: {spec.model_id} needs {eager_footprint:.0f} MiB "
                        f"even eager, {free:.0f} MiB free.",
                        remedies=_remedies(spec, profile.host, "Deficit"),
                    )
                eager = True
                margin = EAGER_GENERATE_MARGIN_MIB
                batched = None
                note = (
                    f"{spec.model_id}: compiled workspace does not fit "
                    f"({compiled_footprint:.0f} MiB); falling back to "
                    "--enforce-eager. Expect lower decode throughput."
                )
                notes.append(note)
                warnings.append(note)
        footprint = spec.weight_mib + kv_pool_mib + margin
        if footprint > free:
            raise BudgetDeficitError(
                f"Deficit: {spec.model_id} needs {footprint:.0f} MiB, "
                f"{free:.0f} MiB free.",
                remedies=_remedies(spec, profile.host, "Deficit"),
            )
        util = _ceil_util(footprint, profile.host.total_vram_mib)
        if util > MAX_UTIL:
            raise BudgetDeficitError(
                f"Deficit: {spec.model_id} alone claims {util:.2f} of the host.",
                remedies=_remedies(spec, profile.host, "Over-concentration"),
            )
        free -= footprint
        plans.append(
            ServerPlan(
                model_id=spec.model_id,
                role=spec.role,
                runner=spec.runner,
                convert=spec.convert,
                gpu_memory_utilization=round(util, 2),
                max_model_len=spec.context_need,
                max_num_batched_tokens=batched,
                max_num_seqs=spec.max_num_seqs,
                enforce_eager=eager,
                enable_prefix_caching=spec.prefix_cache,
                footprint_mib=footprint,
                notes=notes,
            )
        )
    return DeploymentPlan(
        profile_name=profile.name,
        host_total_vram_mib=profile.host.total_vram_mib,
        reserve_mib=profile.host.reserve_mib,
        servers=plans,
        warnings=warnings,
    )

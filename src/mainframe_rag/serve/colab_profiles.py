"""Explicit connected-development reference envelopes, never production defaults.

These allocations are estimates from #706, not measurements or a fit proof.
The existing 8 GiB budget arithmetic cannot model Qwen3.8's recurrent state;
Colab uses observed admission followed by measured co-residency instead.
The serializable profile is the sole producer of remote model argv.
"""
from __future__ import annotations

import math
from typing import Any

GENERATOR_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
EMBEDDING_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
COLAB_PROFILES = ("COLAB_CPU_TRANSPORT", "COLAB_G4_BF16", "COLAB_A100_80GB_BF16")


def build_profile(name: str, *, embed_max_len: int = 4096,
                  deadline_seconds: int = 7200) -> dict[str, Any]:
    """Produce a public reference profile; reject unknown profiles/oversize inputs."""
    if name not in COLAB_PROFILES:
        raise ValueError(f"unknown Colab profile: {name}")
    if (type(deadline_seconds) is not int or type(embed_max_len) is not int
            or not 1 <= deadline_seconds <= 7200 or not 4096 <= embed_max_len <= 32768):
        raise ValueError("deadline/window outside the declared Colab bounds")
    cpu = name == "COLAB_CPU_TRANSPORT"
    a100 = name == "COLAB_A100_80GB_BF16"
    profile: dict[str, Any] = {
        "schema": 1, "name": name, "reference_only": True,
        "accelerator": None if cpu else "A100" if a100 else "G4",
        "deadline_seconds": deadline_seconds,
        "minimum_vram_mib": 0 if cpu else 78000 if a100 else 92160,
        "minimum_ram_mib": 1024 if cpu else 65536,
        "minimum_disk_mib": 1024 if cpu else 131072,
        "cuda_build": None if cpu else "13.0",
        "vllm_version": "0.28.0",
        "transformers_version": "5.15.0", "python_version": "3.12",
        "dense_dim": 0 if cpu else 4096,
        "servers": [],
    }
    if cpu:
        return profile
    for role, model, revision, path, port, allocation, window, sequences in (
        ("reasoning", "Qwen/Qwen3.8-27B", GENERATOR_REVISION, "reasoning", 8000,
         "0.73" if a100 else "0.66", 16384, 1 if a100 else 2),
        ("embed", "Qwen/Qwen3-Embedding-8B", EMBEDDING_REVISION, "embedding", 8001,
         "0.22" if a100 else "0.24", embed_max_len, 1 if a100 else 8),
    ):
        argv = ["/content/rag-vllm/bin/vllm", "serve", f"/content/models/{path}",
                "--served-model-name", model, "--dtype", "bfloat16",
                "--tensor-parallel-size", "1"]
        argv += (["--language-model-only", "--reasoning-parser", "qwen3"]
                 if role == "reasoning" else ["--runner", "pooling", "--convert", "embed"])
        argv += ["--gpu-memory-utilization", allocation, "--max-model-len", str(window),
                 "--max-num-seqs", str(sequences), "--max-num-batched-tokens",
                 str(4096 if role == "reasoning" else window)]
        if role == "reasoning":
            argv.append("--enable-chunked-prefill")
        argv += ["--enforce-eager", "--host", "127.0.0.1", "--port", str(port)]
        profile["servers"].append({"role": role, "model_id": model, "revision": revision,
                                   "local_dir": f"/content/models/{path}", "port": port,
                                   "argv": argv})
    return profile


def _capacity_at_least(value: Any, minimum: int) -> bool:
    # Census emits ints/floats. Missing, textual, boolean and nonfinite evidence
    # cannot establish capacity, even when `< minimum` happens to be false.
    return (type(value) is int or type(value) is float and math.isfinite(value)) and value >= minimum


def admit(profile: dict[str, Any], observed: dict[str, Any]) -> None:
    """Fail before model downloads on an unsuitable actual assignment."""
    if profile["accelerator"]:
        name = observed.get("gpu_name", "")
        name = name.lower() if type(name) is str else ""
        expected_name = ("a100" in name if profile["accelerator"] == "A100"
                         else "rtx pro 6000" in name and "blackwell" in name)
        if not expected_name or not _capacity_at_least(
                observed.get("vram_mib"), profile["minimum_vram_mib"]):
            raise ValueError("assigned GPU does not match the selected profile and VRAM minimum")
        try:
            driver_major = int(str(observed["driver_version"]).split(".")[0])
        except (KeyError, ValueError) as exc:
            raise ValueError("cannot verify the assigned NVIDIA driver") from exc
        if driver_major < 580:
            raise ValueError("CUDA 13.0 reference requires NVIDIA driver 580 or newer")
    for field, minimum in (("available_ram_mib", "minimum_ram_mib"),
                           ("free_disk_mib", "minimum_disk_mib")):
        if not _capacity_at_least(observed.get(field), profile[minimum]):
            raise ValueError(f"assigned resources below {minimum}")

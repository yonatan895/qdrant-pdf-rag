"""Explicit connected-development reference envelopes, never production defaults.

These allocations are estimates from #706, not measurements or a fit proof.
The existing 8 GiB budget arithmetic cannot model Qwen3.8's recurrent state;
Colab uses observed admission followed by measured co-residency instead.
The serializable profile is the sole producer of remote model argv.
"""
from __future__ import annotations

import math
import re
from typing import Any

GENERATOR_MODEL = "Qwen/Qwen3.8-27B"
GENERATOR_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
# Reasoning-server inputs of the measurement protocol (#1195). The defaults
# are the reference values, so an unparameterised profile is unchanged. The
# window ceiling is serve.budget.ModelSpec.context_need's and
# Settings.llm_max_model_len's (131072); a test pins the three in step.
REASONING_DEFAULTS = {"model_id": GENERATOR_MODEL, "revision": GENERATOR_REVISION,
                      "max_model_len": 32768, "reasoning_parser": "qwen3"}
REASONING_WINDOW_BOUNDS = (4096, 131072)
REASONING_SEQUENCE_BOUNDS = (1, 16)
REASONING_PARSER_NONE = "none"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_PARSER = re.compile(r"[a-z0-9_]{1,32}")
EMBEDDING_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
# #862 retrieval-only measurement profiles: Qwen3-Embedding-8B plus ONE
# Qwen3-Reranker and no reasoning server (an 8B reranker cannot co-reside with
# the 27B). Revisions are the HF commit SHAs of `main` resolved 2026-10-22.
RERANKERS = {
    "COLAB_G4_RERANK_8B": ("Qwen/Qwen3-Reranker-8B", "77d193c791ed757ca307ee72715aa132723da912"),
    "COLAB_G4_RERANK_4B": ("Qwen/Qwen3-Reranker-4B", "22e683669bc0f0bd69640a1354a6d0aebcfeede5"),
}
COLAB_RERANK_PROFILES = tuple(RERANKERS)
COLAB_PROFILES = ("COLAB_CPU_TRANSPORT", "COLAB_G4_BF16", "COLAB_A100_80GB_BF16", *COLAB_RERANK_PROFILES)
# vLLM 0.28.0 docs/models/pooling_models/scoring.md: the original (not
# seq-cls converted) Qwen3-Reranker loads only through these overrides.
RERANK_HF_OVERRIDES = ('{"architectures":["Qwen3ForSequenceClassification"],'
                       '"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}')
# vLLM 0.28.0 applies a score template only when --chat-template is given
# (entrypoints/pooling/scoring/io_processor.py), so omitting it scores the
# bare query+document concatenation. The model repo's own pinned template is
# the reference prompt; it is downloaded with the snapshot (*.jinja).
RERANK_CHAT_TEMPLATE = "chat_template.jinja"
RERANK_WINDOW = 8192
RERANK_SEQUENCES = 32  # = Settings.rerank_batch_size default


def profile_server(profile: dict[str, Any], role: str) -> dict[str, Any]:
    """The profile's single server for `role`; consumers must not index by position."""
    matches = [server for server in profile["servers"] if server["role"] == role]
    if len(matches) != 1:
        raise ValueError(f"profile {profile['name']} has no unique {role} server")
    server: dict[str, Any] = matches[0]
    return server


def reasoning_inputs(name: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the reasoning-server inputs of a profile, fail closed.

    Absent keys take the reference value. A model other than the reference
    needs its own immutable revision: the reference pin is never reused for
    it. Fit is not proven here (see the module docstring): the allocation
    fractions stay the profile's, and vLLM refuses at startup a window its
    KV pool cannot hold, which the lane reports as a failed, released run."""
    if name not in COLAB_PROFILES or name == "COLAB_CPU_TRANSPORT" or name in RERANKERS:
        raise ValueError("reasoning inputs need a profile with a reasoning server")
    given = dict(overrides or {})
    allowed = {*REASONING_DEFAULTS, "max_num_seqs"}
    if not set(given) <= allowed or any(value is None for value in given.values()):
        raise ValueError("unknown or empty reasoning input")
    resolved = {**REASONING_DEFAULTS,
                "max_num_seqs": 1 if name == "COLAB_A100_80GB_BF16" else 2, **given}
    model, revision, parser = (resolved[key] for key in ("model_id", "revision", "reasoning_parser"))
    if (type(model) is not str or not _MODEL_ID.fullmatch(model) or ".." in model
            or type(revision) is not str or not _REVISION.fullmatch(revision)):
        raise ValueError("reasoning model needs a Hugging Face repository id and a 40-hex revision")
    if model != GENERATOR_MODEL and "revision" not in given:
        raise ValueError("a non-reference reasoning model needs its own pinned revision")
    if type(parser) is not str or not _PARSER.fullmatch(parser):
        raise ValueError("reasoning parser must be a vLLM parser name or 'none'")
    for key, (low, high) in (("max_model_len", REASONING_WINDOW_BOUNDS),
                             ("max_num_seqs", REASONING_SEQUENCE_BOUNDS)):
        if type(resolved[key]) is not int or not low <= resolved[key] <= high:
            raise ValueError(f"reasoning {key} outside the declared bounds {low}..{high}")
    return resolved


def reasoning_window(profile: dict[str, Any]) -> int:
    """The served reasoning window, read from the argv that launches it."""
    argv = profile_server(profile, "reasoning")["argv"]
    if argv.count("--max-model-len") != 1:
        raise ValueError("reasoning argv has no unique window")
    return int(argv[argv.index("--max-model-len") + 1])


def build_profile(name: str, *, embed_max_len: int = 8192, deadline_seconds: int = 7200,
                  reasoning: dict[str, Any] | None = None) -> dict[str, Any]:
    """Produce a public reference profile; reject unknown profiles/oversize inputs.

    `reasoning` (#1195) carries measurement inputs for the reasoning server.
    Inputs equal to the reference leave the profile exactly as before; any
    other input is recorded as `reasoning_inputs`, so the prepared manifest,
    its approval and every consumer that rebuilds the profile see them."""
    if name not in COLAB_PROFILES:
        raise ValueError(f"unknown Colab profile: {name}")
    if (type(deadline_seconds) is not int or type(embed_max_len) is not int
            or not 1 <= deadline_seconds <= 7200 or not 4096 <= embed_max_len <= 32768):
        raise ValueError("deadline/window outside the declared Colab bounds")
    cpu = name == "COLAB_CPU_TRANSPORT"
    a100 = name == "COLAB_A100_80GB_BF16"
    rerank = name in RERANKERS
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
    if cpu or rerank:
        if reasoning:
            raise ValueError("reasoning inputs need a profile with a reasoning server")
        inputs = None
    else:
        inputs = reasoning_inputs(name, reasoning)
        if inputs != reasoning_inputs(name, None):
            profile["reasoning_inputs"] = inputs
    if cpu:
        return profile
    embedding = ("embed", "Qwen/Qwen3-Embedding-8B", EMBEDDING_REVISION, "embedding", 8001,
                 "0.22" if a100 else "0.24", embed_max_len, 1 if a100 else 8)
    if rerank:
        # Fit estimate, never a measurement: the G4 admission floor is 90 GiB.
        # Embedding 0.24 (21.6 GiB; 14.1 GiB weights, as in the reference
        # profile) + reranker 0.30 (27.0 GiB; 15.3 GiB weights for 8B, 7.5 GiB
        # for 4B; KV is 0.14 MiB/token for both) = 0.54, inside the 0.90 the
        # 27B + 8B reference already uses. The reranker takes the first model
        # slot (:8000) so the CPU transport receipt for ports 8000/8001 holds.
        model, revision = RERANKERS[name]
        models = (embedding, ("rerank", model, revision, "reranker", 8000, "0.30",
                              RERANK_WINDOW, RERANK_SEQUENCES))
    else:
        assert inputs is not None
        models = (("reasoning", inputs["model_id"], inputs["revision"], "reasoning", 8000,
                   "0.73" if a100 else "0.66", inputs["max_model_len"], inputs["max_num_seqs"]),
                  embedding)
    for role, model, revision, path, port, allocation, window, sequences in models:
        argv = ["/content/rag-vllm/bin/vllm", "serve", f"/content/models/{path}",
                "--served-model-name", model, "--dtype", "bfloat16",
                "--tensor-parallel-size", "1"]
        if role == "reasoning":
            assert inputs is not None
            argv.append("--language-model-only")
            if inputs["reasoning_parser"] != REASONING_PARSER_NONE:
                argv += ["--reasoning-parser", inputs["reasoning_parser"]]
        elif role == "rerank":
            argv += ["--runner", "pooling", "--hf-overrides", RERANK_HF_OVERRIDES, "--chat-template",
                     f"/content/models/{path}/{RERANK_CHAT_TEMPLATE}"]
        else:
            argv += ["--runner", "pooling", "--convert", "embed"]
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

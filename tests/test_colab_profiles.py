"""Public-reference model launch/admission contracts; no models or GPU needed."""
import pytest

from mainframe_rag.serve.colab_profiles import admit, build_profile


def _g4_observation():
    return {"gpu_name": "NVIDIA RTX PRO 6000 Blackwell", "vram_mib": 92160,
            "available_ram_mib": 65536, "free_disk_mib": 131072,
            "driver_version": "580.100"}


def test_g4_reference_keeps_immutable_models_and_independent_allocations():
    profile = build_profile("COLAB_G4_BF16")
    reasoning, embedding = profile["servers"]
    assert reasoning["revision"] == "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
    assert embedding["revision"] == "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
    assert reasoning["argv"] == [
        "/content/rag-vllm/bin/vllm", "serve", "/content/models/reasoning",
        "--served-model-name", "Qwen/Qwen3.8-27B", "--dtype", "bfloat16",
        "--tensor-parallel-size", "1", "--language-model-only", "--reasoning-parser", "qwen3",
        "--gpu-memory-utilization", "0.66", "--max-model-len", "32768",
        "--max-num-seqs", "2", "--max-num-batched-tokens", "4096",
        "--enable-chunked-prefill", "--enforce-eager", "--host", "127.0.0.1", "--port", "8000",
    ]
    assert embedding["argv"][-4:] == ["--host", "127.0.0.1", "--port", "8001"]
    assert embedding["argv"][embedding["argv"].index("--gpu-memory-utilization") + 1] == "0.24"
    # #777: production embedding window; the 146k-token reasoning KV pool
    # holds two 32k sequences at 0.66.
    assert embedding["argv"][embedding["argv"].index("--max-model-len") + 1] == "8192"
    assert profile["dense_dim"] == 4096
    with pytest.raises(ValueError, match="assigned GPU"):
        admit(profile, {"gpu_name": "NVIDIA A100-SXM4-40GB", "vram_mib": 40960,
                        "available_ram_mib": 131072, "free_disk_mib": 200000,
                        "driver_version": "580.100"})


# Each capacity field is its own call site; the shared validator rejects a boolean
# (numerically valid), text, an absent value, a nonfinite number, and a shortfall.
MISSING = object()


@pytest.mark.parametrize("field,value", [
    ("vram_mib", 92159), ("available_ram_mib", 65535), ("free_disk_mib", 131071),
    ("vram_mib", True), ("available_ram_mib", "65536"), ("free_disk_mib", float("inf")),
    ("available_ram_mib", MISSING),
])
def test_gpu_admission_refuses_unknown_missing_or_insufficient_capacity(field, value):
    observed = _g4_observation()
    if value is MISSING:
        del observed[field]
    else:
        observed[field] = value
    with pytest.raises(ValueError):
        admit(build_profile("COLAB_G4_BF16"), observed)


@pytest.mark.parametrize("field,unknown", [
    ("available_ram_mib", None), ("free_disk_mib", float("nan")),
])
def test_cpu_transport_also_requires_measured_finite_resources(field, unknown):
    observed = {"available_ram_mib": 1024, "free_disk_mib": 1024}
    observed[field] = unknown
    with pytest.raises(ValueError):
        admit(build_profile("COLAB_CPU_TRANSPORT"), observed)


@pytest.mark.parametrize("as_float", [False, True])
def test_finite_census_capacity_at_declared_minima_is_accepted(as_float):
    observed = _g4_observation()
    if as_float:
        for field in ("vram_mib", "available_ram_mib", "free_disk_mib"):
            observed[field] = float(observed[field])
    admit(build_profile("COLAB_G4_BF16"), observed)
    admit(build_profile("COLAB_CPU_TRANSPORT"), {
        "available_ram_mib": 1024.0 if as_float else 1024,
        "free_disk_mib": 1024.0 if as_float else 1024,
    })


def test_gpu_name_must_be_observed_text_not_a_stringified_container():
    observed = _g4_observation()
    observed["gpu_name"] = ["NVIDIA RTX PRO 6000 Blackwell"]
    with pytest.raises(ValueError, match="assigned GPU"):
        admit(build_profile("COLAB_G4_BF16"), observed)


# Non-int types (bool, a float that equals the bound) and each side of each range.
@pytest.mark.parametrize("field,invalid", [
    ("deadline_seconds", True), ("deadline_seconds", 7200.0),
    ("deadline_seconds", 0), ("deadline_seconds", 7201),
    ("embed_max_len", True), ("embed_max_len", 4096.0),
    ("embed_max_len", 4095), ("embed_max_len", 32769),
])
def test_profile_window_and_deadline_require_bounded_integers(field, invalid):
    with pytest.raises(ValueError, match="deadline/window"):
        build_profile("COLAB_G4_BF16", **{field: invalid})


@pytest.mark.parametrize("window,deadline", [(4096, 1), (32768, 7200)])
def test_profile_accepts_integer_bounds_without_changing_model_arguments(window, deadline):
    profile = build_profile("COLAB_G4_BF16", embed_max_len=window, deadline_seconds=deadline)
    assert profile["deadline_seconds"] == deadline
    embedding = profile["servers"][1]["argv"]
    assert embedding[embedding.index("--max-model-len") + 1] == str(window)
    assert embedding[embedding.index("--max-num-batched-tokens") + 1] == str(window)

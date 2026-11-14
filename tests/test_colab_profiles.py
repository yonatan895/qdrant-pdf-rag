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


# #862: retrieval-only measurement profiles. One Qwen3-Embedding-8B plus ONE
# Qwen3-Reranker, no reasoning server. Revisions are the HF commit SHAs of
# `main` resolved on 2026-10-22 (huggingface.co/api/models/<id>/revision/main).
RERANK_PROFILES = {
    "COLAB_G4_RERANK_8B": ("Qwen/Qwen3-Reranker-8B", "77d193c791ed757ca307ee72715aa132723da912"),
    "COLAB_G4_RERANK_4B": ("Qwen/Qwen3-Reranker-4B", "22e683669bc0f0bd69640a1354a6d0aebcfeede5"),
}
EMBEDDING_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"


@pytest.mark.parametrize("name,expected", sorted(RERANK_PROFILES.items()))
def test_rerank_profiles_serve_embedding_and_one_reranker_without_reasoning(name, expected):
    model, revision = expected
    profile = build_profile(name)
    # Embed first, rerank second; the reranker takes the first model slot (:8000)
    # the other profiles give to reasoning, so the CPU transport rehearsal that
    # forwards 8000/8001 still proves this transport.
    assert [server["role"] for server in profile["servers"]] == ["embed", "rerank"]
    embedding, reranker = profile["servers"]
    assert (embedding["model_id"], embedding["revision"], embedding["port"]) == (
        "Qwen/Qwen3-Embedding-8B", EMBEDDING_REVISION, 8001)
    assert (reranker["model_id"], reranker["revision"], reranker["port"]) == (model, revision, 8000)
    assert reranker["local_dir"] == "/content/models/reranker"
    assert profile["accelerator"] == "G4" and profile["dense_dim"] == 4096
    assert reranker["argv"] == [
        "/content/rag-vllm/bin/vllm", "serve", "/content/models/reranker",
        "--served-model-name", model, "--dtype", "bfloat16", "--tensor-parallel-size", "1",
        "--runner", "pooling", "--hf-overrides",
        ('{"architectures":["Qwen3ForSequenceClassification"],'
         '"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'),
        "--chat-template", "/content/models/reranker/chat_template.jinja",
        "--gpu-memory-utilization", "0.30", "--max-model-len", "8192",
        "--max-num-seqs", "32", "--max-num-batched-tokens", "8192",
        "--enforce-eager", "--host", "127.0.0.1", "--port", "8000",
    ]


@pytest.mark.parametrize("name", sorted(RERANK_PROFILES))
def test_rerank_profiles_keep_the_reference_embedding_server_and_fit_inside_its_budget(name):
    reference = build_profile("COLAB_G4_BF16")
    profile = build_profile(name)
    assert profile["servers"][0] == reference["servers"][1]
    allocations = [float(server["argv"][server["argv"].index("--gpu-memory-utilization") + 1])
                   for server in profile["servers"]]
    # Fit estimate (docs/colab-playground.md): 0.24 + 0.30 = 0.54 of the GPU,
    # well inside the 0.90 the 27B + 8B reference already uses.
    assert allocations == [0.24, 0.30]
    reference_total = sum(float(server["argv"][server["argv"].index("--gpu-memory-utilization") + 1])
                          for server in reference["servers"])
    assert sum(allocations) < reference_total
    for field in ("accelerator", "minimum_vram_mib", "minimum_ram_mib", "minimum_disk_mib", "cuda_build",
                  "vllm_version", "transformers_version", "python_version", "dense_dim"):
        assert profile[field] == reference[field]


@pytest.mark.parametrize("name", sorted(RERANK_PROFILES))
def test_rerank_profiles_admit_only_the_g4_shape(name):
    profile = build_profile(name)
    admit(profile, _g4_observation())
    with pytest.raises(ValueError, match="assigned GPU"):
        admit(profile, {"gpu_name": "NVIDIA A100-SXM4-80GB", "vram_mib": 81920,
                        "available_ram_mib": 131072, "free_disk_mib": 200000,
                        "driver_version": "580.100"})


def test_embedding_window_applies_to_the_rerank_embedder_but_not_the_reranker():
    profile = build_profile("COLAB_G4_RERANK_8B", embed_max_len=4096)
    embedding, reranker = (server["argv"] for server in profile["servers"])
    assert embedding[embedding.index("--max-model-len") + 1] == "4096"
    assert reranker[reranker.index("--max-model-len") + 1] == "8192"
    with pytest.raises(ValueError, match="deadline/window"):
        build_profile("COLAB_G4_RERANK_8B", deadline_seconds=7201)


def test_existing_profiles_keep_their_server_roles_and_order():
    for name in ("COLAB_G4_BF16", "COLAB_A100_80GB_BF16"):
        assert [server["role"] for server in build_profile(name)["servers"]] == ["reasoning", "embed"]
    assert build_profile("COLAB_CPU_TRANSPORT")["servers"] == []


# ---------------------------------------- issue #1195 reasoning-server inputs
REVISION = "0123456789abcdef0123456789abcdef01234567"
# sha256 of the canonical JSON of each default profile at c5259460, the commit
# before the reasoning server took inputs: the unparameterised profile, and so
# the prepared manifest's `profile`, is exactly what it was.
REFERENCE_DIGESTS = {
    "COLAB_CPU_TRANSPORT": "8cab3997dd77c9495bc21602e0c401f41fff338004e2a1037cb0b19fa5a3dd8d",
    "COLAB_G4_BF16": "a8f27a4c57292c821bca6575199b91380c9dde98fa281eeb4d850e6cb9804048",
    "COLAB_A100_80GB_BF16": "49f11a95fa62fbb077b573275b891fa11dfe86dbcfb708ca30d0ad625fd733dd",
    "COLAB_G4_RERANK_8B": "95874206d9009f51891943d8b3313b109f022dc911d96bdfd017f7d21da54b12",
    "COLAB_G4_RERANK_4B": "0171c50fab73a43b3ae37e7fe223bebf91c5901d6c14aa4891694ab20fe8d2da",
}


def _digest(value):
    import hashlib
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _flag(argv, name):
    assert argv.count(name) == 1
    return argv[argv.index(name) + 1]


def test_unparameterised_profiles_are_byte_identical_to_the_reference():
    from mainframe_rag.serve.colab_profiles import COLAB_PROFILES

    assert set(REFERENCE_DIGESTS) == set(COLAB_PROFILES)
    for name, digest in REFERENCE_DIGESTS.items():
        assert _digest(build_profile(name)) == digest, name


@pytest.mark.parametrize("name,sequences", [("COLAB_G4_BF16", 2), ("COLAB_A100_80GB_BF16", 1)])
def test_reference_values_given_explicitly_are_the_unparameterised_profile(name, sequences):
    explicit = build_profile(name, reasoning={
        "model_id": "Qwen/Qwen3.8-27B", "revision": "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
        "max_model_len": 32768, "max_num_seqs": sequences, "reasoning_parser": "qwen3"})
    assert explicit == build_profile(name) == build_profile(name, reasoning={})
    assert "reasoning_inputs" not in explicit and _digest(explicit) == REFERENCE_DIGESTS[name]


def test_reasoning_inputs_produce_the_argv_and_are_recorded_for_the_manifest():
    from mainframe_rag.serve.colab_profiles import reasoning_window

    reference = build_profile("COLAB_G4_BF16")
    profile = build_profile("COLAB_G4_BF16", reasoning={
        "model_id": "Example-Org/Other.Model-9B", "revision": REVISION,
        "max_model_len": 131072, "max_num_seqs": 8, "reasoning_parser": "none"})
    assert profile["reasoning_inputs"] == {
        "model_id": "Example-Org/Other.Model-9B", "revision": REVISION,
        "max_model_len": 131072, "max_num_seqs": 8, "reasoning_parser": "none"}
    reasoning, embedding = profile["servers"]
    assert (reasoning["model_id"], reasoning["revision"]) == ("Example-Org/Other.Model-9B", REVISION)
    argv = reasoning["argv"]
    assert _flag(argv, "--served-model-name") == "Example-Org/Other.Model-9B"
    assert _flag(argv, "--max-model-len") == "131072" and _flag(argv, "--max-num-seqs") == "8"
    assert "--reasoning-parser" not in argv
    # Everything the inputs do not name is the reference: allocations, the
    # loopback bind, the embedding server, the admission minima.
    assert _flag(argv, "--gpu-memory-utilization") == "0.66" and argv[-4:] == ["--host", "127.0.0.1", "--port", "8000"]
    assert embedding == reference["servers"][1]
    assert {k: v for k, v in profile.items() if k not in ("servers", "reasoning_inputs")} == {
        k: v for k, v in reference.items() if k != "servers"}
    assert reasoning_window(profile) == 131072 and reasoning_window(reference) == 32768
    # One input alone keeps the rest at the reference, including its revision.
    window_only = build_profile("COLAB_G4_BF16", reasoning={"max_model_len": 65536})
    assert window_only["servers"][0]["revision"] == reference["servers"][0]["revision"]
    assert _flag(window_only["servers"][0]["argv"], "--reasoning-parser") == "qwen3"
    assert window_only["reasoning_inputs"]["max_num_seqs"] == 2
    # Recorded inputs rebuild the same profile: the round trip consumers rely on.
    assert build_profile("COLAB_G4_BF16", reasoning=profile["reasoning_inputs"]) == profile


@pytest.mark.parametrize("name,inputs", [
    ("COLAB_G4_BF16", {"model_id": "Example/Other"}),  # no revision of its own
    ("COLAB_G4_BF16", {"model_id": "Example/Other", "revision": "main"}),
    ("COLAB_G4_BF16", {"model_id": "../escape/x", "revision": REVISION}),
    ("COLAB_G4_BF16", {"model_id": "no-slash", "revision": REVISION}),
    ("COLAB_G4_BF16", {"model_id": "a/b --trust-remote-code", "revision": REVISION}),
    ("COLAB_G4_BF16", {"revision": REVISION.upper()}),
    ("COLAB_G4_BF16", {"max_model_len": 4095}),
    ("COLAB_G4_BF16", {"max_model_len": 131073}),
    ("COLAB_G4_BF16", {"max_model_len": "65536"}),
    ("COLAB_G4_BF16", {"max_model_len": True}),
    ("COLAB_G4_BF16", {"max_num_seqs": 0}),
    ("COLAB_G4_BF16", {"max_num_seqs": 17}),
    ("COLAB_G4_BF16", {"reasoning_parser": "qwen3 --enable-auto-tool-choice"}),
    ("COLAB_G4_BF16", {"reasoning_parser": ""}),
    ("COLAB_G4_BF16", {"reasoning_parser": None}),
    ("COLAB_G4_BF16", {"gpu_memory_utilization": "0.95"}),  # not an input
    ("COLAB_CPU_TRANSPORT", {"max_model_len": 65536}),
    ("COLAB_G4_RERANK_8B", {"max_model_len": 65536}),
])
def test_reasoning_inputs_fail_closed(name, inputs):
    with pytest.raises(ValueError):
        build_profile(name, reasoning=inputs)


def test_reasoning_window_ceiling_is_the_budget_and_settings_ceiling():
    from mainframe_rag.config import Settings
    from mainframe_rag.serve.budget import ModelSpec
    from mainframe_rag.serve.colab_profiles import REASONING_WINDOW_BOUNDS

    def ceiling(field):
        return next(item.le for item in field.metadata if getattr(item, "le", None) is not None)

    assert REASONING_WINDOW_BOUNDS[1] == ceiling(ModelSpec.model_fields["context_need"]) == ceiling(
        Settings.model_fields["llm_max_model_len"]) == 131072

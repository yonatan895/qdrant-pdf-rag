"""Render + fail-closed tests for scripts/run_local_gateway.sh (local-dev only).

Hermetic: every case runs with GATEWAY_DRYRUN=1 (render-only, exits before
any docker call) and dummy sk-test-* keys, so no daemon, no network, no GPU.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_local_gateway.sh"

DUMMY_KEYS = {
    "GATEWAY_MASTER_KEY": "sk-test-master",
    "GATEWAY_LLM_KEY": "sk-test-llm",
    "GATEWAY_EMBED_KEY": "sk-test-embed",
    "GATEWAY_RERANK_KEY": "sk-test-rerank",
}


def _render(extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GATEWAY_DRYRUN": "1", **DUMMY_KEYS, **(extra_env or {})}
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)


def _read_cfg(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    cfg_dir = Path(proc.stdout.strip().splitlines()[-1])
    try:
        return (cfg_dir / "config.yaml").read_text()
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)


def test_render_routes_three_legs_to_expected_backends():
    text = _read_cfg(_render())
    assert "model_name: google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert "model: openai/google/gemma-4-E4B-it-qat-mobile-ct" in text
    assert "api_base: http://host.docker.internal:8000/v1" in text
    # reasoning_effort must reach vLLM (the agent's control), not be dropped.
    assert 'allowed_openai_params: ["reasoning_effort"]' in text
    assert "model_name: Qwen/Qwen3-Embedding-0.6B" in text
    assert "model: openai/Qwen/Qwen3-Embedding-0.6B" in text
    assert "api_base: http://host.docker.internal:8001/v1" in text
    # Native /rerank goes through the hosted_vllm provider (self-hosted vLLM
    # rerank in the pinned image; Cohere-shaped in/out).
    assert "model_name: BAAI/bge-reranker-v2-m3" in text
    assert "model: hosted_vllm/BAAI/bge-reranker-v2-m3" in text
    assert "api_base: http://host.docker.internal:8002/v1" in text


def test_render_carries_master_key_and_score_passthrough():
    text = _read_cfg(_render())
    assert "master_key: sk-test-master" in text
    # No native /v1/score in LiteLLM: exact-path pass-through to the vLLM
    # backend keeps the score_first wire order exercisable.
    assert 'path: "/v1/score"' in text
    assert 'target: "http://host.docker.internal:8002/v1/score"' in text
    assert 'methods: ["POST"]' in text


def test_render_otel_callback_only_when_endpoint_set():
    # The local waterfall includes the platform stand-in: LiteLLM exports
    # spans when the stack found a local Jaeger. No endpoint (standalone
    # dry-run) keeps the pre-tracing config byte-identical.
    off = _read_cfg(_render())
    assert "litellm_settings" not in off
    on = _read_cfg(_render({"GATEWAY_OTEL_ENDPOINT": "http://host.docker.internal:4318"}))
    assert 'callbacks: ["otel"]' in on


def test_render_honors_url_and_model_overrides():
    text = _read_cfg(
        _render(
            {
                "GATEWAY_RERANK_URL": "http://gpu-box:8002/v1",
                "GATEWAY_RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
            }
        )
    )
    assert "api_base: http://gpu-box:8002/v1" in text
    assert 'target: "http://gpu-box:8002/v1/score"' in text


def test_render_generates_keys_when_unset():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GATEWAY_")}
    env["GATEWAY_DRYRUN"] = "1"
    proc = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, env=env, check=False)
    text = _read_cfg(proc)
    assert "master_key: sk-local-" in text
    # Generated values are terminal-only material: distinct per start, and
    # the committed tree must never contain one.
    assert "sk-test-" not in text


def test_env_file_handoff_written_mode_600(tmp_path):
    env_file = tmp_path / "gateway.env"
    proc = _render({"GATEWAY_ENV_FILE": str(env_file)})
    assert proc.returncode == 0, proc.stderr
    _read_cfg(proc)  # also cleans the render dir
    body = env_file.read_text()
    # Every leg is routed at the gateway with its own key; the master key
    # never leaves the process.
    for line in (
        "export LLM_BASE_URL=http://localhost:4000/v1",
        "export LLM_MODEL_REASONING=google/gemma-4-E4B-it-qat-mobile-ct",
        "export LLM_API_KEY=sk-test-llm",
        "export EMBED_BASE_URL=http://localhost:4000/v1",
        "export EMBED_API_KEY=sk-test-embed",
        "export RERANK_ENABLED=true",
        "export RERANK_BASE_URL=http://localhost:4000/v1",
        "export RERANK_API_KEY=sk-test-rerank",
    ):
        assert line in body, line
    assert "master_key" not in body and "sk-test-master" not in body
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_env_file_absent_when_unset(tmp_path):
    proc = _render({"TMPDIR": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    _read_cfg(proc)
    assert list(tmp_path.glob("*.env")) == []


@pytest.mark.parametrize("var", ["GATEWAY_MASTER_KEY", "GATEWAY_LLM_KEY", "GATEWAY_EMBED_KEY", "GATEWAY_RERANK_KEY"])
def test_bad_key_charset_fails_closed(var):
    proc = _render({var: "sk-bad_$key!"})
    assert proc.returncode != 0
    assert "must contain only alphanumerics" in proc.stderr


@pytest.mark.parametrize(
    "var", ["GATEWAY_REASONING_URL", "GATEWAY_EMBED_URL", "GATEWAY_RERANK_URL"]
)
def test_bare_hostname_url_fails_closed(var):
    proc = _render({var: "gpu-box:8002"})
    assert proc.returncode != 0
    assert "must begin with http:// or https://" in proc.stderr

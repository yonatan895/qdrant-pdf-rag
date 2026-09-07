"""Contract for the Kaggle Path-A notebook (kaggle/path-a-gpu.ipynb).

Pins the notebook's version/flag/mode claims against the repo they must track:
Qdrant + vLLM pins, CLI flags, per-command EMBED_MODE scoping, and the frozen
holdout exclusion. Hermetic: parses files only, no servers, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NB = REPO / "kaggle" / "path-a-gpu.ipynb"


def _sources() -> list[str]:
    nb = json.loads(NB.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4, "notebook must be nbformat 4"
    cells = nb["cells"]
    assert len(cells) >= 10, "notebook looks truncated"
    out = []
    for cell in cells:
        assert cell["cell_type"] in ("markdown", "code"), cell["cell_type"]
        out.append("".join(cell["source"]))
    return out


def _code_sources() -> str:
    nb = json.loads(NB.read_text(encoding="utf-8"))
    return "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")


def test_notebook_is_valid_json_and_code_compiles() -> None:
    sources = _sources()
    code = [s for s in sources if s]
    assert code, "no cell sources found"
    nb = json.loads(NB.read_text(encoding="utf-8"))
    for cell in nb["cells"]:
        if cell["cell_type"] != "code":
            continue
        src = "".join(cell["source"])
        if src.startswith("%%bash"):
            continue  # shell cells: covered by flag assertions below
        compile(src, "<notebook-cell>", "exec")


def test_qdrant_pin_matches_images_txt() -> None:
    code = _code_sources()
    m = re.search(r'QDRANT_VERSION\s*=\s*"([^"]+)"', code)
    assert m, "QDRANT_VERSION pin missing from notebook"
    pinned = next(
        line.split()[0] for line in (REPO / "images.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "qdrant" in line.split()[0]
    )
    tag = pinned.rsplit(":", 1)[1].removesuffix("-unprivileged").lstrip("v")
    assert m.group(1) == tag, f"notebook Qdrant {m.group(1)} != images.txt {tag}"


def test_vllm_pin_matches_run_local_vllm_sh() -> None:
    code = _code_sources()
    m = re.search(r'VLLM_VERSION\s*=\s*"([^"]+)"', code)
    assert m, "VLLM_VERSION pin missing from notebook"
    sh = (REPO / "scripts" / "run_local_vllm.sh").read_text(encoding="utf-8")
    img = re.search(r'VLLM_IMAGE:-([^"\s}]+)', sh)
    assert img, "IMAGE default missing from run_local_vllm.sh"
    assert m.group(1) == img.group(1).rsplit(":v", 1)[1], (
        f"notebook vLLM {m.group(1)} != run_local_vllm.sh {img.group(1)}"
    )


def test_eval_answers_pins_dev_golden_and_never_holdout() -> None:
    code = _code_sources()
    assert "--golden evals/golden.jsonl" in code, "eval-answers must pin the dev golden set"
    assert "holdout.jsonl" not in code, "frozen holdout must never be scored from a demo box"


def test_embed_mode_scoped_per_command_and_no_hash() -> None:
    code = _code_sources()
    assert "export EMBED_MODE" not in code, "EMBED_MODE must be scoped per command, never exported"
    assert "EMBED_MODE=vllm" in code, "Path A is vllm-only"
    assert "ALLOW_HASH_MODE" not in code, "hash mode has no place in the Path-A notebook"
    assert "RERANK" not in code, "rerank ships default-off; the notebook must not enable it"


def test_cli_flags_exist() -> None:
    code = _code_sources()
    for flag in ("--src", "--progress", "--workers", "--limit"):
        assert flag in code, f"run_ingest flag {flag} missing"
    assert "python -m mainframe_rag.ingest.run_ingest" in code
    for flag in ("--answer", "--query", "--collection", "--embed-mode", "--embed-url",
                 "--embed-model", "--dense-dim", "--vllm-url", "--model"):
        assert flag in code, f"query_demo flag {flag} missing"
    assert "generate_synthetic_golden_corpus" in code
    assert "--golden evals/golden.jsonl --max-queries" in code


def test_no_hardcoded_secrets_or_dataset() -> None:
    code = _code_sources()
    assert "UserSecretsClient" in code and "HF_TOKEN" in code, "HF_TOKEN must come from Kaggle secrets"
    assert "<your-dataset-slug>" in code, "dataset path must stay a user-filled placeholder"
    assert "hf_" not in code, "no Hugging Face token may be committed"

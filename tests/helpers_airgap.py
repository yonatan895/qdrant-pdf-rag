"""Shared hermetic harness for scripts/airgap/*.sh tests.

Share builders, pin behavior: per-script env deltas, stub arms, and
fail-close assertions stay explicit at each call site. Only the
mechanical tree/run/sign/stub plumbing lives here.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STUB_TOOL = "#!/bin/sh\nexit 0\n"

PULL_SECRET_RE = re.compile(r"^([ ]*)imagePullSecrets:\n\1  - name: (\S+)$", re.MULTILINE)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_stub(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def make_bin_tree(tmp_path: Path, scripts: list[str]) -> Path:
    """Create bin/ + scripts/airgap/ and copy the named scripts from the repo."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    for f in scripts:
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    return tmp_path


def copy_chart(tmp_path: Path) -> Path:
    (tmp_path / "charts").mkdir(exist_ok=True)
    dest = tmp_path / "charts" / next(REPO.glob("charts/qdrant-*.tgz")).name
    if not dest.exists():
        shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), dest)
    return dest


def symlink_tools(tmp_path: Path, tools: tuple[str, ...]) -> None:
    for tool in tools:
        src = shutil.which(tool)
        if src and not (tmp_path / "bin" / tool).exists():
            (tmp_path / "bin" / tool).symlink_to(src)


def run_sh(script: Path, env: dict, cwd: Path):
    return subprocess.run(
        ["sh", str(script)], capture_output=True, text=True, env=env, cwd=cwd, check=False
    )


def gen_sign_keypair(artdir: Path, key_name: str = "signing.key") -> Path:
    key = artdir / key_name
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def sign_sums(artdir: Path, key: Path | None = None) -> None:
    key = key or (artdir / "signing.key")
    if not (artdir / "sneakernet-signing.pub").exists():
        subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout",
             "-out", str(artdir / "sneakernet-signing.pub")],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key),
         "-out", str(artdir / "SHA256SUMS.sig"), str(artdir / "SHA256SUMS")],
        check=True,
        capture_output=True,
    )


def gen_other_pub(tmp_path: Path, name: str = "other") -> Path:
    other = tmp_path / f"{name}.key"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(other)],
        check=True,
        capture_output=True,
    )
    pub = tmp_path / f"{name}.pub"
    subprocess.run(
        ["openssl", "pkey", "-in", str(other), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    return pub


def skopeo_stub(digest_char: str, materialize: bool = False) -> str:
    """Canned `inspect` digest + optional docker-archive materialization.

    pack.sh needs the materialize arm (it "pulls" into tar files);
    load.sh only needs inspect + arg logging.
    """
    digest = "sha256:" + digest_char * 64
    materialize_arm = ""
    if materialize:
        materialize_arm = """for arg in "$@"; do
  case "$arg" in
    docker-archive:*)
      dest="${arg#docker-archive:}"
      mkdir -p "$(dirname "$dest")"
      printf 'stub-image-tar\\n' > "$dest"
      ;;
  esac
done
"""
    return f"""#!/bin/sh
if [ "$1" = "inspect" ]; then
  printf '{digest}\\n'
  printf '%s\\n' "$@" >> "$SKOPEO_LOG"
  exit 0
fi
{materialize_arm}printf '%s\\n' "$@" >> "$SKOPEO_LOG"
exit 0
"""


def git_init_repo(path: Path, files: dict[str, str] | None = None) -> str:
    """Init a throwaway git repo at path; return HEAD sha."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    for name, content in (files or {"README.md": "Hello"}).items():
        (path / name).write_text(content)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def assert_no_placeholders(text: str) -> None:
    assert "__" not in text


def assert_pull_secret_wired(rendered: str, name: str) -> None:
    assert re.search(
        rf"^([ ]*)imagePullSecrets:\n\1  - name: {re.escape(name)}$", rendered, re.MULTILINE
    )

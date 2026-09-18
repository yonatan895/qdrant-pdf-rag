"""Actual pinned host tool fixture: provisioned explicitly, never downloaded by tests."""

import os
import shutil
import tarfile
from pathlib import Path

from tests.helpers_airgap import REPO, sha256_bytes

TASK_ASSET = "task_linux_amd64.tar.gz"
TASK_SHA256 = "a54a408f6861ff921f6e87774180db31bacd8c1e7c944ca696db9fea49a82fc7"
TASK_BINARY_SHA256 = "48fc3727244d39e0fe04f99c512d0a6eaabf12be376828d96ad1545054523aeb"
TASK_LICENSE_SHA256 = "ae22882adc54d4c2cec2bb1ce5e0b252c7f3344efd55c72144738e8fcc89509b"


def task_archive() -> Path:
    archive = Path(os.environ.get("AIRGAP_TASK_ARCHIVE", REPO / ".tools/cache" / TASK_ASSET))
    assert archive.is_file(), "Provision actual Task archive with sh scripts/tools/install-task.sh before this required artifact lane"
    assert sha256_bytes(archive.read_bytes()) == TASK_SHA256
    return archive


def copy_task_tools(repo: Path) -> None:
    (repo / "scripts/tools").mkdir(parents=True, exist_ok=True)
    for name in ("task-pin.txt", "task-artifact.sh", "install-task.sh"):
        shutil.copy(REPO / "scripts/tools" / name, repo / "scripts/tools" / name)


def task_members() -> dict[str, bytes]:
    archive = task_archive()
    with tarfile.open(archive) as tf:
        license_file = tf.extractfile("LICENSE")
        assert license_file is not None
        license_bytes = license_file.read()
    return {
        TASK_ASSET: archive.read_bytes(),
        "task-pin.txt": (REPO / "scripts/tools/task-pin.txt").read_bytes(),
        "task-LICENSE": license_bytes,
    }


def task_manifest() -> str:
    return (
        "task_version: v3.53.1\ntask_platform: linux-amd64\n"
        f"task_asset: {TASK_ASSET}\ntask_sha256: {TASK_SHA256}\n"
        f"task_binary_sha256: {TASK_BINARY_SHA256}\n"
        f"task_license_sha256: {TASK_LICENSE_SHA256}\n"
    )

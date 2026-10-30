"""Dev kit key, pin and factory contracts (#1118, ADR-0009 §8)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml
from scripts import devkit_key
from scripts.acceptance import PRODUCERS
from scripts.image_inventory import BUILD_CONTEXTS

REPO = Path(__file__).resolve().parents[1]
FIXED = (
    "requirements.dev.lock.txt",
    "locks/cp314-linux-x86_64.json",
    "scripts/tools/task-pin.txt",
    "scripts/tools/helm-pin.txt",
    "scripts/tools/oc-pin.txt",
    "images/devkit-packages.txt",
    "images/Containerfile.devkit",
)
PIN_OK = "# comment\nkey: " + "a" * 64 + "\ndigest: sha256:PENDING\n"


def _tree(tmp_path: Path) -> Path:
    for relative in FIXED:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic {relative}\n")
    (tmp_path / "images/devkit/bin").mkdir(parents=True)
    (tmp_path / "images/devkit/bin/mfr-b").write_text("b\n")
    (tmp_path / "images/devkit/bin/mfr-a").write_text("a\n")
    (tmp_path / "images/devkit/README").write_text("r\n")
    return tmp_path


def _framed(root: Path, order: list[str]) -> str:
    """Independent statement of the framing: domain, then (len, path, len, bytes) each."""
    stream = b"mfr-devkit-key/v1\n"
    for relative in order:
        name, data = relative.encode(), (root / relative).read_bytes()
        stream += len(name).to_bytes(8, "big") + name + len(data).to_bytes(8, "big") + data
    return hashlib.sha256(stream).hexdigest()


def test_key_frames_the_fixed_inputs_in_order_then_the_image_tree_sorted(tmp_path):
    root = _tree(tmp_path)
    expected = _framed(root, [*FIXED, "images/devkit/README", "images/devkit/bin/mfr-a",
                              "images/devkit/bin/mfr-b"])
    assert devkit_key.key(root) == expected
    assert devkit_key.INPUTS == FIXED


@pytest.mark.parametrize("change", [
    *[("edit", relative) for relative in FIXED],
    ("edit", "images/devkit/bin/mfr-a"),
    ("add", "images/devkit/bin/mfr-c"),
    ("remove", "images/devkit/README"),
    ("rename", "images/devkit/bin/mfr-b"),
])
def test_every_image_input_change_changes_the_key(tmp_path, change):
    root = _tree(tmp_path)
    before = devkit_key.key(root)
    action, relative = change
    path = root / relative
    if action == "edit":
        path.write_bytes(path.read_bytes() + b"x")
    elif action == "add":
        path.write_text("c\n")
    elif action == "remove":
        path.unlink()
    else:
        path.rename(path.with_name("mfr-z"))
    assert devkit_key.key(root) != before


def test_moving_bytes_between_a_path_and_its_content_changes_the_key(tmp_path):
    """Unframed concatenation would collide: name "bin/x" + "yz" vs name "bin/xy" + "z"."""
    first = _tree(tmp_path / "first")
    (first / "images/devkit/bin/x").write_text("yz")
    second = _tree(tmp_path / "second")
    (second / "images/devkit/bin/xy").write_text("z")
    assert devkit_key.key(first) != devkit_key.key(second)


@pytest.mark.parametrize("damage", ["missing", "symlink-input", "symlink-in-tree", "symlink-tree"])
def test_key_refuses_missing_or_symlinked_inputs_with_a_fixed_message(tmp_path, damage):
    root = _tree(tmp_path)
    if damage == "missing":
        (root / "scripts/tools/oc-pin.txt").unlink()
        match = "missing or not a regular file: scripts/tools/oc-pin.txt"
    elif damage == "symlink-input":
        target = root / "elsewhere"
        target.write_text("x")
        (root / "images/devkit-packages.txt").unlink()
        (root / "images/devkit-packages.txt").symlink_to(target)
        match = "missing or not a regular file: images/devkit-packages.txt"
    elif damage == "symlink-in-tree":
        (root / "images/devkit/bin/link").symlink_to(root / "images/devkit/README")
        match = "only regular files and directories"
    else:
        (root / "images/devkit").rename(root / "devkit-real")
        (root / "images/devkit").symlink_to(root / "devkit-real")
        match = "not a symlink"
    with pytest.raises(devkit_key.DevkitKeyError, match=match):
        devkit_key.key(root)
    assert devkit_key.main(["--root", str(root)]) == 2


@pytest.mark.parametrize("text", [
    "",
    "key: " + "a" * 64 + "\n",
    "digest: sha256:PENDING\n",
    "key: " + "a" * 63 + "\ndigest: sha256:PENDING\n",
    "key: " + "A" * 64 + "\ndigest: sha256:PENDING\n",
    "key: " + "a" * 64 + "\ndigest: sha256:pending\n",
    "key: " + "a" * 64 + "\ndigest: " + "b" * 64 + "\n",
    "key: " + "a" * 64 + "\nkey: " + "a" * 64 + "\ndigest: sha256:PENDING\n",
    "key " + "a" * 64 + "\ndigest: sha256:PENDING\n",
])
def test_pin_refuses_every_other_shape(tmp_path, text):
    pin = tmp_path / devkit_key.PIN
    pin.parent.mkdir(parents=True)
    pin.write_text(text)
    with pytest.raises(devkit_key.DevkitKeyError, match="missing or malformed"):
        devkit_key.read_pin(tmp_path)


def test_check_names_the_fix_for_a_stale_pin_and_reports_a_pending_digest(tmp_path, capsys):
    root = _tree(tmp_path)
    pin = root / devkit_key.PIN
    pin.write_text(PIN_OK)
    assert devkit_key.main(["--root", str(root), "--check"]) == 2
    err = capsys.readouterr().err
    current = devkit_key.key(root)
    assert "is stale" in err and f"key: {current}" in err and ".github/workflows/devkit.yml" in err
    pin.write_text(PIN_OK.replace("a" * 64, current))
    assert devkit_key.main(["--root", str(root), "--check"]) == 0
    assert "digest pending" in capsys.readouterr().out
    pin.write_text(PIN_OK.replace("a" * 64, current).replace("PENDING", "c" * 64))
    assert devkit_key.main(["--root", str(root), "--check"]) == 0
    assert "sha256:" + "c" * 64 in capsys.readouterr().out
    assert devkit_key.main(["--root", str(root)]) == 0
    assert capsys.readouterr().out == current + "\n"


def test_committed_pin_is_well_formed_but_its_freshness_is_not_a_unit_gate():
    """D-8: a stale pin is refused by kit packing and the release gate, not by unit CI."""
    key, digest = devkit_key.read_pin(REPO)
    assert re.fullmatch(r"[0-9a-f]{64}", key) and re.fullmatch(r"sha256:(PENDING|[0-9a-f]{64})", digest)
    assert re.fullmatch(r"[0-9a-f]{64}", devkit_key.key(REPO))


def _pin_fields(relative: str) -> dict[str, str]:
    fields = {}
    for line in (REPO / relative).read_text().splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition(":")
            fields[name] = value.strip()
    return fields


def test_oc_pin_names_a_verified_rhel9_client_for_the_cluster_minor():
    pin = _pin_fields("scripts/tools/oc-pin.txt")
    assert re.fullmatch(r"4\.20\.\d+", pin["version"])
    assert pin["asset"] == f"openshift-client-linux-amd64-rhel9-{pin['version']}.tar.gz"
    assert pin["origin"] == ("https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/"
                             f"{pin['version']}/{pin['asset']}")
    for field in ("sha256", "binary-sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", pin[field])
    assert re.fullmatch(r"[0-9A-F]{40}", pin["signing-key"])


def test_rpm_list_is_sorted_unique_nevras_covering_the_requested_tools():
    lines = [line for line in (REPO / "images/devkit-packages.txt").read_text().splitlines()
             if line and not line.startswith("#")]
    nevra = re.compile(r"(?P<name>[A-Za-z0-9._+-]+)-(?P<epoch>\d+):[^-\s]+-[^-\s]+\.(x86_64|noarch)")
    assert all(nevra.fullmatch(line) for line in lines)
    assert lines == sorted(set(lines), key=lambda line: line.encode())
    names = {nevra.fullmatch(line)["name"] for line in lines}
    assert {"skopeo", "git-core", "openssl", "tar", "gzip"} <= names


def test_recipe_uses_only_admitted_contexts_and_runs_as_a_non_root_user():
    recipe = (REPO / "images/Containerfile.devkit").read_text()
    contexts = set(re.findall(r"(?:--from=|\bfrom=)([A-Za-z0-9_.-]+)", recipe)) - {"tools"}
    assert contexts == {"wheelhouse", "devkit-tools"} and contexts <= BUILD_CONTEXTS
    users = re.findall(r"^USER\s+(\S+)", recipe, re.MULTILINE)
    assert users[-1] not in ("root", "0") and "COPY --chmod=0755 images/devkit/bin/ /usr/local/bin/" in recipe
    pip = [line for line in recipe.splitlines() if "pip --isolated install" in line]
    assert len(pip) == 1 and "--profile dev" in recipe and "requirements.dev.lock.txt" in pip[0]
    assert not re.search(r"\s(-e|--editable)\s", pip[0])  # the project itself is never installed


def _workflow() -> dict:
    document = yaml.safe_load((REPO / ".github/workflows/devkit.yml").read_text())
    return {"on": document.get("on", document.get(True)), **document}


def test_factory_triggers_on_exactly_the_kit_inputs_and_stays_out_of_acceptance():
    workflow = _workflow()
    expected = {*FIXED, "images/devkit/**", "scripts/devkit_key.py", ".github/workflows/devkit.yml"}
    assert set(workflow["on"]["pull_request"]["paths"]) == expected
    assert set(workflow["on"]["push"]["paths"]) == expected and workflow["on"]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in workflow["on"]
    assert "devkit.yml" not in {producer.workflow for producer in PRODUCERS}


def test_factory_least_permissions_pinned_actions_and_buildkit():
    workflow = _workflow()
    e2e = (REPO / ".github/workflows/e2e.yml").read_text()
    assert workflow["permissions"] == {"contents": "read"}
    build, publish = workflow["jobs"]["build"], workflow["jobs"]["publish"]
    assert build["permissions"] == {"contents": "read"}
    assert build["if"] == "github.event_name == 'pull_request'"
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    for job in (build, publish):
        assert job["runs-on"] == "ubuntu-26.04"
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
                if step["uses"].startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False
    buildkit = workflow["env"]["BUILDKIT_IMAGE"]
    assert re.fullmatch(r"moby/buildkit:v[\d.]+@sha256:[0-9a-f]{64}", buildkit) and buildkit in e2e
    push = next(step["run"] for step in publish["steps"] if "push-by-digest=true" in step.get("run", ""))
    assert "rewrite-timestamp=true" in push and "imagetools create -t \"$IMAGE:$KEY\"" in push
    assert push.index("imagetools inspect \"$IMAGE:$KEY\"") < push.index("imagetools create")

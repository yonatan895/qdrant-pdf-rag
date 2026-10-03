"""Third-party license inventory check (issue #376).

Proves the release-visibility contract of scripts/license_inventory.py against the
real repository record and synthetic changes: an unrecorded or changed
dependency, image pin, vendored asset or notice is flagged; a restricted license
class cannot rest on declared metadata; owner approval goes stale when records
change. None of this is a legal verdict, and no test marks a license approved.
"""
import copy
import shutil
import zipfile

import pytest
from scripts import license_inventory as li

from tests.helpers_airgap import REPO, copy_chart, copy_license_inputs
from tests.helpers_task_artifact import copy_task_tools


def real_inventory():
    return li.load_inventory(REPO)


def problems_for(inventory, root=REPO, release=False, found=None):
    problems, unresolved, state = li.run_check(root, inventory, found, release)
    return problems, unresolved, state


@pytest.mark.parametrize("expression, expected", [
    ("MIT", "permissive"),
    ("Apache-2.0 OR BSD-2-Clause", "permissive"),
    ("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "permissive"),
    ("MPL-2.0 AND MIT", "weak-copyleft"),
    ("GPL-3.0-or-later", "strong-copyleft"),
    ("GPL-2.0-only WITH Classpath-exception-2.0", "strong-copyleft"),
    ("AGPL-3.0-only", "network-copyleft"),
    ("AGPL-3.0-only OR LicenseRef-Artifex-Commercial", "election-required"),
    ("MIT AND LicenseRef-Vendor-EULA", "proprietary"),
    ("(MIT OR Apache-2.0) AND MPL-2.0", "weak-copyleft"),
    ("Some Free Text License", "unknown"),
    ("MIT AND", "unknown"),
    ("", "unknown"),
])
def test_expression_class_is_conservative(expression, expected):
    assert li.expression_class(expression) == expected


def test_recorded_election_decides_a_dual_license():
    expression = "AGPL-3.0-only OR LicenseRef-Artifex-Commercial"
    assert li.expression_class(expression, "AGPL-3.0-only") == "network-copyleft"
    assert li.expression_class(expression, "LicenseRef-Artifex-Commercial") == "proprietary"


def test_repository_record_reconciles_with_the_real_pins():
    problems, unresolved, state = problems_for(real_inventory())
    assert problems == []
    assert state == "absent"
    assert unresolved, "open owner decisions must stay visible, not be dropped"


def test_every_locked_runtime_wheel_has_a_record_with_its_exact_identity():
    from scripts import dependency_lock as locks

    _, locked = locks.load(REPO, "runtime")
    recorded = {e["name"]: e for e in real_inventory()["python"]}
    assert set(recorded) == set(locked)
    for name, entry in locked.items():
        assert (recorded[name]["version"], recorded[name]["wheel"], recorded[name]["sha256"]) == (
            entry["version"], entry["wheel"], entry["sha256"])


def test_pymupdf_cannot_be_recorded_as_a_declared_permissive_dependency():
    entry = next(e for e in real_inventory()["python"] if e["name"] == "pymupdf")
    assert entry["class"] == "election-required"
    assert entry["status"] in {"pending-owner-decision", "approved"}
    assert "AGPL" in entry["license_spdx"]
    inventory = real_inventory()
    target = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    target["status"] = "declared"
    problems, _, _ = problems_for(inventory)
    assert any("pymupdf" in p and "cannot rest on declared metadata" in p for p in problems)
    assert "AFFERO" in target["license_declared"]


def test_new_locked_dependency_without_a_record_is_flagged():
    inventory = real_inventory()
    removed = inventory["python"].pop(5)
    problems, _, _ = problems_for(inventory)
    assert [p for p in problems if removed["name"] in p and "no license record" in p]


def test_record_for_a_dependency_not_in_the_lock_is_flagged():
    inventory = real_inventory()
    ghost = copy.deepcopy(inventory["python"][0]) | {"name": "ghost-package"}
    inventory["python"].append(ghost)
    problems, _, _ = problems_for(inventory)
    assert any("ghost-package" in p and "not in the locked runtime profile" in p for p in problems)


@pytest.mark.parametrize("field", ["version", "wheel", "sha256"])
def test_version_or_wheel_bump_without_review_is_flagged(field):
    inventory = real_inventory()
    inventory["python"][0][field] = "0" * 64 if field == "sha256" else "9.9.9"
    problems, _, _ = problems_for(inventory)
    assert any(f"recorded {field} differs from the lock" in p for p in problems)


def test_class_claim_must_follow_the_recorded_expression():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "click")
    entry["license_spdx"] = "AGPL-3.0-only"
    problems, _, _ = problems_for(inventory)
    assert any("click" in p and "network-copyleft" in p for p in problems)
    entry["class"] = "network-copyleft"
    problems, _, _ = problems_for(inventory)
    assert any("click" in p and "declared metadata" in p for p in problems)


def test_unrecognized_license_id_is_unknown_and_not_approvable_by_declaration():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "click")
    entry["license_spdx"] = "Totally-New-License-1.0"
    entry["class"] = "unknown"
    problems, _, _ = problems_for(inventory)
    assert any("click" in p and "unknown" in p for p in problems)


def test_approved_status_needs_a_protected_reference():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    entry["status"] = "approved"
    problems, _, _ = problems_for(inventory)
    assert any("pymupdf" in p and "approval_ref" in p for p in problems)


def test_weak_copyleft_requires_a_recorded_obligation():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "certifi")
    del entry["obligation"]
    problems, _, _ = problems_for(inventory)
    assert any("certifi" in p and "obligation" in p for p in problems)


def test_wheel_without_license_text_cannot_be_declared():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if not e["license_files"])
    entry["status"] = "declared"
    problems, _, _ = problems_for(inventory)
    assert any(entry["name"] in p and "no license text" in p for p in problems)


def test_release_mode_blocks_on_unresolved_decisions_and_missing_approval():
    problems, unresolved, state = problems_for(real_inventory(), release=True)
    assert state == "absent"
    assert any("awaits owner decision" in p and "pymupdf" in p for p in problems)
    assert any("release approval: no owner approval" in p for p in problems)
    assert len(unresolved) >= 1


def resolve_everything(inventory):
    for group in ("python", "components"):
        for entry in inventory[group]:
            if entry["status"] == "pending-owner-decision":
                entry["status"] = "approved"
                entry["approval_ref"] = "protected-record-ref"


def test_release_approval_is_bound_to_the_exact_inventory_and_goes_stale():
    inventory = real_inventory()
    resolve_everything(inventory)
    inventory["release_approval"] = {"approval_ref": "protected-record-ref", "approved_digest": li.scope_digest(inventory)}
    problems, unresolved, state = problems_for(inventory, release=True)
    assert (problems, unresolved, state) == ([], [], "current")
    # Decision fields do not move the digest; any license/dependency/asset record does.
    inventory["python"][3]["sha256"] = "0" * 64
    inventory["python"][3]["version"] = "9.9.9"
    problems, _, state = problems_for(inventory, release=True)
    assert state == "stale"
    assert any("changed since the approved digest" in p for p in problems)


def test_scope_digest_ignores_decision_fields_only():
    inventory = real_inventory()
    before = li.scope_digest(inventory)
    inventory["python"][0]["status"] = "approved"
    inventory["python"][0]["approval_ref"] = "x"
    inventory["python"][0]["note"] = "n"
    assert li.scope_digest(inventory) == before
    inventory["python"][0]["license_spdx"] = "ISC"
    assert li.scope_digest(inventory) != before


# --- tree-level changes: images, vendored assets, charts, notices -------------


@pytest.fixture
def tree(tmp_path):
    copy_license_inputs(tmp_path)
    copy_task_tools(tmp_path)
    copy_chart(tmp_path)
    shutil.copy(REPO / "images.txt", tmp_path / "images.txt")
    shutil.copy(REPO / "requirements.lock.txt", tmp_path / "requirements.lock.txt")
    shutil.copytree(REPO / "locks", tmp_path / "locks")
    assert li.main(["--root", str(tmp_path), "check"]) == 0
    return tmp_path


def run(tree, capsys, *args):
    code = li.main(["--root", str(tree), "check", *args])
    return code, capsys.readouterr().out


def test_unrecorded_image_pin_is_flagged_then_passes_after_the_record_is_added(tree, capsys):
    images = tree / "images.txt"
    images.write_text(images.read_text() + "docker.io/example/newcomponent:1.0  sha256:" + "c" * 64 + "\n")
    code, out = run(tree, capsys)
    assert code == 1 and "newcomponent:1.0: images.txt entry has no license record" in out
    images.write_text(images.read_text().rsplit("docker.io/example", 1)[0])
    assert run(tree, capsys)[0] == 0


def test_changed_image_digest_requires_re_review(tree, capsys):
    images = tree / "images.txt"
    images.write_text(images.read_text().replace("sha256:a0e04fe6", "sha256:b0e04fe6"))
    code, out = run(tree, capsys)
    assert code == 1 and "image:qdrant" in out and "re-review the image" in out


def test_unrecorded_vendored_asset_and_second_chart_are_flagged(tree, capsys):
    vendor = tree / "src/mainframe_rag/webui/static/vendor"
    (vendor / "extra-lib.js").write_text("// new vendored library\n")
    shutil.copy(next((tree / "charts").glob("qdrant-*.tgz")), tree / "charts" / "qdrant-9.9.9.tgz")
    code, out = run(tree, capsys)
    assert code == 1
    assert "vendored asset extra-lib.js: no license record" in out
    assert "chart qdrant-9.9.9.tgz: vendored chart archive has no license record" in out


@pytest.mark.parametrize("relative", ["NOTICE.qdrant-skills", "LICENSE.qdrant-skills",
                                      "src/mainframe_rag/webui/static/vendor/LICENSE.htmx", "LICENSE"])
def test_missing_or_altered_notice_file_is_flagged(tree, capsys, relative):
    (tree / relative).write_bytes((tree / relative).read_bytes() + b"\nchanged\n")
    code, out = run(tree, capsys)
    assert code == 1 and "differs from the recorded hash" in out
    (tree / relative).unlink()
    code, out = run(tree, capsys)
    assert code == 1 and "missing or differs" in out


def test_pin_file_changes_require_re_review(tree, capsys):
    (tree / "bm25-weights.sha256").write_text((tree / "bm25-weights.sha256").read_text() + "# revision bump\n")
    (tree / "scripts/tools/task-pin.txt").write_text(
        (tree / "scripts/tools/task-pin.txt").read_text().replace("v3.53.1", "v3.54.0"))
    code, out = run(tree, capsys)
    assert code == 1
    assert "model:bm25-sparse-weights" in out and "tool:task" in out


def test_project_license_change_in_pyproject_is_flagged(tree, capsys):
    path = tree / "pyproject.toml"
    path.write_text(path.read_text().replace('"Apache-2.0"', '"AGPL-3.0-only"'))
    code, out = run(tree, capsys)
    assert code == 1 and "pyproject.toml license differs from the record" in out


def test_invalid_inventory_fails_with_a_fixed_message(tree, capsys):
    (tree / "licenses/inventory.json").write_text("{not json")
    code, out = run(tree, capsys)
    assert code == 2 and out.strip() == "license-inventory: FAIL: license inventory or its inputs are invalid"


# --- artifact metadata re-derivation -------------------------------------------


def synthetic_installed(tmp_path, name, version, metadata_lines, files):
    info = tmp_path / "site-packages" / f"{name}-{version}.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.4\n" + f"Name: {name}\nVersion: {version}\n" + metadata_lines)
    for relative, data in files.items():
        (info / relative).write_bytes(data)
    return tmp_path / "site-packages"


def test_license_change_in_artifact_metadata_is_flagged_and_matching_metadata_passes(tmp_path):
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "click")
    site = synthetic_installed(tmp_path, "click", entry["version"],
                               "License-Expression: GPL-3.0-only\n", {"licenses/LICENSE.txt": b"gpl text"})
    found = li.collect_dists([], [site])
    problems, _, _ = problems_for(inventory, found=found)
    mine = [p for p in problems if "python:click==" in p]
    assert any("license_declared differs from artifact metadata" in p for p in mine)
    assert any("license_files differs from artifact metadata" in p for p in mine)
    # Once the record is deliberately updated to what the artifact declares it matches.
    derived = li.derived_fields(found[("click", entry["version"])][0])
    entry.update(derived)
    problems, _, _ = problems_for(inventory, found=found)
    assert not [p for p in problems if "python:click==" in p and "artifact metadata" in p]


def test_wheel_bytes_with_a_different_hash_are_not_accepted_as_the_locked_wheel(tmp_path):
    entry = next(e for e in real_inventory()["python"] if e["name"] == "click")
    wheel = tmp_path / entry["wheel"]
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"click-{entry['version']}.dist-info/METADATA",
                         f"Name: click\nVersion: {entry['version']}\nLicense-Expression: BSD-3-Clause\n")
    found = li.collect_dists([tmp_path], [])
    from scripts import dependency_lock as locks

    _, locked = locks.load(REPO, "runtime")
    assert li.select_dist("click", locked["click"], found) is None


def test_declared_license_precedence_and_seeding():
    parse = li.parse_metadata
    assert li.declared_license(parse(b"License-Expression: MIT\nLicense: other\n")) == ("MIT", "License-Expression")
    assert li.declared_license(parse(b"License: Apache 2.0\n")) == ("Apache 2.0", "License")
    assert li.declared_license(parse(b"Classifier: License :: OSI Approved :: MIT License\n"))[1] == "Classifier"
    assert li.declared_license(parse(b"Name: x\n")) == ("", "none")
    assert li.seed_spdx("Apache 2.0", "License") == ("Apache-2.0", "normalized-license-field")
    assert li.seed_spdx("Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License", "License")[0] == "UNRECORDED"
    assert li.fingerprint("MIT License\n\nCopyright") == "MIT"


# --- notices ---------------------------------------------------------------


def notice_inventory(tmp_path, text=b"Synthetic MIT task license\n"):
    inventory = real_inventory()
    task = next(c for c in inventory["components"] if c["id"] == "tool:task")
    task["bind"]["bundle_member"]["sha256"] = li.sha256_bytes(text)
    (tmp_path / "task-LICENSE").write_bytes(text)
    return inventory


def test_notices_carry_real_texts_and_open_decisions_and_refuse_a_missing_member(tmp_path):
    inventory = notice_inventory(tmp_path)
    text = li.render_notices(REPO, inventory, tmp_path)
    assert "Apache License" in text and "Zero-Clause BSD" in text
    assert "### tool:task: task-LICENSE" in text and "Synthetic MIT task license" in text
    assert "### chart:qdrant-1.19.0: charts/qdrant-1.19.0.tgz:qdrant/LICENSE" in text
    assert "pymupdf==1.28.2" in text.split("COMPONENTS")[0]
    assert "Owner approval of this inventory: absent" in text
    (tmp_path / "task-LICENSE").write_bytes(b"tampered")
    with pytest.raises(li.InventoryError):
        li.render_notices(REPO, inventory, tmp_path)
    with pytest.raises(li.InventoryError):
        li.render_notices(REPO, inventory, None)


def test_notices_are_deterministic(tmp_path):
    inventory = notice_inventory(tmp_path)
    assert li.render_notices(REPO, inventory, tmp_path) == li.render_notices(REPO, inventory, tmp_path)


def test_script_is_stdlib_only_and_offline():
    source = (REPO / "scripts/license_inventory.py").read_text()
    for banned in ("import requests", "import httpx", "urllib.request", "import socket", "subprocess"):
        assert banned not in source

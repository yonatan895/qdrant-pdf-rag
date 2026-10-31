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

# Open WebUI v0.12.3-slim upstream index qualified by the #1073 Kind trial.
BASE_DIGEST = "5adfb0355bbe4a5860c0ba9482cc0b40f573de3749a21a64cf4789391a14fffc"
OPENWEBUI_DIGEST = "8417d47dd04505cac429c8b50c0d9452f635ae23a6c17351299b703b8eb04e04"


def real_inventory():
    return li.load_inventory(REPO)


def problems_for(inventory, root=REPO, release=False, found=None):
    problems, unresolved, state = li.run_check(root, inventory, found, release)
    return problems, unresolved, state


@pytest.mark.parametrize("expression, expected", [
    ("MIT", "permissive"),
    ("MPL-2.0 AND MIT", "weak-copyleft"),
    ("GPL-3.0-or-later", "strong-copyleft"),
    ("GPL-2.0-only WITH Classpath-exception-2.0", "strong-copyleft"),
    ("AGPL-3.0-only", "network-copyleft"),
    ("OFL-1.1", "permissive"),
    ("MIT-0", "permissive"),
    ("(MIT OR Apache-2.0) AND MPL-2.0", "weak-copyleft"),
    ("AGPL-3.0-only OR LicenseRef-Artifex-Commercial", "election-required"),
    ("MIT AND LicenseRef-Vendor-EULA", "proprietary"),
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
    assert unresolved == [], "every owner decision is recorded; pymupdf is the owner-decision exception"


def test_every_locked_runtime_wheel_has_a_record_with_its_exact_identity():
    from scripts import dependency_lock as locks

    _, locked = locks.load(REPO, "runtime")
    recorded = {e["name"]: e for e in real_inventory()["python"]}
    assert set(recorded) == set(locked)
    for name, entry in locked.items():
        assert (recorded[name]["version"], recorded[name]["wheel"], recorded[name]["sha256"]) == (
            entry["version"], entry["wheel"], entry["sha256"])


def test_pymupdf_election_is_recorded_as_an_owner_decision_exception_to_legal_confirmation():
    inventory = real_inventory()
    entry = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    assert entry["decision"]["owner"] == "yonatan895" and entry["decision"]["date"] == "2026-10-26"
    assert entry["decision"]["scope"] == "ingest" and entry["elected"] == "AGPL-3.0-only"
    assert entry["decision"]["legal_confirmation"] == "waived-by-owner-decision"
    assert entry["status"] == "approved"
    assert "issuecomment-6427197208" in entry["approval_ref"]
    assert "explicit exception to legal confirmation" in entry["approval_ref"]
    assert "withdrawn if legal confirmation is refused or the use changes" in entry["approval_ref"]
    problems, unresolved, _ = problems_for(inventory)
    assert problems == [] and unresolved == []
    # Withdrawal: back to pending, the item is unresolved again and release refuses it.
    entry["status"] = "pending-owner-decision"
    entry["decision"]["legal_confirmation"] = "pending"
    _, unresolved, _ = problems_for(inventory)
    assert [u for u in unresolved if "pymupdf" in u] and len(unresolved) == 1
    entry["decision"] = {"owner": "yonatan895"}
    problems, _, _ = problems_for(inventory)
    assert any("decision record needs owner, date and disposition" in p for p in problems)
    # An elected AGPL entry can never be recorded as a declared permissive one.
    assert entry["elected"] == "AGPL-3.0-only" and entry["class"] == "network-copyleft"
    assert "AFFERO" in entry["license_declared"]
    entry["status"] = "declared"
    problems, _, _ = problems_for(inventory)
    assert any("pymupdf" in p and "cannot rest on declared metadata" in p for p in problems)


def test_recorded_decisions_match_the_maintainer_dispositions():
    inventory = real_inventory()
    by_name = {e["name"]: e for e in inventory["python"]}
    by_id = {e["id"]: e for e in inventory["components"]}
    for name in ("flatbuffers", "loguru", "onnxruntime", "tokenizers"):
        entry = by_name[name]
        assert entry["status"] == "approved" and entry["approval_ref"]
        bound = entry["bind"]["files"][0]
        assert bound["notice"] and bound["source_url"].startswith("https://raw.githubusercontent.com/")
    for cid in ("image:qdrant", "image:ubi9-python-314-minimal", "image:oauth-proxy",
                "asset:ibm-plex", "model:bm25-sparse-weights"):
        assert by_id[cid]["status"] == "approved" and by_id[cid]["decision"]["owner"] == "yonatan895"
    assert by_id["asset:ibm-plex"]["class"] == "permissive"
    # ADR-0006 (#956): AGPL OpenObserve is network-copyleft but approved for the
    # unmodified binary; otelcol-contrib is permissive. Both are bound to images.txt.
    assert by_id["image:openobserve"]["class"] == "network-copyleft"
    assert by_id["image:openobserve"]["status"] == "approved" and by_id["image:openobserve"]["obligation"]
    assert by_id["image:otelcol-contrib"]["license_spdx"] == "Apache-2.0"
    assert "image:jaeger" not in by_id  # OpenObserve is the only trace backend (#956)
    assert by_id["image:openobserve"]["bind"]["images_txt"].endswith("openobserve:v1.1.2")
    assert by_id["image:oauth-proxy"]["decision"]["escalation"] == "maintainer -> platform team"
    assert by_id["model:bm25-sparse-weights"]["license_spdx"] == "Apache-2.0"
    rpm_list = REPO / "licenses/ubi9-python-314-minimal.rpms.txt"
    assert len([ln for ln in rpm_list.read_text().splitlines() if ln and not ln.startswith("#")]) == 116


def test_open_webui_licence_records_the_maintainer_decision_and_its_conditions():
    """ADR-0007 L1 (#1074): both Open WebUI images.txt lines (derived bundle
    member and upstream build base) carry a record bound to their pins, classed
    proprietary (BSD-3 plus a branding clause, non-OSI), approved by the
    2026-10-24 maintainer decision with its conditions kept in the record."""
    inventory = real_inventory()
    by_id = {e["id"]: e for e in inventory["components"]}
    binds = {
        "image:openwebui": "ghcr.io/yonatan895/qdrant-pdf-rag-openwebui:v0.12.3-slim",
        "image:openwebui-base": "ghcr.io/open-webui/open-webui:v0.12.3-slim",
    }
    for cid, ref in binds.items():
        entry = by_id[cid]
        digest = "sha256:" + (BASE_DIGEST if cid == "image:openwebui-base" else OPENWEBUI_DIGEST)
        assert entry["bind"] == {"images_txt": ref, "digest": digest}, cid
        assert (entry["license_spdx"], entry["class"]) == ("LicenseRef-Open-WebUI-License", "proprietary")
        # ADR-0007 (accepted 2026-10-24) L1, ADR-0008 (accepted 2026-10-26): approved, bound to the digest.
        assert entry["status"] == "approved"
        assert "ADR-0007 L1 (accepted 2026-10-24" in entry["approval_ref"]
        assert "ADR-0008 (accepted 2026-10-26)" in entry["approval_ref"]
        assert "pending acceptance" not in entry["approval_ref"]
        assert entry["decision"]["disposition"].startswith(
            "maintainer disposition 2026-10-24 (ADR-0007 accepted 2026-10-24)")
        assert entry["decision"]["owner"] == "yonatan895" and entry["decision"]["date"] == "2026-10-24"
        assert "branding" in entry["obligation"] and "100 users" in entry["obligation"]
    assert "unmodified" in by_id["image:openwebui"]["decision"]["disposition"]
    assert "100 users" in by_id["image:openwebui"]["decision"]["disposition"]
    assert "not an OSI-approved" in by_id["image:openwebui"]["license_declared"]
    problems, unresolved, _ = problems_for(inventory)
    assert problems == [] and [u for u in unresolved if "openwebui" in u] == []
    # Without the decision the restricted class cannot rest on declared metadata.
    del by_id["image:openwebui"]["decision"]
    by_id["image:openwebui"].update(status="declared", approval_ref=None)
    problems, _, _ = problems_for(inventory)
    assert any("component:image:openwebui: class proprietary cannot rest on declared metadata" in p
               for p in problems)
    # ADR-0007 E1: the nginx allow-list sidecar is an images.txt member with its own
    # record, bound to the pinned digest and classed permissive.
    proxy = by_id["image:openwebui-proxy"]
    line = next(ln.split() for ln in (REPO / "images.txt").read_text().splitlines()
                if not ln.startswith("#") and "nginx-unprivileged" in ln)
    assert proxy["bind"] == {"images_txt": line[0], "digest": line[1]}
    assert (proxy["license_spdx"], proxy["class"]) == ("BSD-2-Clause", "permissive")
    assert proxy["ships_in"] == ["openwebui-proxy-image.tar (when the Open WebUI member is bundled)"]
    assert proxy["status"] == "approved" and "pending acceptance" not in proxy["approval_ref"]
    assert "ADR-0007 E1 (accepted 2026-10-24)" in proxy["approval_ref"]


def test_altered_python_notice_text_is_flagged_and_notices_carry_the_four_texts(tmp_path):
    inventory = notice_inventory(tmp_path)
    text = li.render_notices(REPO, inventory, tmp_path)
    for name in ("flatbuffers", "loguru", "onnxruntime", "tokenizers"):
        assert f"### python:{name}: licenses/texts/{name}.LICENSE" in text
    assert "Permission is hereby granted, free of charge" in text
    inventory["python"][[e["name"] for e in inventory["python"]].index("loguru")]["bind"]["files"][0]["sha256"] = "0" * 64
    problems, _, _ = problems_for(inventory)
    assert any("python:loguru" in p and "differs from the recorded hash" in p for p in problems)
    with pytest.raises(li.InventoryError):
        li.render_notices(REPO, inventory, tmp_path)


def _drop_record(inventory):
    return inventory["python"].pop(5)["name"]


def _add_ghost(inventory):
    ghost = copy.deepcopy(inventory["python"][0]) | {"name": "ghost-package"}
    inventory["python"].append(ghost)
    return "ghost-package"


def _bump(field):
    def mutate(inventory):
        entry = inventory["python"][0]
        entry[field] = "0" * 64 if field == "sha256" else "9.9.9"
        return entry["name"]
    return mutate


def _elect_agpl(inventory):
    next(e for e in inventory["python"] if e["name"] == "click")["license_spdx"] = "AGPL-3.0-only"
    return "click"


def _claim_network_copyleft(inventory):
    _elect_agpl(inventory)
    next(e for e in inventory["python"] if e["name"] == "click")["class"] = "network-copyleft"
    return "click"


def _drop_approval(inventory):
    next(e for e in inventory["python"] if e["name"] == "pymupdf")["approval_ref"] = None
    return "pymupdf"


def _drop_obligation(inventory):
    del next(e for e in inventory["python"] if e["name"] == "certifi")["obligation"]
    return "certifi"


def _declare_without_text(inventory):
    entry = next(e for e in inventory["python"] if not e["license_files"])
    entry["status"] = "declared"
    return entry["name"]


@pytest.mark.parametrize(("mutate", "needle"), [
    (_drop_record, "no license record"),
    (_add_ghost, "not in the locked runtime profile"),
    (_bump("version"), "recorded version differs from the lock"),
    (_bump("wheel"), "recorded wheel differs from the lock"),
    (_bump("sha256"), "recorded sha256 differs from the lock"),
    (_elect_agpl, "network-copyleft"),
    (_claim_network_copyleft, "declared metadata"),
    (_drop_approval, "approval_ref"),
    (_drop_obligation, "obligation"),
    (_declare_without_text, "no license text"),
], ids=["unrecorded-dependency", "ghost-dependency", "version-bump", "wheel-bump",
        "hash-bump", "class-mismatch", "class-claim-needs-decision", "missing-approval-ref",
        "missing-obligation", "declared-without-text"])
def test_record_drift_is_flagged(mutate, needle):
    """An unrecorded, ghosted, bumped or hand-classed dependency is flagged; an
    approved restricted class needs its protected reference, obligation and text."""
    inventory = real_inventory()
    name = mutate(inventory)
    problems, _, _ = problems_for(inventory)
    assert any(name in p and needle in p for p in problems), problems


def test_release_mode_blocks_on_unresolved_decisions_and_missing_approval():
    problems, unresolved, state = problems_for(real_inventory(), release=True)
    assert state == "absent"
    assert unresolved == []
    assert problems == ["release approval: no owner approval is recorded for this inventory"]
    # Withdrawal: an unresolved item must block the release, not only the missing approval.
    inventory = real_inventory()
    pymupdf = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    pymupdf["status"] = "pending-owner-decision"
    pymupdf["decision"]["legal_confirmation"] = "pending"
    problems, unresolved, _ = problems_for(inventory, release=True)
    assert len(unresolved) == 1
    assert any("python:pymupdf" in p and "awaits owner decision" in p for p in problems)
    assert any("pymupdf: legal confirmation of the recorded decision is pending" in p for p in problems)
    assert any("release approval: no owner approval" in p for p in problems)


@pytest.mark.parametrize("value", ["refused", "Pending", "pending ", "pendng", "", None, 3])
def test_unknown_legal_confirmation_value_fails_check(value):
    inventory = real_inventory()
    pymupdf = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    pymupdf["decision"]["legal_confirmation"] = value
    problems, _, _ = problems_for(inventory)
    assert any("pymupdf" in p and "decision.legal_confirmation must be one of pending, confirmed, "
               "waived-by-owner-decision" in p for p in problems)


def test_missing_legal_confirmation_on_an_elected_entry_fails_check():
    inventory = real_inventory()
    pymupdf = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    del pymupdf["decision"]["legal_confirmation"]
    problems, _, _ = problems_for(inventory)
    assert any("decision.legal_confirmation must be one of" in p for p in problems)
    del pymupdf["decision"]
    problems, _, _ = problems_for(inventory)
    assert any("decision.legal_confirmation must be one of" in p for p in problems)


@pytest.mark.parametrize("value", ["confirmed", "waived-by-owner-decision"])
def test_recognized_legal_confirmation_values_pass(value):
    inventory = real_inventory()
    next(e for e in inventory["python"] if e["name"] == "pymupdf")["decision"]["legal_confirmation"] = value
    assert problems_for(inventory, release=True)[0] == [
        "release approval: no owner approval is recorded for this inventory"]


def resolve_everything(inventory):
    for group in ("python", "components"):
        for entry in inventory[group]:
            if entry["status"] == "pending-owner-decision":
                entry["status"] = "approved"
                entry["approval_ref"] = "protected-record-ref"
            if (entry.get("decision") or {}).get("legal_confirmation") == "pending":
                entry["decision"]["legal_confirmation"] = "confirmed"


def test_release_refuses_a_pending_legal_confirmation_even_when_approved_and_current():
    """#376: pymupdf's election is approved only pending legal confirmation. Flipping its
    status and recording a current release approval must still not release it."""
    inventory = real_inventory()
    resolve_everything(inventory)
    pymupdf = next(e for e in inventory["python"] if e["name"] == "pymupdf")
    pymupdf["decision"]["legal_confirmation"] = "pending"
    inventory["release_approval"] = {"approval_ref": "protected-record-ref", "approved_digest": li.scope_digest(inventory)}
    problems, unresolved, state = problems_for(inventory, release=True)
    assert state == "current" and unresolved == []
    assert problems == ["pymupdf: legal confirmation of the recorded decision is pending"]
    pymupdf["decision"]["legal_confirmation"] = "confirmed"
    assert problems_for(inventory, release=True) == ([], [], "current")


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
    # Decision fields alone never move the digest; license/dependency records do.
    before = li.scope_digest(inventory)
    inventory["python"][0]["status"] = "approved"
    inventory["python"][0]["approval_ref"] = "x"
    inventory["python"][0]["note"] = "n"
    assert li.scope_digest(inventory) == before
    inventory["python"][0]["license_spdx"] = "ISC"
    assert li.scope_digest(inventory) != before


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


@pytest.mark.parametrize(("old", "new", "record"), [
    ("sha256:efb96a94", "sha256:b0e04fe6", "image:qdrant"),
    # #1074: changing the recorded Open WebUI digest must not rebind the approved entry.
    (OPENWEBUI_DIGEST, "d" * 64, "image:openwebui"),
], ids=["qdrant", "openwebui"])
def test_changed_image_digest_requires_re_review(tree, capsys, old, new, record):
    images = tree / "images.txt"
    assert old in images.read_text()
    images.write_text(images.read_text().replace(old, new))
    code, out = run(tree, capsys)
    assert code == 1 and record in out and "re-review the image" in out


def test_unrecorded_vendored_asset_and_second_chart_are_flagged(tree, capsys):
    vendor = tree / "src/mainframe_rag/webui/static/vendor"
    (vendor / "extra-lib.js").write_text("// new vendored library\n")
    shutil.copy(next((tree / "charts").glob("qdrant-*.tgz")), tree / "charts" / "qdrant-9.9.9.tgz")
    code, out = run(tree, capsys)
    assert code == 1
    assert "vendored asset extra-lib.js: no license record" in out
    assert "chart qdrant-9.9.9.tgz: vendored chart archive has no license record" in out


@pytest.mark.parametrize("relative", ["NOTICE.qdrant-skills", "LICENSE"])
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
    # An open decision (here a withdrawn pymupdf disposition) must stay visible in the notices.
    next(e for e in inventory["python"] if e["name"] == "pymupdf")["status"] = "pending-owner-decision"
    text = li.render_notices(REPO, inventory, tmp_path)
    assert "Apache License" in text and "Zero-Clause BSD" in text
    assert "### skills:mattpocock: LICENSE.mattpocock-skills" in text
    assert "Copyright (c) 2026 Matt Pocock" in text
    assert "### skills:pstack: LICENSE.pstack-skills" in text
    assert "Copyright (c) 2026 Lauren Tan" in text
    assert "### tool:task: task-LICENSE" in text and "Synthetic MIT task license" in text
    assert "### chart:qdrant-1.19.2: charts/qdrant-1.19.2.tgz:qdrant/LICENSE" in text
    assert "pymupdf==1.28.2" in text.split("COMPONENTS")[0]
    assert "Owner approval of this inventory: absent" in text
    (tmp_path / "task-LICENSE").write_bytes(b"tampered")
    with pytest.raises(li.InventoryError):
        li.render_notices(REPO, inventory, tmp_path)
    with pytest.raises(li.InventoryError):
        li.render_notices(REPO, inventory, None)




# --- dev kit scope (#1118, ADR-0009 §8) -------------------------------------

GPL_DECISION_REF = "gh:yonatan895/qdrant-pdf-rag#1118 issuecomment-6480982316"


def kit_problems(inventory, root=REPO):
    return li.run_kit_check(root, inventory, None)


def test_repository_kit_record_reconciles_and_only_owner_decisions_block_the_pack():
    """Every kit component has a record bound to its real pin; what still refuses the
    pack is only the owner's: open decisions and the absent kit approval."""
    inventory = real_inventory()
    problems, unresolved, state = kit_problems(inventory)
    assert state == "absent"
    blockers = {f"devkit: {item} awaits owner decision" for item in unresolved}
    assert set(problems) == blockers | {
        ("devkit approval: no owner approval is recorded for the kit inventory; record devkit.approval with "
         "the kit scope digest (docs/licensing.md#dev-kit)")}
    # The release scope and its digest do not include the kit section.
    without = {k: v for k, v in inventory.items() if k != "devkit"}
    assert li.scope_digest(without) == li.scope_digest(inventory)
    assert problems_for(inventory)[0] == []


def test_only_the_maintainers_gpl_decision_is_recorded_as_a_kit_approval():
    """GPL RPMs carry the 2026-10-28 decision; nothing else restricted is approved on the
    owner's behalf, and ambiguous RPM tags stay open with their terms named."""
    kit = real_inventory()["devkit"]
    approved = {e["id"]: e for e in kit["components"] if e["status"] == "approved"}
    assert set(approved) == {"rpm:crun", "rpm:git-core", "rpm:tar"}
    for entry in approved.values():
        assert entry["class"] == "strong-copyleft" and entry["approval_ref"].startswith(GPL_DECISION_REF)
        assert entry["decision"]["owner"] == "yonatan895" and entry["decision"]["date"] == "2026-10-28"
    assert not [e for e in kit["python"] if e["status"] == "approved"]
    for entry in kit["components"]:
        if entry["status"] == "pending-owner-decision":
            assert entry["class"] in li.RESTRICTED - {"strong-copyleft"}
            assert "NOASSERTION" in entry["license_spdx"] and entry["ambiguous_terms"]
        if entry.get("kind") == "rpm":
            # The declared tag is the RPM header, kept verbatim next to its normalized form.
            assert li.normalize_rpm_tag(entry["license_declared"])[0] == entry["license_spdx"]


@pytest.mark.parametrize("tag, expected", [
    ("GPLv2", ("GPL-2.0-only", "normalized-rpm-license-tag", [])),
    ("ASL 2.0", ("Apache-2.0", "normalized-rpm-license-tag", [])),
    ("GPL-2.0-only", ("GPL-2.0-only", "rpm-license-tag", [])),
    ("Apache-2.0 AND MIT AND MPL-2.0", ("Apache-2.0 AND MIT AND MPL-2.0", "rpm-license-tag", [])),
    # Legacy abbreviations Fedora maps to several identifiers are never guessed.
    ("MIT", ("NOASSERTION", "normalized-rpm-license-tag", ["MIT"])),
    ("BSD and GPLv2+", ("NOASSERTION AND GPL-2.0-or-later", "normalized-rpm-license-tag", ["BSD"])),
    ("GPLv3+ or BSD", ("GPL-3.0-or-later OR NOASSERTION", "normalized-rpm-license-tag", ["BSD"])),
    ("LGPLv2+", ("NOASSERTION", "normalized-rpm-license-tag", ["LGPLv2+"])),
])
def test_rpm_license_tags_normalize_only_unambiguous_terms(tag, expected):
    assert li.normalize_rpm_tag(tag) == expected


def test_kit_generate_derives_rpm_records_from_header_tags_and_never_approves():
    inventory = real_inventory()
    tags = {e["bind"]["nevra"]: e["license_declared"] for e in inventory["devkit"]["components"] if e["kind"] == "rpm"}
    # Stand-in dists carrying the recorded dev-only identities, so only the RPM path changes.
    found = {}
    for entry in inventory["devkit"]["python"]:
        found[(entry["name"], entry["version"])] = [li.Dist(entry["name"], entry["version"], "wheel", entry["wheel"],
                                                            entry["sha256"], _metadata(entry), {})]
    nevra = next(n for n in tags if n.startswith("git-core-"))
    tags[nevra] = "GPLv2 and Sleepycat"
    merged, warnings = li.generate_kit(REPO, inventory, found, tags)
    git = next(e for e in merged["devkit"]["components"] if e["id"] == "rpm:git-core")
    assert git["status"] == "pending-owner-decision" and git["approval_ref"] is None and "decision" not in git
    assert warnings[-1] == "rpm git-core: License tag changed; decision fields dropped, owner re-review needed"
    tar = next(e for e in merged["devkit"]["components"] if e["id"] == "rpm:tar")
    assert tar["status"] == "approved" and tar["approval_ref"].startswith(GPL_DECISION_REF)
    with pytest.raises(li.InventoryError):
        li.generate_kit(REPO, inventory, found, {k: v for k, v in tags.items() if k != nevra})


def _metadata(entry):
    field = {"License-Expression": "License-Expression", "License": "License"}.get(entry["declared_source"], "License")
    return f"Metadata-Version: 2.4\nName: {entry['name']}\nVersion: {entry['version']}\n{field}: {entry['license_declared']}\n".encode()


def test_kit_notices_refuse_an_oras_licence_off_its_pinned_digest(tmp_path):
    inventory = real_inventory()
    (tmp_path / "oras-LICENSE").write_bytes(b"not the pinned licence\n")
    with pytest.raises(li.InventoryError):
        li.render_kit_notices(REPO, inventory, tmp_path)

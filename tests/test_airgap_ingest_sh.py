"""scripts/airgap/ingest.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dry-run rendering of the prod ingest Job manifest,
NFS storage refusal, INGEST_WORKERS, contextual embed placeholders,
PULL_SECRET wiring, and strategic merge patches without a cluster.
"""

import re
import shutil

import pytest

from tests.helpers_airgap import (
    REPO,
    STUB_TOOL,
    assert_no_placeholders,
    assert_pull_secret_wired,
    make_bin_tree,
    run_sh,
    write_stub,
)

IMAGE_SHA = "d" * 40

STUB_BIN = """#!/bin/sh
if [ "$1" = "kustomize" ] || [ "$1" = "build" ]; then
  cat {stub_yaml}
  exit 0
fi
if [ "$1" = "patch" ]; then
  # Local strategic merge patch stub
  cat {stub_patched_yaml}
  exit 0
fi
printf '%s\\n' "$@" >> "$KC_LOG"
exit 0
"""

STUB_INGEST_KUSTOMIZE = """apiVersion: batch/v1
kind: Job
metadata:
  name: ingest
  namespace: mainframe-rag
spec:
  template:
    spec:
      imagePullSecrets: []
      containers:
        - name: ingest
          image: __INTERNAL_REGISTRY__/qdrant-pdf-rag-ingest:__IMAGE_SHA__
          args: __INGEST_ARGS__
          env:
            - name: QDRANT_URL
              value: __QDRANT_URL__
            - name: QDRANT_COLLECTION
              value: mainframe_manuals
            - name: INGEST_ALIAS_PUBLISH
              value: "__INGEST_ALIAS_PUBLISH__"
            - name: QDRANT_API_KEY
              valueFrom:
                secretKeyRef:
                  name: __QDRANT_RELEASE__-apikey
                  key: api-key
            - name: EMBED_BASE_URL
              value: __EMBED_BASE_URL__
            - name: EMBED_MODEL
              value: __EMBED_MODEL__
            - name: EMBED_MODEL_REVISION
              value: __EMBED_MODEL_REVISION__
            - name: DENSE_DIM
              value: __DENSE_DIM__
            - name: QDRANT_SHARD_NUMBER
              value: __QDRANT_SHARD_NUMBER__
            - name: QDRANT_REPLICATION_FACTOR
              value: __QDRANT_REPLICATION_FACTOR__
            - name: QDRANT_WRITE_CONSISTENCY_FACTOR
              value: __QDRANT_WRITE_CONSISTENCY_FACTOR__
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: __OTEL_EXPORTER_OTLP_ENDPOINT__
            - name: IMAGE_SHA
              value: __IMAGE_SHA__
            - name: OTEL_SERVICE_NAME
              value: mainframe-rag-ingest
            - name: OTEL_DEPLOYMENT_ENVIRONMENT
              value: __OTEL_DEPLOYMENT_ENVIRONMENT__
            - name: INGEST_WORKERS
              value: "__INGEST_WORKERS__"
            - name: CONTEXTUAL_EMBED_ENABLED
              value: "__CONTEXTUAL_EMBED_ENABLED__"
            - name: CONTEXT_LLM_BASE_URL
              value: "__CONTEXT_LLM_BASE_URL__"
            - name: CONTEXT_LLM_MODEL
              value: "__CONTEXT_LLM_MODEL__"
            - name: EMBED_API_KEY
              valueFrom:
                secretKeyRef:
                  key: embed-api-key
                  name: __GATEWAY_API_KEY_SECRET__
            - name: CONTEXT_LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  key: context-llm-api-key
                  name: __GATEWAY_API_KEY_SECRET__
      volumes:
        - name: corpus
          persistentVolumeClaim:
            claimName: __CORPUS_PVC__
"""


@pytest.fixture
def ingest_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "ingest.sh", "map_values.py"])
    (tmp_path / "deploy" / "kustomize" / "overlays" / "openshift-ingest").mkdir(
        parents=True, exist_ok=True
    )
    stub_yaml = tmp_path / "stub-ingest.yaml"
    stub_yaml.write_text(STUB_INGEST_KUSTOMIZE)
    stub_patched_yaml = tmp_path / "stub-patched-ingest.yaml"
    stub_patched_yaml.write_text(STUB_INGEST_KUSTOMIZE.replace("cpu: 4", "cpu: 1"))
    kc_log = tmp_path / "kc.log"
    for name in ("kubectl", "oc", "kustomize"):
        write_stub(
            tmp_path / "bin" / name,
            STUB_BIN.format(stub_yaml=stub_yaml, stub_patched_yaml=stub_patched_yaml),
        )
    # Issue #448 H2a: ingest.sh dry-run also maps release values and renders
    # the chart Job (real mapper + real python3); helm stays stubbed here —
    # real helm rendering is proven by test_helm_chart_shadow,
    # test_map_values and the e2e dry-run gate.
    write_stub(tmp_path / "bin" / "helm", STUB_TOOL)
    return tmp_path, kc_log


def _run_ingest(tree, *extra_env, policy: tuple[str, str, str] | None = ("1", "1", "1")):
    """Run ingest.sh hermetically.

    `policy` defaults to the explicit single-node 1/1/1 selection; pass None
    to leave the three keys to the tree's checked-in production preset.
    """
    tmp_path, kc_log = tree
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "KC_LOG": str(kc_log),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "test-ns",
        "STORAGE_CLASS": "gp3-csi",
        "CORPUS_PVC": "my-manuals-pvc",
        "EMBED_MODEL": "test-embed",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "VLLM_BASE_URL": "http://vllm:8000/v1",
        "AIRGAP_DRYRUN": "1",
    }
    if policy is not None:
        (
            env["QDRANT_SHARD_NUMBER"],
            env["QDRANT_REPLICATION_FACTOR"],
            env["QDRANT_WRITE_CONSISTENCY_FACTOR"],
        ) = policy
    for k, v in extra_env:
        env[k] = v
    return run_sh(tmp_path / "scripts" / "airgap" / "ingest.sh", env, tmp_path)


def _copy_collection_preset(tree):
    """Place the checked-in production preset in the copied tree."""
    tmp_path, _ = tree
    target = tmp_path / "overlays" / "openshift"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "overlays" / "openshift" / "collection-policy.env", target)


def test_ingest_dryrun_renders_clean_manifest(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert_no_placeholders(rendered)
    assert "reg.internal:5000/qdrant-pdf-rag-ingest:" in rendered
    assert "claimName: my-manuals-pvc" in rendered
    assert 'value: "4"' in rendered or "value: 4" in rendered
    assert "value: http://vllm:8000/v1" in rendered
    # Issue #391 F1: the representation preflight refuses a blank revision,
    # so the operator-declared value must reach the Job environment.
    assert re.search(r"(?m)^\s*- name: EMBED_MODEL_REVISION$", rendered)
    assert re.search(r"(?m)^\s*value: rev-1$", rendered)


def test_ingest_missing_embed_revision_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("EMBED_MODEL_REVISION", ""))
    assert r.returncode != 0
    assert "required variables unset" in r.stderr and "EMBED_MODEL_REVISION" in r.stderr


def test_ingest_whitespace_embed_revision_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("EMBED_MODEL_REVISION", "  "))
    assert r.returncode != 0
    assert "EMBED_MODEL_REVISION must be a non-blank" in r.stderr


def test_ingest_identity_version_always_rendered(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    # service.version is the packed SHA: always set, ingest.sh fail-closes
    # on empty/HEAD before rendering (issue #315).
    assert re.search(r"IMAGE_SHA\n\s+value: " + IMAGE_SHA, rendered, re.MULTILINE)


# ------------------------------------------------------- Qdrant least privilege (#366)

def _ingest_qdrant_block(rendered):
    lines = rendered.splitlines()
    start = next(i for i, l in enumerate(lines) if "- name: QDRANT_API_KEY" in l)
    return "\n".join(lines[start : start + 5])


def test_ingest_qdrant_key_keeps_write_access(ingest_tree):
    """Issue #366 mirror: ingestion owns corpus mutation, so the rendered
    Job keeps the full-access key while the agent goes read-only."""
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    block = _ingest_qdrant_block(
        (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    )
    assert re.search(r"(?m)^\s*key: api-key$", block)


def test_ingest_qdrant_readonly_key_fails_closed(ingest_tree):
    """A render downgrading ingest to the read-only key must stop the run."""
    tmp_path, _ = ingest_tree
    stub = (tmp_path / "stub-ingest.yaml").read_text()
    lines = [
        "key: read-only-api-key" if l.strip() == "key: api-key" else l
        for l in stub.splitlines()
    ]
    (tmp_path / "stub-ingest.yaml").write_text("\n".join(lines) + "\n")
    r = _run_ingest(ingest_tree)
    assert r.returncode != 0
    assert "key api-key" in r.stderr


def test_ingest_qdrant_copresent_readonly_key_fails_closed(ingest_tree):
    """Anti-revert parity with the agent check: a read-only key smuggled
    into the ingest render must stop the run even with api-key present."""
    tmp_path, _ = ingest_tree
    stub = (tmp_path / "stub-ingest.yaml").read_text()
    anchor = "                  key: api-key\n"
    assert anchor in stub
    stub = stub.replace(
        anchor,
        anchor
        + "            - name: QDRANT_READ_API_KEY\n"
        + "              valueFrom:\n"
        + "                secretKeyRef:\n"
        + "                  key: read-only-api-key\n"
        + "                  name: __QDRANT_RELEASE__-apikey\n",
    )
    (tmp_path / "stub-ingest.yaml").write_text(stub)
    r = _run_ingest(ingest_tree)
    assert r.returncode != 0
    assert "read-only" in r.stderr


def test_ingest_overlay_qdrant_contract():
    """Pin the real ingest overlay to the write-key contract so it cannot
    silently follow the agent to read-only."""
    real = (
        REPO / "deploy" / "kustomize" / "overlays" / "openshift-ingest" / "ingest-job.yaml"
    ).read_text()
    assert "- name: QDRANT_API_KEY" in real
    assert re.search(r"(?m)^\s*key: api-key$", real)
    assert "read-only-api-key" not in real
    assert "__QDRANT_RELEASE__-apikey" in real


def test_ingest_overlay_maintenance_contract():
    """Issue #391 current packet: maintenance modes are explicit, validated
    launcher inputs rendered into the prod Job, and the progress path stays
    the single shared publisher path (no implicit alias/default flip)."""
    real = (
        REPO / "deploy" / "kustomize" / "overlays" / "openshift-ingest" / "ingest-job.yaml"
    ).read_text()
    assert re.search(r"(?m)^\s*args: __INGEST_ARGS__$", real)
    assert re.search(r"(?m)^\s*- name: INGEST_ALIAS_PUBLISH$", real)
    assert re.search(r'(?m)^\s*value: "__INGEST_ALIAS_PUBLISH__"$', real)
    assert re.search(r'(?m)^\s*value: "true"$', real) is None
    # The launcher owns the one shared progress path (writer constraint).
    launcher = (REPO / "scripts" / "airgap" / "ingest.sh").read_text()
    assert "--progress\", \"/work/inventory.jsonl" in launcher


def test_ingest_default_render_adds_no_maintenance_args(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert (
        'args: ["--src", "/corpus", "--progress", "/work/inventory.jsonl"]' in rendered
    )
    assert '"--reingest"' not in rendered
    assert '"--retire-doc"' not in rendered
    # Review F1: boolean env values render as quoted strings (K8s EnvVar.value
    # is a string field), matching the sibling integer quoting contract.
    assert re.search(r'(?m)^\s*value: "false"$', rendered)
    assert_no_placeholders(rendered)


def test_ingest_force_repair_args_rendered(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_REINGEST", "true"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert '"--reingest"' in rendered
    assert_no_placeholders(rendered)


def test_ingest_retire_docs_require_alias_publish(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_RETIRE_DOCS", "SA22-0000-00"))
    assert r.returncode != 0
    assert "INGEST_ALIAS_PUBLISH=true" in r.stderr


def test_ingest_retire_docs_rendered_with_alias_publish(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", "SA22-0000-00, SA22-7777-01@rev-1"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert '"--retire-doc", "SA22-0000-00"' in rendered
    assert '"--retire-doc", "SA22-7777-01@rev-1"' in rendered
    assert re.search(r'(?m)^\s*value: "true"$', rendered)
    assert_no_placeholders(rendered)


def test_ingest_retire_docs_accepts_source_revision_alphabet(ingest_tree):
    """Review F2: a real source_rev (`vendor|product|version|sha256`, labels
    may carry '/', '|' and spaces) reaches the Job args verbatim; the old
    narrow charset made revision-scoped retirement unreachable and the sed
    delimiter collided with the revision pipes."""
    rev = "ibm|z/os|3.1|" + "a" * 64
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", f"SA23-1380-09@{rev}"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert f'"--retire-doc", "SA23-1380-09@{rev}"' in rendered
    assert_no_placeholders(rendered)


def test_ingest_retire_docs_preserves_interior_spaces_in_product_label(ingest_tree):
    """Issue #391 Q419-I1: operator inputs with interior spaces (e.g. from
    normalize_label on product names) must not be fragmented by word splitting.
    Leading and trailing whitespace per entry is stripped, while interior spaces
    are preserved lossless into Job args and validate cleanly against backend
    retirement planning."""
    import yaml

    from mainframe_rag.ingest.identity import source_rev_key
    from mainframe_rag.ingest.inventory import InventoryRecord
    from mainframe_rag.ingest.publish import plan_approved_removals

    rev = source_rev_key("IBM", "z/OS communications server", "3.1", "a" * 64)
    assert "z/os communications server" in rev

    # Test padded entries separated by comma
    retire_input = f"  SA23-1380-09@{rev}  ,  SA22-0000-00  "
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", retire_input),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert_no_placeholders(rendered)

    parsed = yaml.safe_load(rendered)
    container = parsed["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]

    # Extract all --retire-doc values from container args
    retire_args = [
        args[i + 1]
        for i, arg in enumerate(args)
        if arg == "--retire-doc" and i + 1 < len(args)
    ]
    assert retire_args == [f"SA23-1380-09@{rev}", "SA22-0000-00"]

    # Verify backend retirement planner accepts the exact parsed args against inventory
    inv = {
        "/corpus/doc1.pdf": InventoryRecord(
            path="/corpus/doc1.pdf",
            sha256="a" * 64,
            rules_version="r" * 16,
            status="upserted",
            chunks=1,
            doc_id="SA23-1380-09",
            source_rev=rev,
        ),
        "/corpus/doc2.pdf": InventoryRecord(
            path="/corpus/doc2.pdf",
            sha256="b" * 64,
            rules_version="r" * 16,
            status="upserted",
            chunks=1,
            doc_id="SA22-0000-00",
            source_rev="rev2",
        ),
    }
    plan, retired = plan_approved_removals(tuple(retire_args), inv)
    assert retired == frozenset({"SA23-1380-09", "SA22-0000-00"})
    assert plan["SA23-1380-09"]["revs"] == {rev}
    assert plan["SA22-0000-00"]["whole"] is True


def test_ingest_retire_docs_newline_separated(ingest_tree):
    """Q419-I1: INGEST_RETIRE_DOCS supports newline-separated entries."""
    import yaml

    retire_input = "SA23-1380-09@rev-1\nSA22-0000-00"
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", retire_input),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    parsed = yaml.safe_load(rendered)
    container = parsed["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    retire_args = [
        args[i + 1]
        for i, arg in enumerate(args)
        if arg == "--retire-doc" and i + 1 < len(args)
    ]
    assert retire_args == ["SA23-1380-09@rev-1", "SA22-0000-00"]


def test_ingest_retire_docs_wildcard_fails_closed(ingest_tree):
    """Review F3: operator input is never pathname-expanded (noglob) and
    wildcards fail closed instead of rendering local filenames."""
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", "*"),
    )
    assert r.returncode != 0
    assert "malformed INGEST_RETIRE_DOCS" in r.stderr
    assert "wildcards" in r.stderr


def test_ingest_retire_docs_empty_side_fails_closed(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", "SA22-0000-00@"),
    )
    assert r.returncode != 0
    assert "empty side of '@'" in r.stderr


def test_ingest_malformed_retire_docs_fails_closed(ingest_tree):
    """Quotes/backslashes would break the rendered double-quoted YAML scalar
    and are refused; other punctuation (/, |, spaces, ';') is data, never a
    shell evaluation, and reaches the backend verbatim."""
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", 'SA22-0000-00"bad'),
    )
    assert r.returncode != 0
    assert "malformed INGEST_RETIRE_DOCS" in r.stderr


def test_ingest_invalid_maintenance_bool_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_REINGEST", "maybe"))
    assert r.returncode != 0
    assert "must be true/false" in r.stderr


def test_ingest_dryrun_custom_workers(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_WORKERS", "8"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert 'value: "8"' in rendered or "value: 8" in rendered


def test_ingest_dryrun_contextual_embed_propagation(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("CONTEXTUAL_EMBED_ENABLED", "true"),
        ("CONTEXT_LLM_BASE_URL", "http://context-llm:8000/v1"),
        ("CONTEXT_LLM_MODEL", "meta-llama/Llama-3-8B"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert 'value: "true"' in rendered or "value: true" in rendered
    assert 'value: "http://context-llm:8000/v1"' in rendered or "value: http://context-llm:8000/v1" in rendered
    assert 'value: "meta-llama/Llama-3-8B"' in rendered or "value: meta-llama/Llama-3-8B" in rendered


def test_ingest_overlay_collection_policy_contract():
    """Issue #360: the real ingest overlay carries the three optional
    distribution-policy entries as bare placeholders for the launcher."""
    real = (
        REPO / "deploy" / "kustomize" / "overlays" / "openshift-ingest" / "ingest-job.yaml"
    ).read_text()
    for key in ("QDRANT_SHARD_NUMBER", "QDRANT_REPLICATION_FACTOR",
                "QDRANT_WRITE_CONSISTENCY_FACTOR"):
        assert re.search(rf"(?m)^\s*- name: {key}$", real), key
        assert f"value: __{key}__" in real, key


def test_ingest_collection_policy_absent_fails_closed(ingest_tree):
    """Issue #360: the production path requires a complete policy tuple.
    A tree without the checked-in preset and without explicit selection
    must refuse before rendering — no silent 1/1/1 downgrade."""
    r = _run_ingest(ingest_tree, policy=None)
    assert r.returncode == 1
    assert "collection distribution policy is incomplete" in r.stderr
    assert "1/1/1" in r.stderr


def test_ingest_collection_policy_partial_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("QDRANT_REPLICATION_FACTOR", ""))
    assert r.returncode == 1
    assert "QDRANT_REPLICATION_FACTOR" in r.stderr


def test_ingest_collection_policy_preset_supplies_production_tuple(ingest_tree):
    """The checked-in preset is the lowest-precedence default: an unset
    operator selection renders the production 6/3/2 tuple."""
    _copy_collection_preset(ingest_tree)
    r = _run_ingest(ingest_tree, policy=None)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: 6$", rendered)
    assert re.search(r"(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: 3$", rendered)
    assert re.search(
        r"(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: 2$", rendered
    )
    assert_no_placeholders(rendered)


def test_ingest_collection_policy_file_beats_preset(ingest_tree):
    _copy_collection_preset(ingest_tree)
    env_file = ingest_tree[0] / "policy.env"
    env_file.write_text(
        "QDRANT_SHARD_NUMBER=2\n"
        "QDRANT_REPLICATION_FACTOR=2\n"
        "QDRANT_WRITE_CONSISTENCY_FACTOR=1\n"
    )
    r = _run_ingest(
        ingest_tree, ("AIRGAP_ENV", str(env_file)), policy=None
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: 2$", rendered)
    assert re.search(r"(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: 2$", rendered)
    assert re.search(
        r"(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: 1$", rendered
    )


def test_ingest_collection_policy_caller_beats_file_and_preset(ingest_tree):
    _copy_collection_preset(ingest_tree)
    env_file = ingest_tree[0] / "policy.env"
    env_file.write_text(
        "QDRANT_SHARD_NUMBER=2\n"
        "QDRANT_REPLICATION_FACTOR=2\n"
        "QDRANT_WRITE_CONSISTENCY_FACTOR=1\n"
    )
    r = _run_ingest(
        ingest_tree,
        ("AIRGAP_ENV", str(env_file)),
        policy=("6", "3", "2"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: 6$", rendered)
    assert re.search(r"(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: 3$", rendered)
    assert re.search(
        r"(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: 2$", rendered
    )


def test_ingest_collection_policy_set_renders_bare_ints(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        policy=("6", "2", "1"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: 6$", rendered)
    assert re.search(r"(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: 2$", rendered)
    assert re.search(
        r"(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: 1$", rendered
    )
    assert_no_placeholders(rendered)


def test_ingest_collection_policy_invalid_fails_closed(ingest_tree):
    for bad in ("two", "0", "-1", "1.5", "2x"):
        r = _run_ingest(ingest_tree, ("QDRANT_REPLICATION_FACTOR", bad))
        assert r.returncode != 0, bad
        assert "QDRANT_REPLICATION_FACTOR" in r.stderr, bad


def test_ingest_collection_policy_write_above_replication_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, policy=("6", "2", "3"))
    assert r.returncode == 1
    assert "exceeds" in r.stderr and "QDRANT_WRITE_CONSISTENCY_FACTOR" in r.stderr


def test_production_preset_is_the_owner_decision():
    """The checked-in preset is the owner decision (6 shards / RF 3 / W 2),
    not an inferred default. Local lanes override it explicitly."""
    from tests.helpers_airgap import REPO as repo

    text = (repo / "overlays" / "openshift" / "collection-policy.env").read_text()
    for line in (
        "QDRANT_SHARD_NUMBER=6",
        "QDRANT_REPLICATION_FACTOR=3",
        "QDRANT_WRITE_CONSISTENCY_FACTOR=2",
    ):
        assert line in text.splitlines()


def test_ingest_otel_on_by_default(ingest_tree):
    # Unset endpoint resolves to the in-cluster Jaeger (tracing ON); the
    # service name is fixed so the Job never merges into the agent service.
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "value: http://jaeger:4318" in rendered
    assert "value: mainframe-rag-ingest" in rendered


def test_ingest_otel_off_sentinel(ingest_tree):
    r = _run_ingest(ingest_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r"OTEL_EXPORTER_OTLP_ENDPOINT\n\s+value:\s*$", rendered, re.MULTILINE)


def test_ingest_otel_endpoint_and_environment_wired(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"),
        ("OTEL_DEPLOYMENT_ENVIRONMENT", "prod"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "value: http://jaeger:4318" in rendered
    assert "value: prod" in rendered
    assert_no_placeholders(rendered)


def test_ingest_pull_secret_wired_when_set(ingest_tree):
    r = _run_ingest(ingest_tree, ("PULL_SECRET", "custom-registry-secret"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "name: custom-registry-secret" in rendered
    # The wired item must stay inside the pod-spec mapping: a fixed 2-space
    # insert broke out of it and kubectl rejected the manifest
    # ("did not find expected key" in the Kind rehearsal).
    assert_pull_secret_wired(rendered, "custom-registry-secret")


def test_ingest_pull_secret_bad_name_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("PULL_SECRET", "Bad_Name!"))
    assert r.returncode != 0
    assert "PULL_SECRET must be a DNS-subdomain name" in r.stderr


def test_ingest_pull_secret_stays_empty_when_unset(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "imagePullSecrets: []" in rendered
    assert "name: custom-registry-secret" not in rendered


def test_ingest_refuses_nfs_storage(ingest_tree):
    r = _run_ingest(ingest_tree, ("STORAGE_CLASS", "nfs-storage-class"))
    assert r.returncode == 1
    assert "looks like NFS" in r.stderr


def test_ingest_missing_corpus_pvc_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("CORPUS_PVC", ""))
    assert r.returncode == 1
    assert "required variables unset: CORPUS_PVC" in r.stderr


def test_ingest_cli_corpus_pvc_beats_env_file(ingest_tree):
    env_file = ingest_tree[0] / "case.env"
    env_file.write_text("CORPUS_PVC=file-pvc-should-lose\n")
    r = _run_ingest(ingest_tree, ("AIRGAP_ENV", str(env_file)))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "claimName: my-manuals-pvc" in rendered
    assert "file-pvc-should-lose" not in rendered


def test_ingest_gateway_keys_off_strips_secret_block(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    # No gateway secret reference: the only secretKeyRef left is the
    # chart-managed QDRANT_API_KEY. The neighboring plain entries and the
    # volumes section survive the strip.
    assert rendered.count("secretKeyRef") == 1
    assert "EMBED_API_KEY" not in rendered
    assert "CONTEXT_LLM_API_KEY" not in rendered
    assert "RERANK_API_KEY" not in rendered
    assert "CONTEXT_LLM_MODEL" in rendered
    assert "volumes:" in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys off" in r.stdout


def test_ingest_gateway_keys_wired_when_secret_set(ingest_tree):
    import re

    r = _run_ingest(ingest_tree, ("GATEWAY_API_KEY_SECRET", "gateway-api-keys"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    for env_name, data_key in (
        ("EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    ):
        assert re.search(
            rf"- name: {env_name}\n\s+valueFrom:\n\s+secretKeyRef:\n\s+key: {data_key}\n\s+name: gateway-api-keys",
            rendered,
        ), env_name
    # The ingest Job never touches the reasoning/rerank legs (anchored:
    # CONTEXT_LLM_API_KEY contains LLM_API_KEY as a substring).
    assert not re.search(r"^- name: LLM_API_KEY$", rendered, re.MULTILINE)
    assert "RERANK_API_KEY" not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys wired" in r.stdout


def test_ingest_gateway_secret_bad_name_fails_closed(ingest_tree):
    r = _run_ingest(ingest_tree, ("GATEWAY_API_KEY_SECRET", "Bad_Name!"))
    assert r.returncode == 1
    assert "GATEWAY_API_KEY_SECRET must be a DNS-subdomain name" in r.stderr


def test_ingest_gateway_overlay_block_matches_stub_contract():
    """The stub kustomize above mirrors the real ingest overlay by hand —
    pin the real file to the same contract so the two cannot diverge."""
    from tests.helpers_airgap import REPO

    real = (
        REPO
        / "deploy"
        / "kustomize"
        / "overlays"
        / "openshift-ingest"
        / "ingest-job.yaml"
    ).read_text()
    assert "# gateway-api-keys-begin" in real
    assert "# gateway-api-keys-end" in real
    assert real.index("# gateway-api-keys-begin") < real.index("# gateway-api-keys-end")
    # Deploy identity (issue #315): ingest spans carry service.version.
    assert "- name: IMAGE_SHA" in real
    assert "value: __IMAGE_SHA__" in real
    for env_name, data_key in (
        ("EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    ):
        assert f"- name: {env_name}" in real
        assert f"key: {data_key}" in real
    assert "__GATEWAY_API_KEY_SECRET__" in real

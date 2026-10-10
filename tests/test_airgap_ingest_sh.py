"""scripts/airgap/ingest.sh fail-close and operability tests (issue #15).

Hermetic tests: tests dry-run rendering of the prod ingest Job manifest,
NFS storage refusal, INGEST_WORKERS, contextual embed placeholders,
PULL_SECRET wiring, and strategic merge patches without a cluster.
"""

import re
import shutil
import subprocess

import pytest

from tests.helpers_airgap import (
    REPO,
    STUB_TOOL,
    assert_no_placeholders,
    assert_pull_secret_wired,
    install_rendering_helm,
    make_bin_tree,
    rendered_container,
    rendered_env,
    run_sh,
    write_git_identity_stub,
    write_signed_manifest,
    write_stub,
)

IMAGE_SHA = "d" * 40

STUB_BIN = """#!/bin/sh
printf '%s\\n' "$@" >> "$KC_LOG"
exit 0
"""

@pytest.fixture
def ingest_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "ingest.sh", "map_values.py"])
    for name in ("kubectl", "oc"):
        write_stub(
            tmp_path / "bin" / name,
            STUB_BIN,
        )
    # Real Helm renders the chart; cluster mutations remain stubbed.
    write_stub(tmp_path / "bin" / "helm", STUB_TOOL)
    install_rendering_helm(tmp_path)
    return tmp_path, tmp_path / "kc-args.log"


def _run_ingest(tree, *extra_env, policy: tuple[str, str, str] | None = ("1", "1", "1"), runner=run_sh):
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
    return runner(tmp_path / "scripts" / "airgap" / "ingest.sh", env, tmp_path)


def _copy_collection_preset(tree):
    """Place the checked-in production preset in the copied tree."""
    tmp_path, _ = tree
    target = tmp_path / "scripts" / "airgap"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "scripts" / "airgap" / "collection-policy.env", target)


def test_ingest_dryrun_renders_clean_manifest(ingest_tree):
    r = _run_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    # Identical default runs share a render; every field oracle stays explicit.
    assert_no_placeholders(rendered)
    assert "reg.internal:5000/qdrant-pdf-rag-ingest:" in rendered
    assert "claimName: my-manuals-pvc" in rendered
    assert 'value: "4"' in rendered or "value: 4" in rendered
    assert 'value: "http://vllm:8000/v1"' in rendered
    assert re.search(r"(?m)^\s*- name: EMBED_MODEL_REVISION$", rendered)
    assert rendered_env(rendered, "ingest")["EMBED_MODEL_REVISION"] == "rev-1"
    assert rendered_env(rendered, "ingest")["IMAGE_SHA"] == IMAGE_SHA
    block = _ingest_qdrant_block(
        (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    )
    assert re.search(r"(?m)^\s*key: api-key$", block)
    assert rendered_container(rendered, "ingest")["args"] == [
        "--src", "/corpus", "--progress", "/work/inventory.jsonl",
    ]
    assert rendered_env(rendered, "ingest")["INGEST_ALIAS_PUBLISH"] == "true"
    assert '"--reingest"' not in rendered
    assert '"--retire-doc"' not in rendered
    assert re.search(r'(?m)^\s*value: "false"$', rendered)
    assert 'value: "http://jaeger:4318"' in rendered
    assert "value: mainframe-rag-ingest" in rendered
    assert "imagePullSecrets: []" in rendered
    assert "name: custom-registry-secret" not in rendered
    assert rendered.count("secretKeyRef") == 1
    assert "EMBED_API_KEY" not in rendered
    assert "CONTEXT_LLM_API_KEY" not in rendered
    assert "RERANK_API_KEY" not in rendered
    assert "CONTEXT_LLM_MODEL" in rendered
    assert "volumes:" in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert "Gateway keys off" in r.stdout



@pytest.mark.parametrize("delete_fails", [False, True])
def test_ingest_replacement_waits_for_prior_writer(ingest_tree, delete_fails):
    """A deleted Job can still have a terminating pod holding the writer lock."""
    tree, _ = ingest_tree
    (tree / "old-writer").touch()
    write_stub(tree / "bin/kubectl", """#!/bin/sh
case "$*" in
  *"get pvc ingest-work"*) echo persistentvolumeclaim/ingest-work ;;
  *"delete job ingest"*)
    [ "$DELETE_FAILS" != true ] || exit 23
    case "$*" in
      *--cascade=foreground*--wait=true*) rm old-writer ;;
    esac
    ;;
  *"apply -f dist/ingest-rendered.yaml"*)
    if [ -e old-writer ]; then echo 'prior writer still holds lock' >&2; exit 24; fi
    touch replacement-applied
    ;;
  *"get pods"*) echo Succeeded ;;
  *"wait --for=condition=complete"*) test -e replacement-applied ;;
esac
""")
    result = _run_ingest(
        ingest_tree,
        ("AIRGAP_DRYRUN", "0"),
        ("DELETE_FAILS", str(delete_fails).lower()),
    )
    if delete_fails:
        assert result.returncode == 23, result.stderr
        assert (tree / "old-writer").exists()
        assert not (tree / "replacement-applied").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert not (tree / "old-writer").exists()
        assert (tree / "replacement-applied").exists()


@pytest.mark.parametrize("job_succeeds", [False, True])
def test_ingest_logs_follow_retry_and_stop_owned_stream(ingest_tree, job_succeeds):
    """A failed first pod must not hide the retry or leave its logs child alive."""
    import os
    from pathlib import Path

    tree, _ = ingest_tree
    write_stub(tree / "bin/kubectl", """#!/usr/bin/python3
import os,signal,sys,time
from pathlib import Path
a=sys.argv[1:]
if 'pvc' in a:
 print('persistentvolumeclaim/ingest-work')
elif 'pods' in a:
 if any('items[0].status.phase' in x for x in a):print('Failed')
 elif Path('first-followed').exists():print('ingest-first Failed\\ningest-second Running')
 else:print('ingest-first Failed')
elif 'logs' in a:
 if 'ingest-second' in a:
  def stopped(*_):
   Path('stream-stopped').touch()
   sys.exit(0)
  signal.signal(signal.SIGTERM,stopped)
  print('second pod progress',flush=True)
  Path('stream-pid').write_text(str(os.getpid()))
  Path('second-followed').touch()
  time.sleep(60)
 else:
  print('first pod failure',flush=True)
  Path('first-followed').touch()
  sys.exit(1)
elif 'wait' in a:
 deadline=time.monotonic()+8
 while not Path('second-followed').exists():
  if time.monotonic()>deadline:sys.exit(2)
  time.sleep(.02)
 sys.exit(0 if os.environ['JOB_SUCCEEDS']=='true' else 1)
""")
    result = _run_ingest(
        ingest_tree, ("AIRGAP_DRYRUN", "0"), ("JOB_SUCCEEDS", str(job_succeeds).lower())
    )
    assert "first pod failure" in result.stdout
    assert "second pod progress" in result.stdout, result.stdout + result.stderr
    assert result.returncode == (0 if job_succeeds else 1)
    assert (tree / "stream-stopped").exists(), "launcher did not terminate its stream"
    pid = int((tree / "stream-pid").read_text())
    assert not Path(f"/proc/{pid}").exists(), f"owned logs child {pid} survived launcher exit"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("cancel_signal", ["SIGTERM", "SIGINT"])
@pytest.mark.parametrize("cancel_stage", ["logs", "pods"])
def test_ingest_cancellation_reaps_local_observers(ingest_tree, cancel_signal, cancel_stage):
    import os
    import signal
    import subprocess
    import time
    from pathlib import Path

    tree, _ = ingest_tree
    write_stub(tree / "bin/kubectl", """#!/usr/bin/python3
import os,sys,time
from pathlib import Path
a=sys.argv[1:]
with open('operations', 'a') as f: f.write(' '.join(a)+'\\n')
if 'pvc' in a:
 print('persistentvolumeclaim/ingest-work')
elif 'wait' in a or os.environ['CANCEL_STAGE'] in a:
 name='wait' if 'wait' in a else 'observer'
 Path(name+'-pid').write_text(str(os.getpid()))
 time.sleep(60)
elif 'pods' in a:
 print('ingest-first Running')
""")

    def start(script, env, cwd):
        return subprocess.Popen(["sh", str(script)], env=env, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    # Repeat to prove no stale process/output handle blocks the next invocation.
    for _ in range(2):
        for name in ("wait-pid", "observer-pid"):
            (tree / name).unlink(missing_ok=True)
        proc = _run_ingest(ingest_tree, ("AIRGAP_DRYRUN", "0"),
                           ("CANCEL_STAGE", cancel_stage), runner=start)
        try:
            deadline = time.monotonic() + 10
            while not all((tree / name).exists() for name in ("wait-pid", "observer-pid")):
                if proc.poll() is not None:
                    pytest.fail(f"launcher exited before observers started: {proc.communicate()}")
                assert time.monotonic() < deadline, "observers did not start"
                time.sleep(.02)
            children = [int((tree / name).read_text()) for name in ("wait-pid", "observer-pid")]
            operations = (tree / "operations").read_text()
            proc.send_signal(getattr(signal, cancel_signal))
            _, stderr = proc.communicate(timeout=5)
            assert proc.returncode == (143 if cancel_signal == "SIGTERM" else 130)
            assert "job/ingest in namespace test-ns may still be running" in stderr
            assert (tree / "operations").read_text() == operations
            for pid in children:
                assert not Path(f"/proc/{pid}").exists(), f"owned observer {pid} survived"
        finally:
            # Bounded harness cleanup also contains failures against the old code.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=5)


@pytest.mark.parametrize("extra_env,exit_code,messages", [
    pytest.param({'EMBED_MODEL_REVISION': ''}, None, ('required variables unset', 'EMBED_MODEL_REVISION'),
                 id='ingest_missing_embed_revision_fails_closed'),
    pytest.param({'EMBED_MODEL_REVISION': '  '}, None, ('EMBED_MODEL_REVISION must be a non-blank',),
                 id='ingest_whitespace_embed_revision_fails_closed'),
    pytest.param({'INGEST_ALIAS_PUBLISH': 'false', 'INGEST_RETIRE_DOCS': 'SA22-0000-00'}, None, ('INGEST_ALIAS_PUBLISH=true',),
                 id='ingest_retire_docs_require_alias_publish'),
    pytest.param({'INGEST_ALIAS_PUBLISH': 'true', 'INGEST_RETIRE_DOCS': '*'}, None, ('malformed INGEST_RETIRE_DOCS', 'wildcards'),
                 id='ingest_retire_docs_wildcard_fails_closed'),
    pytest.param({'INGEST_ALIAS_PUBLISH': 'true', 'INGEST_RETIRE_DOCS': 'SA22-0000-00@'}, None, ("empty side of '@'",),
                 id='ingest_retire_docs_empty_side_fails_closed'),
    pytest.param({'INGEST_ALIAS_PUBLISH': 'true', 'INGEST_RETIRE_DOCS': 'SA22-0000-00"bad'}, None, ('malformed INGEST_RETIRE_DOCS',),
                 id='ingest_malformed_retire_docs_fails_closed'),
    pytest.param({'INGEST_REINGEST': 'maybe'}, None, ('must be true/false',),
                 id='ingest_invalid_maintenance_bool_fails_closed'),
    pytest.param({'QDRANT_REPLICATION_FACTOR': ''}, 1, ('QDRANT_REPLICATION_FACTOR',),
                 id='ingest_collection_policy_partial_fails_closed'),
    pytest.param({'PULL_SECRET': 'Bad_Name!'}, None, ('PULL_SECRET must be a DNS-subdomain name',),
                 id='ingest_pull_secret_bad_name_fails_closed'),
    pytest.param({'STORAGE_CLASS': 'nfs-storage-class'}, 1, ('looks like NFS',),
                 id='ingest_refuses_nfs_storage'),
    pytest.param({'CORPUS_PVC': ''}, 1, ('required variables unset: CORPUS_PVC',),
                 id='ingest_missing_corpus_pvc_fails_closed'),
    pytest.param({'GATEWAY_API_KEY_SECRET': 'Bad_Name!'}, 1, ('GATEWAY_API_KEY_SECRET must be a DNS-subdomain name',),
                 id='ingest_gateway_secret_bad_name_fails_closed'),
])
def test_invalid_configuration_fails_closed(ingest_tree, extra_env, exit_code, messages):
    result = _run_ingest(ingest_tree, *extra_env.items())
    assert result.returncode != 0 if exit_code is None else result.returncode == exit_code, result.stderr
    for message in messages:
        assert message in result.stderr



# ------------------------------------------------------- Qdrant least privilege (#366)

def _ingest_qdrant_block(rendered):
    lines = rendered.splitlines()
    start = next(i for i, l in enumerate(lines) if "- name: QDRANT_API_KEY" in l)
    return "\n".join(lines[start : start + 5])


def test_ingest_qdrant_readonly_key_fails_closed(ingest_tree):
    """A render downgrading ingest to the read-only key must stop the run."""
    tmp_path, _ = ingest_tree
    stub = (tmp_path / "charts/mainframe-rag/templates/ingest-job.yaml").read_text()
    lines = [
        l.replace("key: api-key", "key: read-only-api-key") if l.strip() == "key: api-key" else l
        for l in stub.splitlines()
    ]
    (tmp_path / "charts/mainframe-rag/templates/ingest-job.yaml").write_text("\n".join(lines) + "\n")
    r = _run_ingest(ingest_tree)
    assert r.returncode != 0
    assert "key api-key" in r.stderr


def test_ingest_qdrant_copresent_readonly_key_fails_closed(ingest_tree):
    """Anti-revert parity with the agent check: a read-only key smuggled
    into the ingest render must stop the run even with api-key present."""
    tmp_path, _ = ingest_tree
    stub = (tmp_path / "charts/mainframe-rag/templates/ingest-job.yaml").read_text()
    anchor = "                  key: api-key\n"
    assert anchor in stub
    stub = stub.replace(
        anchor,
        anchor
        + "            - name: QDRANT_READ_API_KEY\n"
        + "              valueFrom:\n"
        + "                secretKeyRef:\n"
        + "                  key: read-only-api-key\n"
        + "                  name: qdrant-apikey\n",
    )
    (tmp_path / "charts/mainframe-rag/templates/ingest-job.yaml").write_text(stub)
    r = _run_ingest(ingest_tree)
    assert r.returncode != 0
    assert "read-only" in r.stderr




def test_ingest_force_repair_args_rendered(ingest_tree):
    r = _run_ingest(ingest_tree, ("INGEST_REINGEST", "true"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "--reingest" in rendered_container(rendered, "ingest")["args"]
    assert_no_placeholders(rendered)


def test_ingest_retire_docs_rendered_with_alias_publish(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("INGEST_ALIAS_PUBLISH", "true"),
        ("INGEST_RETIRE_DOCS", "SA22-0000-00, SA22-7777-01@rev-1"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert rendered_container(rendered, "ingest")["args"][-4:-2] == ["--retire-doc", "SA22-0000-00"]
    assert rendered_container(rendered, "ingest")["args"][-2:] == ["--retire-doc", "SA22-7777-01@rev-1"]
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
    assert rendered_container(rendered, "ingest")["args"][-2:] == ["--retire-doc", f"SA23-1380-09@{rev}"]
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




def test_ingest_collection_policy_absent_fails_closed(ingest_tree):
    """Issue #360: the production path requires a complete policy tuple.
    A tree without the checked-in preset and without explicit selection
    must refuse before rendering — no silent 1/1/1 downgrade."""
    r = _run_ingest(ingest_tree, policy=None)
    assert r.returncode == 1
    assert "collection distribution policy is incomplete" in r.stderr
    assert "1/1/1" in r.stderr


def test_ingest_collection_policy_preset_supplies_production_tuple(ingest_tree):
    """The checked-in preset is the lowest-precedence default: an unset
    operator selection renders the production 6/3/2 tuple."""
    _copy_collection_preset(ingest_tree)
    r = _run_ingest(ingest_tree, policy=None)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r'(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: "6"$', rendered)
    assert re.search(r'(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: "3"$', rendered)
    assert re.search(
        r'(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: "2"$', rendered
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
    assert re.search(r'(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: "2"$', rendered)
    assert re.search(r'(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: "2"$', rendered)
    assert re.search(
        r'(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: "1"$', rendered
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
    assert re.search(r'(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: "6"$', rendered)
    assert re.search(r'(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: "3"$', rendered)
    assert re.search(
        r'(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: "2"$', rendered
    )


def test_ingest_collection_policy_set_renders_quoted_strings(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        policy=("6", "2", "1"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert re.search(r'(?m)^\s*- name: QDRANT_SHARD_NUMBER\n\s*value: "6"$', rendered)
    assert re.search(r'(?m)^\s*- name: QDRANT_REPLICATION_FACTOR\n\s*value: "2"$', rendered)
    assert re.search(
        r'(?m)^\s*- name: QDRANT_WRITE_CONSISTENCY_FACTOR\n\s*value: "1"$', rendered
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

    text = (repo / "scripts" / "airgap" / "collection-policy.env").read_text()
    for line in (
        "QDRANT_SHARD_NUMBER=6",
        "QDRANT_REPLICATION_FACTOR=3",
        "QDRANT_WRITE_CONSISTENCY_FACTOR=2",
    ):
        assert line in text.splitlines()


def test_ingest_otel_off_sentinel(ingest_tree):
    r = _run_ingest(ingest_tree, ("OTEL_EXPORTER_OTLP_ENDPOINT", "off"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert rendered_env(rendered, "ingest")["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""


def test_ingest_jaeger_false_requires_intentional_destination(ingest_tree):
    # Issue #568: the Job must not inherit the defaulted bundled hostname
    # when the bundled backend is disabled.
    r = _run_ingest(ingest_tree, ("JAEGER_ENABLED", "false"))
    assert r.returncode != 0
    assert "intentional trace destination" in r.stderr
    r = _run_ingest(ingest_tree, ("JAEGER_ENABLED", "false"),
                    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.platform:4318"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert rendered_env(rendered, "ingest")["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.platform:4318"


def test_ingest_otel_endpoint_and_environment_wired(ingest_tree):
    r = _run_ingest(
        ingest_tree,
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318"),
        ("OTEL_DEPLOYMENT_ENVIRONMENT", "prod"),
    )
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert 'value: "http://jaeger:4318"' in rendered
    assert 'value: "prod"' in rendered
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


def test_ingest_cli_corpus_pvc_beats_env_file(ingest_tree):
    env_file = ingest_tree[0] / "case.env"
    env_file.write_text("CORPUS_PVC=file-pvc-should-lose\n")
    r = _run_ingest(ingest_tree, ("AIRGAP_ENV", str(env_file)))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert "claimName: my-manuals-pvc" in rendered
    assert "file-pvc-should-lose" not in rendered


def test_ingest_gateway_keys_wired_when_secret_set(ingest_tree):
    import re

    r = _run_ingest(ingest_tree, ("GATEWAY_API_KEY_SECRET", "gateway-api-keys"))
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    for env_name, data_key in (
        ("EMBED_API_KEY", "embed-api-key"),
        ("CONTEXT_LLM_API_KEY", "context-llm-api-key"),
    ):
        assert rendered_env(rendered, "ingest")[env_name] == {
            "secretKeyRef": {"key": data_key, "name": "gateway-api-keys"}
        }
    # The ingest Job never touches the reasoning/rerank legs (anchored:
    # CONTEXT_LLM_API_KEY contains LLM_API_KEY as a substring).
    assert not re.search(r"^- name: LLM_API_KEY$", rendered, re.MULTILINE)
    assert "RERANK_API_KEY" not in rendered
    assert "__GATEWAY_API_KEY_SECRET__" not in rendered
    assert_no_placeholders(rendered)
    assert "Gateway keys wired" in r.stdout


@pytest.mark.parametrize("via_task", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_ingest_direct_peers_roundtrip_and_precedence(ingest_tree, override, via_task):
    file_peers = "http://peer-0:6333, http://peer-1:6333,\n\thttp://peer-2:6333"
    caller_peers = " http://override-0:6333 ,\thttp://override-1:6333,http://override-2:6333 "
    env_file = ingest_tree[0] / "peers.env"
    env_file.write_text("QDRANT_PEER_URLS='" + file_peers + "'\n")
    extra = [("AIRGAP_ENV", str(env_file)), ("INGEST_ALIAS_PUBLISH", "true")]
    if override:
        extra.append(("QDRANT_PEER_URLS", caller_peers))
    runner = run_sh
    if via_task:
        shutil.copy(REPO / "Taskfile.yml", ingest_tree[0])
        shutil.copytree(REPO / "taskfiles", ingest_tree[0] / "taskfiles")

        def runner(script, env, cwd):
            cli = ["QDRANT_PEER_URLS=" + env.pop("QDRANT_PEER_URLS")] if override else []
            return subprocess.run(
                [str(REPO / ".tools/bin/task"), "airgap:ingest", *cli],
                env=env, cwd=cwd, text=True, capture_output=True, check=False,
            )

    result = _run_ingest(ingest_tree, *extra, policy=("6", "3", "2"), runner=runner)
    assert result.returncode == 0, result.stderr
    rendered = (ingest_tree[0] / "dist/ingest-rendered.yaml").read_text()
    assert rendered_env(rendered, "ingest")["QDRANT_PEER_URLS"] == (
        caller_peers if override else file_peers
    )


def test_ingest_does_not_infer_direct_peers(ingest_tree):
    result = _run_ingest(ingest_tree, ("INGEST_ALIAS_PUBLISH", "true"), policy=("6", "3", "2"))
    assert result.returncode == 0, result.stderr
    rendered = (ingest_tree[0] / "dist/ingest-rendered.yaml").read_text()
    assert "QDRANT_PEER_URLS" not in rendered_env(rendered, "ingest")


@pytest.mark.parametrize("via_task", [False, True])
def test_shared_gateway_ingest_and_context_refs(ingest_tree, via_task):
    path = ingest_tree[0] / "gateway.env"
    path.write_text("GATEWAY_BASE_URL=https://file-gateway/v1\nGATEWAY_API_KEY_SECRET_KEY=file-key\n")
    runner = run_sh
    if via_task:
        shutil.copy(REPO / "Taskfile.yml", ingest_tree[0])
        shutil.copytree(REPO / "taskfiles", ingest_tree[0] / "taskfiles")

        def runner(script, env, cwd):
            cli = [f"{key}={env.pop(key)}" for key in ("GATEWAY_BASE_URL", "GATEWAY_API_KEY_SECRET_KEY")]
            env["GATEWAY_BASE_URL"] = "https://ambient-gateway/v1"
            return subprocess.run([str(REPO / ".tools/bin/task"), "airgap:ingest", *cli],
                                  env=env, cwd=cwd, text=True, capture_output=True, check=False)

    result = _run_ingest(ingest_tree, ("AIRGAP_ENV", str(path)), ("VLLM_BASE_URL", ""),
                         ("GATEWAY_BASE_URL", "https://sample-api/v1"),
                         ("GATEWAY_API_KEY_SECRET", "shared"), ("GATEWAY_API_KEY_SECRET_KEY", "api-key"),
                         ("EMBED_MODEL", "embedding-v1"), ("CONTEXTUAL_EMBED_ENABLED", "true"),
                         ("CONTEXT_LLM_MODEL", "fast"), runner=runner)
    assert result.returncode == 0, result.stderr
    rendered = (ingest_tree[0] / "dist/ingest-rendered.yaml").read_text()
    values = rendered_env(rendered, "ingest")
    assert values["EMBED_BASE_URL"] == "https://sample-api/v1"
    assert values["CONTEXT_LLM_BASE_URL"] == "https://sample-api/v1"
    assert values["EMBED_MODEL"] == "embedding-v1"
    assert values["CONTEXT_LLM_MODEL"] == "fast"
    entries = {entry["name"]: entry for entry in rendered_container(rendered, "ingest")["env"]}
    for key in ("EMBED_API_KEY", "CONTEXT_LLM_API_KEY"):
        assert entries[key]["valueFrom"]["secretKeyRef"] == {"name": "shared", "key": "api-key"}


# ------------------------------------------------- registry image identity (#272)

INGEST_REF = f"reg.internal:5000/qdrant-pdf-rag-ingest:{IMAGE_SHA}"
INGEST_CONFIG = "sha256:" + "c" * 64
INGEST_STUB_SKOPEO = r"""#!/bin/sh
printf 'skopeo %s\n' "$*" >> "$KC_LOG"
for a in "$@"; do case "$a" in docker://*) ref="${a#docker://}" ;; esac; done
f="$SKOPEO_REGISTRY/$(printf '%s' "$ref" | tr '/:@' '___').raw"
[ -f "$f" ] || { echo "manifest unknown" >&2; exit 1; }
cat "$f"
"""


def _release_ingest(tree, config=INGEST_CONFIG):
    """Packed MANIFEST + loaded registry; returns the registry manifest digest."""
    import hashlib
    import json

    tmp_path, _ = tree
    write_stub(tmp_path / "bin" / "skopeo", INGEST_STUB_SKOPEO)
    # A claimed release (#414): signed MANIFEST chain and a resolvable checkout.
    write_signed_manifest(tmp_path / "dist", f"sha: {IMAGE_SHA}\ningest_config_digest: {INGEST_CONFIG}\n")
    write_git_identity_stub(tmp_path, IMAGE_SHA)
    raw = json.dumps({"config": {"digest": config}, "layers": [{"digest": "sha256:" + "e" * 64}]}).encode() + b"\n"
    reg = tmp_path / "registry"
    reg.mkdir(exist_ok=True)
    (reg / (INGEST_REF.replace("/", "_").replace(":", "_") + ".raw")).write_bytes(raw)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _run_release_ingest(tree):
    return _run_ingest(
        tree, ("AIRGAP_DRYRUN", "0"), ("SKOPEO_REGISTRY", str(tree[0] / "registry"))
    )


def test_ingest_release_renders_job_image_by_verified_digest(ingest_tree):
    digest = _release_ingest(ingest_tree)
    r = _run_release_ingest(ingest_tree)
    assert r.returncode == 0, r.stderr
    rendered = (ingest_tree[0] / "dist" / "ingest-rendered.yaml").read_text()
    assert f"image: reg.internal:5000/qdrant-pdf-rag-ingest@{digest}" in rendered
    assert f"qdrant-pdf-rag-ingest:{IMAGE_SHA}" not in rendered.replace("value:", "")


def test_ingest_swapped_registry_tag_refuses_before_any_cluster_call_then_next_run_passes(ingest_tree):
    _release_ingest(ingest_tree, config="sha256:" + "f" * 64)
    r = _run_release_ingest(ingest_tree)
    assert r.returncode != 0
    assert "is not the packed image" in r.stderr
    kc_log = ingest_tree[1]
    assert not kc_log.exists() or "apply" not in kc_log.read_text().split()
    _release_ingest(ingest_tree)  # the tag is reloaded with the packed image
    assert _run_release_ingest(ingest_tree).returncode == 0


def test_ingest_missing_registry_image_refuses_before_any_cluster_call(ingest_tree):
    _release_ingest(ingest_tree)
    (ingest_tree[0] / "registry" / (INGEST_REF.replace("/", "_").replace(":", "_") + ".raw")).unlink()
    r = _run_release_ingest(ingest_tree)
    assert r.returncode != 0
    assert f"cannot read back registry image {INGEST_REF}" in r.stderr
    assert not ingest_tree[1].exists() or "apply" not in ingest_tree[1].read_text().split()

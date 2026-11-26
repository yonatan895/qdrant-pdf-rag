"""scripts/airgap/generations.sh: the site launcher for the retained-generation
listing and snapshot-only disposal (issue #1373).

Hermetic: real Helm renders the chart; kubectl is a recording fake. The wait,
quota and cleanup machinery is shared with ingest.sh (scripts/airgap/
one_shot_job.sh), whose own suite stays the oracle for it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.helpers_airgap import (
    STUB_TOOL,
    connected_checkout,
    install_rendering_helm,
    make_bin_tree,
    rendered_container,
    rendered_env,
    system_path,
    write_stub,
)

IMAGE_SHA = "d" * 40
BUILD = "aaaaaaaa-0000-4000-8000-000000000001"

RECORDER = """#!/bin/sh
printf '%s\\n' "$*" >> "$KC_LOG"
exit 0
"""

# A fake cluster: every call is recorded; behaviour comes from the environment.
FAKE_KC = """#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
a = sys.argv[1:]
line = ' '.join(a)
with open('operations', 'a') as f:
    f.write(line + '\\n')
env = os.environ.get
def pid(name):
    Path(name + '.tmp').write_text(str(os.getpid()))
    os.replace(name + '.tmp', name)
if 'pvc' in a:
    if env('NO_PVC') != 'true':
        print('persistentvolumeclaim/ingest-work')
elif a[:3] == ['-n', 'test-ns', 'get'] and 'job' in a and '--ignore-not-found' in a:
    name = a[a.index('job') + 1]
    if env('JOB_READ_FAILS') == name:
        sys.exit(1)
    print(env('JOB_' + name.upper(), ''), end='')
elif 'pods' in a and any(x.startswith('job-name=') for x in a):
    name = [x for x in a if x.startswith('job-name=')][0].split('=', 1)[1]
    print(env('PODS_' + name.upper(), ''), end='')
elif 'job' in a and any('metadata.uid' in x for x in a):
    print(env('JOB_UID', ''), end='')
elif 'events' in a and '--field-selector' in a:
    sel = a[a.index('--field-selector') + 1]
    if env('JOB_UID') and sel == 'involvedObject.uid=' + env('JOB_UID') + ',reason=FailedCreate':
        print(env('EVENT_MESSAGE', ''))
elif 'job' in a and any('status.active' in x for x in a):
    print('   {}', end='')
elif 'pods' in a and any('controller-uid' in x for x in a):
    pass
elif 'apply' in a:
    Path('applied').touch()
elif 'wait' in a and '--for=condition=complete' in a:
    pid('wait-pid')
    deadline = time.monotonic() + float(env('COMPLETE_AFTER', '60'))
    while time.monotonic() < deadline:
        time.sleep(.02)
    sys.exit(0 if env('COMPLETE_AFTER') else 1)
elif 'wait' in a and '--for=condition=failed' in a:
    pid('failed-pid')
    if env('FAIL_AFTER'):
        time.sleep(float(env('FAIL_AFTER')))
        sys.exit(0)
    time.sleep(60)
    sys.exit(1)
elif 'logs' in a:
    print(env('JOB_LOG', ''), end='')
    if env('LOGS_FAIL') == 'true':
        sys.exit(1)
"""


@pytest.fixture
def gen_tree(tmp_path):
    make_bin_tree(tmp_path, ["common.sh", "generations.sh", "map_values.py"])
    for name in ("kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, RECORDER)
    write_stub(tmp_path / "bin" / "helm", STUB_TOOL)
    install_rendering_helm(tmp_path)
    return tmp_path


def _run(tree, *args, extra=(), runner=None):
    env = {
        "PATH": system_path(tree / "bin"),
        "KC_LOG": str(tree / "kc-args.log"),
        "IMAGE_SHA": IMAGE_SHA,
        "INTERNAL_REGISTRY": "reg.internal:5000",
        "NAMESPACE": "test-ns",
        "STORAGE_CLASS": "gp3-csi",
        "EMBED_MODEL": "test-embed",
        "DENSE_DIM": "768",
        "EMBED_MODEL_REVISION": "rev-1",
        "EMBED_BASE_URL": "http://vllm:8000/v1",
        "OPENWEBUI_ENABLED": "false",
        "AIRGAP_DRYRUN": "1",
        "QDRANT_SHARD_NUMBER": "6",
        "QDRANT_REPLICATION_FACTOR": "3",
        "QDRANT_WRITE_CONSISTENCY_FACTOR": "2",
        "QDRANT_PEER_URLS": "http://qdrant-0.qdrant-headless:6333,http://qdrant-1.qdrant-headless:6333",
        "USER": "site-op",
    }
    env.update(dict(extra))
    connected_checkout(env, {k for k, _ in extra})
    script = tree / "scripts" / "airgap" / "generations.sh"
    return (runner or _run_with_args)(script, env, tree, args)


def _run_with_args(script, env, cwd, args):
    return subprocess.run(["sh", str(script), *args], env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=120, check=False)


def _rendered(tree):
    return (tree / "dist" / "generations-rendered.yaml").read_text()


def _cluster_calls(tree):
    log = tree / "kc-args.log"
    return log.read_text() if log.exists() else ""


def test_dryrun_renders_the_generations_job_from_the_ingest_template(gen_tree):
    result = _run(gen_tree, "list", "--format", "json", extra=(("CORPUS_PVC", "my-manuals-pvc"),))
    assert result.returncode == 0, result.stderr
    rendered = _rendered(gen_tree)
    assert "\n  name: generations\n" in rendered and "backoffLimit: 0" in rendered
    container = rendered_container(rendered, "ingest")
    assert container["command"] == ["python3", "-m", "mainframe_rag.ingest.generations"]
    assert container["args"] == ["list", "--progress", "/work/inventory.jsonl", "--format", "json"]
    assert [m["name"] for m in container["volumeMounts"]] == ["tmp", "work"]
    assert "claimName: ingest-work" in rendered and "my-manuals-pvc" not in rendered
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}}
    assert "runAsNonRoot: true" in rendered and "type: RuntimeDefault" in rendered
    assert "automountServiceAccountToken: false" in rendered
    env = rendered_env(rendered, "ingest")
    assert env["QDRANT_PEER_URLS"].startswith("http://qdrant-0.qdrant-headless:6333,")
    assert (env["QDRANT_SHARD_NUMBER"], env["QDRANT_REPLICATION_FACTOR"]) == ("6", "3")
    assert "reg.internal:5000/qdrant-pdf-rag-ingest" in rendered
    block = rendered.split("- name: QDRANT_API_KEY", 1)[1][:200]
    assert "key: api-key" in block and "read-only" not in block
    assert "apply" not in _cluster_calls(gen_tree) and "delete" not in _cluster_calls(gen_tree)


def test_dryrun_dispose_passes_execute_reference_and_operator(gen_tree):
    result = _run(gen_tree, "dispose-snapshots", "--build-id", BUILD, "--keep", "2", "--allow-below-keep",
                  "--execute", "--recovery-ref", "CHG-1373")
    assert result.returncode == 0, result.stderr
    assert rendered_container(_rendered(gen_tree), "ingest")["args"] == [
        "dispose-snapshots", "--progress", "/work/inventory.jsonl", "--build-id", BUILD, "--keep", "2",
        "--allow-below-keep", "--execute", "--recovery-ref", "CHG-1373", "--operator", "site-op"]


def test_dispose_without_execute_is_the_module_dry_run(gen_tree):
    result = _run(gen_tree, "dispose-snapshots", "--build-id", BUILD)
    assert result.returncode == 0, result.stderr
    args = rendered_container(_rendered(gen_tree), "ingest")["args"]
    assert "--execute" not in args and "--operator" not in args


@pytest.mark.parametrize(("args", "needle"), [
    ((), "usage"),
    (("purge",), "usage"),
    (("dispose-snapshots",), "at least one --build-id"),
    (("dispose-snapshots", "--build-id", BUILD.upper()), "full lowercase build UUID"),
    (("dispose-snapshots", "--build-id", "mainframe_manuals__genabc"), "full lowercase build UUID"),
    (("list", "--keep", "0"), "keeping 0 is not supported"),
    (("list", "--execute"), "unknown or misplaced argument"),
    (("dispose-snapshots", "--build-id", BUILD, "--format", "json"), "unknown or misplaced argument"),
    (("dispose-snapshots", "--build-id", BUILD, "--progress", "/elsewhere"), "unknown or misplaced argument"),
    (("dispose-snapshots", "--build-id", BUILD, "--recovery-ref", "bad ref;rm"), "--recovery-ref takes"),
    (("list", "--keep"), "--keep needs a value"),
])
def test_bad_arguments_refuse_before_any_render_or_cluster_call(gen_tree, args, needle):
    result = _run(gen_tree, *args, extra=(("AIRGAP_DRYRUN", "0"),))
    assert result.returncode == 1
    assert needle in result.stderr
    assert not (gen_tree / "dist" / "generations-rendered.yaml").exists()
    assert _cluster_calls(gen_tree) == ""


def _live(gen_tree, *args, **env):
    write_stub(gen_tree / "bin" / "kubectl", FAKE_KC)
    base = {"AIRGAP_DRYRUN": "0", "JOB_INGEST": "ingest|Complete=True,", "PODS_INGEST": "Succeeded|\n",
            "COMPLETE_AFTER": "0.3", "JOB_LOG": "GENERATIONS alias mainframe_manuals; live x\n"}
    base.update(env)
    return _run(gen_tree, *(args or ("list",)), extra=tuple(base.items()))


def _operations(tree):
    path = tree / "operations"
    return path.read_text() if path.exists() else ""


def test_after_a_finished_ingest_the_job_runs_and_its_listing_is_printed(gen_tree):
    result = _live(gen_tree)
    assert result.returncode == 0, result.stderr
    assert "GENERATIONS alias mainframe_manuals" in result.stdout
    ops = _operations(gen_tree).splitlines()
    delete = [line for line in ops if "delete job generations" in line]
    assert delete and "--request-timeout=30s" in delete[0] and "--timeout=300s" in delete[0]
    apply = next(i for i, line in enumerate(ops) if line.startswith("apply"))
    assert ops.index(delete[0]) < apply
    assert "logs job/generations" in "\n".join(ops[apply:])
    assert not any("logs -f" in line for line in ops)  # collect mode: no follower


@pytest.mark.parametrize(("env", "needle"), [
    pytest.param({"JOB_INGEST": "ingest|"}, "job/ingest exists", id="ingest-job-running"),
    pytest.param({"JOB_INGEST": "ingest|Complete=True,", "PODS_INGEST": "Running|\n"}, "job/ingest exists",
                 id="ingest-pod-still-running"),
    pytest.param({"JOB_INGEST": "", "PODS_INGEST": "Succeeded|2026-11-26T00:00:00Z\n"}, "job/ingest exists",
                 id="ingest-pod-terminating"),
    pytest.param({"JOB_READ_FAILS": "ingest"}, "cannot read job/ingest", id="ingest-unreadable"),
    pytest.param({"JOB_GENERATIONS": "generations|"}, "earlier job/generations", id="generations-running"),
    pytest.param({"NO_PVC": "true"}, "ingest-work PVC does not exist", id="no-work-volume"),
])
def test_cluster_preconditions_refuse_before_any_apply(gen_tree, env, needle):
    result = _live(gen_tree, **env)
    assert result.returncode == 1
    assert needle in result.stderr
    ops = _operations(gen_tree)
    assert "apply" not in ops and "delete" not in ops and "create" not in ops


def test_a_refusal_ends_the_wait_promptly_and_prints_the_module_output(gen_tree):
    started = time.monotonic()
    result = _live(gen_tree, "dispose-snapshots", "--build-id", BUILD, "--execute",
                   COMPLETE_AFTER="", FAIL_AFTER="0.5",
                   JOB_LOG="FAIL: E_NOT_DISPOSABLE: aaaaaaaa (KEEP_N); nothing deleted\n")
    assert time.monotonic() - started < 30
    assert result.returncode == 1
    assert "E_NOT_DISPOSABLE" in result.stdout
    assert "generations Job did not complete successfully" in result.stderr
    for name in ("wait-pid", "failed-pid"):
        child = int((gen_tree / name).read_text())
        assert not Path(f"/proc/{child}").exists(), f"{name} child survived"
    assert not (gen_tree / "dist" / "generations-failed").exists()


def test_quota_refusal_uses_the_shared_fail_fast_and_bounded_removal(gen_tree):
    message = ('Error creating: pods "generations-x" is forbidden: exceeded quota: sentinel-q, '
               'requested: limits.memory=9991Gi, used: limits.memory=1Gi, limited: limits.memory=2Gi')
    result = _live(gen_tree, JOB_UID="uid-1", EVENT_MESSAGE=message, COMPLETE_AFTER="")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ResourceQuota refused the generations Job's pod" in result.stderr
    assert "lower INGEST_MEMORY_LIMIT in airgap.env" in result.stderr
    assert "the refused generations Job was removed; rerun airgap:generations" in result.stderr
    assert "sentinel-q" not in result.stdout + result.stderr and "9991" not in result.stderr
    deletes = [line for line in _operations(gen_tree).splitlines() if "delete job generations" in line]
    assert len(deletes) == 2 and "--request-timeout=30s" in deletes[1]


@pytest.mark.parametrize(("cancel_signal", "code"), [("SIGINT", 130), ("SIGTERM", 143)])
def test_cancellation_reaps_the_local_observers(gen_tree, cancel_signal, code):
    write_stub(gen_tree / "bin" / "kubectl", FAKE_KC)

    def start(script, env, cwd, args):
        return subprocess.Popen(["sh", str(script), *args], env=env, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)

    proc = _run(gen_tree, "list", runner=start, extra=(
        ("AIRGAP_DRYRUN", "0"), ("JOB_INGEST", ""), ("PODS_INGEST", ""), ("COMPLETE_AFTER", "")))
    try:
        deadline = time.monotonic() + 20
        while not all((gen_tree / n).exists() for n in ("wait-pid", "failed-pid")):
            if proc.poll() is not None:
                pytest.fail(f"launcher exited early: {proc.communicate()}")
            assert time.monotonic() < deadline, "observers did not start"
            time.sleep(.02)
        children = [int((gen_tree / n).read_text()) for n in ("wait-pid", "failed-pid")]
        proc.send_signal(getattr(signal, cancel_signal))
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == code
        assert "job/generations in namespace test-ns may still be running" in stderr
        for child in children:
            assert not Path(f"/proc/{child}").exists(), f"observer {child} survived"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)

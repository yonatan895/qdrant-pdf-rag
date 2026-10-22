"""CI gateway wiring: explicit model legs, authenticated routing, failure propagation."""
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gateway_rendered(tmp_path):
    from tests.helpers_airgap import copy_chart, install_rendering_helm, make_bin_tree, write_stub

    make_bin_tree(tmp_path, ["common.sh", "validate.sh", "deploy.sh", "ingest.sh",
                            "smoke.sh", "pipeline.sh", "map_values.py"])
    copy_chart(tmp_path)
    shutil.copy(ROOT / "charts/qdrant-openshift.values.yaml", tmp_path / "charts")
    shutil.copy(ROOT / "images.txt", tmp_path / "images.txt")
    for name in ("skopeo", "kubectl", "oc"):
        write_stub(tmp_path / "bin" / name, "#!/bin/sh\nexit 0\n")
    install_rendering_helm(tmp_path)
    selected = {
        "INTERNAL_REGISTRY": "registry.example/test", "NAMESPACE": "gateway-test",
        "IMAGE_SHA": "a" * 40, "STORAGE_CLASS": "standard", "CORPUS_PVC": "corpus",
        "GATEWAY_BASE_URL": "https://test-gateway:4000/v1", "EMBED_MODEL": "mock-embed",
        "DENSE_DIM": "1024", "EMBED_MODEL_REVISION": "mock-embed@ci",
        "LLM_MODEL_REASONING": "mock-reasoning", "GATEWAY_API_KEY_SECRET": "test-gateway-keys",
        "GATEWAY_API_KEY_SECRET_KEY": "api-key", "QDRANT_SHARD_NUMBER": "1",
        "QDRANT_REPLICATION_FACTOR": "1", "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    }
    path = tmp_path / "airgap.env"
    example = "\n".join(line for line in (ROOT / "airgap.env.example").read_text().splitlines()
                        if not line.startswith(("EMBED_BASE_URL=", "LLM_BASE_URL=")))
    path.write_text(example
                    + "\n" + "\n".join(f"{key}={value}" for key, value in selected.items()) + "\n")
    result = subprocess.run(["sh", "scripts/airgap/pipeline.sh", "--skip-load"], cwd=tmp_path,
                            env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "AIRGAP_DRYRUN": "1"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


def check_gateway_rendered(tree, *extra):
    return subprocess.run([
        sys.executable, str(ROOT / "scripts/ci/check_gateway_rendering.py"),
        "--env-file", str(tree / "airgap.env"),
        "--agent-render", str(tree / "dist/agent-rendered.yaml"),
        "--ingest-render", str(tree / "dist/ingest-rendered.yaml"),
        "--shared-url", "https://test-gateway:4000/v1", "--secret-name", "test-gateway-keys",
        "--secret-key", "api-key", "--embed-model", "mock-embed", "--reasoning-model", "mock-reasoning",
        "--context-model", "",
        "--diagnostics", str(tree / "diagnostics/gateway.json"), *extra,
    ], capture_output=True, text=True, check=False)


def rewrite_gateway_consumer(tree, resource, mutate):
    path = tree / "dist" / ("agent-rendered.yaml" if resource == "agent" else "ingest-rendered.yaml")
    documents = [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
    kind = "Deployment" if resource == "agent" else "Job"
    name = "rag-agent" if resource == "agent" else "ingest"
    target = next(doc for doc in documents if doc and doc.get("kind") == kind
                  and doc["metadata"]["name"] == name)
    container = next(entry for entry in target["spec"]["template"]["spec"]["containers"]
                     if entry["name"] == resource)
    mutate(container)
    path.write_text(yaml.safe_dump_all(documents))


def test_ingest_local_patch_reproduces_quote_sensitive_predicate(gateway_rendered):
    path = gateway_rendered / "dist/ingest-rendered.yaml"
    before = yaml.safe_load(path.read_text())
    assert 'key: "api-key"' in path.read_text()
    patched = subprocess.run(["kubectl", "patch", "--local", "-f", str(path),
                              "-p", '{"metadata":{"labels":{"rehearsal":"true"}}}', "-o", "yaml"],
                             capture_output=True, text=True, check=False)
    assert patched.returncode == 0, patched.stderr
    path.write_text(patched.stdout)
    after = yaml.safe_load(patched.stdout)
    assert before["spec"] == after["spec"]
    source = (gateway_rendered / "airgap.env").read_text()
    assert "GATEWAY_BASE_URL=https://test-gateway:4000/v1\n" in source
    assert "GATEWAY_API_KEY_SECRET_KEY=api-key\n" in source
    assert not any(line.startswith(("EMBED_BASE_URL=", "LLM_BASE_URL=")) for line in source.splitlines())
    assert "https://test-gateway:4000/v1" in (gateway_rendered / "dist/mainframe-rag-release-values.yaml").read_text()
    assert 'key: "api-key"' in (gateway_rendered / "dist/agent-rendered.yaml").read_text()
    assert 'key: "api-key"' not in patched.stdout
    assert "key: api-key" in patched.stdout
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 0, result.stderr
    diagnostic = json.loads((gateway_rendered / "diagnostics/gateway.json").read_text())
    assert diagnostic["passed"] is True
    assert len(diagnostic["consumers"]) == 5


@pytest.mark.parametrize("resource,key,fault", [
    ("agent", "LLM_API_KEY", "name"), ("agent", "EMBED_API_KEY", "key"),
    ("agent", "RERANK_API_KEY", "missing"), ("ingest", "EMBED_API_KEY", "optional"),
    ("ingest", "CONTEXT_LLM_API_KEY", "plaintext"),
])
def test_gateway_check_identifies_each_wrong_or_missing_reference(gateway_rendered, resource, key, fault):
    def mutate(container):
        entry = next(entry for entry in container["env"] if entry["name"] == key)
        if fault == "missing":
            container["env"].remove(entry)
        elif fault == "plaintext":
            entry.pop("valueFrom")
            entry["value"] = "private-key-sentinel"
        elif fault == "optional":
            entry["valueFrom"]["secretKeyRef"]["optional"] = True
        else:
            entry["valueFrom"]["secretKeyRef"][fault] = "wrong-reference"
    rewrite_gateway_consumer(gateway_rendered, resource, mutate)
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert f"container {resource} env {key}" in result.stderr
    diagnostic = (gateway_rendered / "diagnostics/gateway.json").read_text()
    assert "private-key-sentinel" not in diagnostic + result.stdout + result.stderr
    assert json.loads(diagnostic)["passed"] is False


@pytest.mark.parametrize("resource,key", [("agent", "LLM_BASE_URL"), ("ingest", "CONTEXT_LLM_BASE_URL")])
def test_gateway_check_identifies_wrong_url(gateway_rendered, resource, key):
    def mutate(container):
        entry = next(entry for entry in container["env"] if entry["name"] == key)
        entry["value"] = "https://user:private-password@wrong-host/private-path?key=private-token#private-fragment"
    rewrite_gateway_consumer(gateway_rendered, resource, mutate)
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert f"container {resource} env {key}: resolved URL" in result.stderr
    diagnostic = (gateway_rendered / "diagnostics/gateway.json").read_text()
    assert "wrong-host" in diagnostic
    assert "private-" not in diagnostic + result.stdout + result.stderr


def test_gateway_check_rejects_placeholders_with_attribution(gateway_rendered):
    resource = "ingest"
    path = gateway_rendered / "dist" / f"{resource}-rendered.yaml"
    path.write_text(path.read_text() + "\n# __UNRESOLVED_GATEWAY__\n")
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert f"{resource} render: unresolved placeholder" in result.stderr
    assert (gateway_rendered / "diagnostics/gateway.json").exists()


def test_gateway_check_retains_leg_overrides_and_legacy_secret_keys(gateway_rendered):
    source = gateway_rendered / "airgap.env"
    source.write_text(source.read_text() + "\nLLM_BASE_URL=https://reasoning-override/v1\nGATEWAY_API_KEY_SECRET_KEY=\n")
    legacy_keys = {"LLM_API_KEY": "llm-api-key", "EMBED_API_KEY": "embed-api-key",
                   "RERANK_API_KEY": "rerank-api-key", "CONTEXT_LLM_API_KEY": "context-llm-api-key"}
    def mutate(container):
        for entry in container["env"]:
            if entry["name"] in legacy_keys:
                entry["valueFrom"]["secretKeyRef"]["key"] = legacy_keys[entry["name"]]
            if entry["name"] == "LLM_BASE_URL":
                entry["value"] = "https://reasoning-override/v1"
        container["env"].append({"name": "UNRELATED_PRIVATE_VALUE", "value": "private-sentinel"})
    for resource in ("agent", "ingest"):
        rewrite_gateway_consumer(gateway_rendered, resource, mutate)
    path = gateway_rendered / "dist/agent-rendered.yaml"
    path.write_text(path.read_text() + yaml.safe_dump({
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "unrelated"},
        "stringData": {"api-key": "private-secret-data-sentinel"},
    }, explicit_start=True))
    result = check_gateway_rendered(gateway_rendered, "--llm-url", "https://reasoning-override/v1", "--secret-key", "")
    assert result.returncode == 0, result.stderr
    diagnostic = (gateway_rendered / "diagnostics/gateway.json").read_text()
    assert "private-sentinel" not in diagnostic + result.stdout + result.stderr
    assert "private-secret-data-sentinel" not in diagnostic + result.stdout + result.stderr
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert "operator configuration LLM_BASE_URL" in result.stderr


@pytest.mark.parametrize("fault", ["container", "resource", "duplicate-env", "model"])
def test_gateway_check_requires_exact_consumer_identity(gateway_rendered, fault):
    def mutate(container):
        if fault == "container":
            container["name"] = "other"
        elif fault == "duplicate-env":
            container["env"].append(next(entry for entry in container["env"] if entry["name"] == "LLM_API_KEY"))
        else:
            next(entry for entry in container["env"] if entry["name"] == "LLM_MODEL_REASONING")["value"] = "wrong"
    if fault == "resource":
        path = gateway_rendered / "dist/agent-rendered.yaml"
        documents = [doc for doc in yaml.safe_load_all(path.read_text()) if doc]
        next(doc for doc in documents if doc.get("kind") == "Deployment"
             and doc["metadata"]["name"] == "rag-agent")["metadata"]["name"] = "other"
        path.write_text(yaml.safe_dump_all(documents))
    else:
        rewrite_gateway_consumer(gateway_rendered, "agent", mutate)
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert "Deployment/rag-agent container agent" in result.stderr


@pytest.mark.parametrize("fault", ["wrong", "missing", "valueFrom"])
def test_gateway_check_requires_exact_context_model(gateway_rendered, fault):
    def mutate(container):
        entry = next(entry for entry in container["env"] if entry["name"] == "CONTEXT_LLM_MODEL")
        if fault == "missing":
            container["env"].remove(entry)
        elif fault == "duplicate":
            container["env"].append(entry.copy())
        elif fault == "valueFrom":
            entry.pop("value")
            entry["valueFrom"] = {"secretKeyRef": {"name": "private-model-sentinel", "key": "alias"}}
        else:
            entry["value"] = "private-model-sentinel"
    rewrite_gateway_consumer(gateway_rendered, "ingest", mutate)
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert "Job/ingest container ingest env CONTEXT_LLM_MODEL" in result.stderr
    diagnostic = (gateway_rendered / "diagnostics/gateway.json").read_text()
    assert "private-model-sentinel" not in diagnostic + result.stdout + result.stderr
    assert json.loads(diagnostic)["passed"] is False


def test_gateway_check_rejects_source_context_model_drift(gateway_rendered):
    source = gateway_rendered / "airgap.env"
    source.write_text(source.read_text() + "\nCONTEXT_LLM_MODEL=private-model-sentinel\n")
    result = check_gateway_rendered(gateway_rendered)
    assert result.returncode == 1
    assert "operator configuration CONTEXT_LLM_MODEL: selected value differs" in result.stderr
    diagnostic = (gateway_rendered / "diagnostics/gateway.json").read_text()
    assert "private-model-sentinel" not in diagnostic + result.stdout + result.stderr
    assert json.loads(diagnostic)["passed"] is False


@pytest.mark.parametrize("context_model", ["", "context alias | :/v2"])
def test_gateway_check_round_trips_context_model_and_rejects_drift(gateway_rendered, context_model):
    source = gateway_rendered / "airgap.env"
    source.write_text(source.read_text()
                      + f"\nCONTEXT_LLM_MODEL={shlex.quote(context_model)}\n"
                      + f"CONTEXTUAL_EMBED_ENABLED={'true' if context_model else 'false'}\n")
    rendered = subprocess.run(["sh", "scripts/airgap/pipeline.sh", "--skip-load"], cwd=gateway_rendered,
                              env={"PATH": f"{gateway_rendered / 'bin'}:/usr/bin:/bin", "AIRGAP_DRYRUN": "1"},
                              capture_output=True, text=True, check=False)
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    job = yaml.safe_load((gateway_rendered / "dist/ingest-rendered.yaml").read_text())
    container = next(entry for entry in job["spec"]["template"]["spec"]["containers"]
                     if entry["name"] == "ingest")
    entry = next(entry for entry in container["env"] if entry["name"] == "CONTEXT_LLM_MODEL")
    assert entry["value"] == context_model
    assert next(entry for entry in container["env"] if entry["name"] == "CONTEXTUAL_EMBED_ENABLED")["value"] == (
        "true" if context_model else "false")
    assert next(entry for entry in container["env"] if entry["name"] == "CONTEXT_LLM_BASE_URL")["value"] == (
        "https://test-gateway:4000/v1")
    result = check_gateway_rendered(gateway_rendered, "--context-model", context_model)
    assert result.returncode == 0, result.stderr
    def mutate(container):
        next(entry for entry in container["env"] if entry["name"] == "CONTEXT_LLM_MODEL")["value"] = "wrong"
    rewrite_gateway_consumer(gateway_rendered, "ingest", mutate)
    result = check_gateway_rendered(gateway_rendered, "--context-model", context_model)
    assert result.returncode == 1
    assert "Job/ingest container ingest env CONTEXT_LLM_MODEL: model alias differs" in result.stderr


def test_workflow_has_no_duplicate_mapping_keys():
    # safe_load silently keeps the last duplicate; GitHub rejects the entire
    # workflow before creating any job/check. Inspect the YAML nodes instead.
    def visit(node):
        if isinstance(node, yaml.MappingNode):
            keys = [key.value for key, _ in node.value]
            assert len(keys) == len(set(keys)), f'duplicate keys at line {node.start_mark.line + 1}'
            for _, value in node.value:
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                visit(value)
    visit(yaml.compose((ROOT / '.github/workflows/e2e.yml').read_text()))


@pytest.mark.parametrize('fault_case,bad_probe', [
    *[(case, 'expected-failure') for case in (
        'all', 'chat-upstream', 'chat-malformed', 'chat-truncated',
        'embed-upstream', 'embed-malformed', 'embed-dimension', 'auth')],
    # A lane must fail unless the fault produced the contract failure and recovery.
    ('all', 'false-success'), ('all', 'unrelated-failure'), ('all', 'state-not-ready'),
    ('auth', 'false-success'),
])
def test_fault_lane_requires_contract_failure_and_recovery(tmp_path, bad_probe, fault_case):
    log = tmp_path / 'calls'
    kc = tmp_path / 'kubectl'
    kc.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
root=Path(os.environ['FAKE_STATE'])
with (root/'calls').open('a') as out: out.write(' '.join(sys.argv[1:])+'\\n')
if 'set' in sys.argv:
    values=[x for x in sys.argv if x.startswith('MOCK_')]
    (root/'fault').write_text(' '.join(values))
if 'exec' in sys.argv and 'deploy/test-gateway' in sys.argv:
    print('MOCK STATE READY' if os.environ['PROBE_MODE']!='state-not-ready' else 'state did not converge')
    sys.exit(1 if os.environ['PROBE_MODE']=='state-not-ready' else 0)
if 'exec' in sys.argv:
    fault=(root/'fault').read_text() if (root/'fault').exists() else ''
    failing=any(x in fault for x in ('upstream','malformed','truncated','dimension','5000')) or 'env' in sys.argv
    if '-' in sys.argv:
        if failing != ('--expect-failure' in sys.argv):
            print('APPLICATION CONTRACT FAILED');sys.exit(1)
        print('APPLICATION CONTRACT PASSED');sys.exit(0)
    if failing and os.environ['PROBE_MODE']=='expected-failure':
        print('GATEWAY PROBE FAILED');sys.exit(1)
    if failing and os.environ['PROBE_MODE']=='unrelated-failure':
        print('pod disappeared');sys.exit(1)
    print('GATEWAY PROBE PASSED')
''')
    kc.chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/gateway_faults.sh'), 'test', fault_case],
        env={**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH'], 'FAKE_STATE': str(tmp_path), 'PROBE_MODE': bad_probe},
        capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (bad_probe == 'expected-failure'), result.stderr
    lines = log.read_text().splitlines()
    assert any('MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0' in line for line in lines)
    if not result.returncode:
        mutations = [line for line in lines if 'set env deploy/vllm-mock' in line
                     and 'MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0' not in line]
        expected = {
            'chat-upstream': 'MOCK_CHAT_FAULT=upstream MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0',
            'chat-malformed': 'MOCK_CHAT_FAULT=malformed MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0',
            'chat-truncated': 'MOCK_CHAT_FAULT=truncated MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0',
            'embed-upstream': 'MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=upstream MOCK_TTFT_MS=0',
            'embed-malformed': 'MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=malformed MOCK_TTFT_MS=0',
            'embed-dimension': 'MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=dimension MOCK_TTFT_MS=0',
            'auth': 'MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=5000',
        }
        required = list(expected.values()) if fault_case == 'all' else [expected[fault_case]]
        assert [line.split('deploy/vllm-mock ', 1)[1] for line in mutations] == required
        credentials = [line for line in lines if 'exec deploy/rag-agent' in line and ' env ' in line]
        assert len(credentials) == (4 if fault_case in ('all', 'auth') else 0)
        assert 'GATEWAY PROBE PASSED' in result.stdout
        assert 'APPLICATION CONTRACT PASSED' in result.stdout
        assert lines[-1] == '-n test exec -i deploy/rag-agent -- python3 -'
        if fault_case == 'all':
            assert sum('exec deploy/rag-agent' in line for line in lines) >= 17


def test_parallel_build_join_preserves_failure_and_sha_bound_inputs(tmp_path):
    jobs = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())['jobs']
    assert jobs['build-inputs']['needs'] == 'select'
    assert jobs['build-inputs']['strategy']['matrix'] == {'artifact': ['wheelhouse', 'bm25-weights']}
    assert jobs['images']['needs'] == ['select', 'build-inputs']
    assert jobs['images']['strategy']['matrix'] == {'role': ['ingest', 'agent']}
    for name in ('build-inputs', 'images'):
        assert jobs[name]['strategy']['fail-fast'] is False
        assert 'max-parallel' not in jobs[name]['strategy']
    steps = jobs['images']['steps']
    downloads = [step['with']['name'] for step in steps if 'download-artifact@' in step.get('uses', '')]
    assert downloads == [f'build-{artifact}-${{{{ github.sha }}}}'
                         for artifact in ('wheelhouse', 'bm25-weights')]
    upload = next(step['with'] for step in jobs['build-inputs']['steps'] if 'upload-artifact@' in step.get('uses', ''))
    assert upload['name'] == 'build-${{ matrix.artifact }}-${{ github.sha }}'
    assert upload['overwrite'] is True
    assert upload['include-hidden-files'] is True
    assert all('run-id' not in step['with'] and 'repository' not in step['with']
               for step in steps if 'download-artifact@' in step.get('uses', ''))
    build = jobs['build']
    assert build['needs'] == ['select', 'build-inputs', 'images']
    assert build['if'].startswith('always() &&')
    guard = build['steps'][0]
    for field in ('SELECT_RESULT', 'INPUTS_RESULT', 'IMAGES_RESULT'):
        for result in ('success', 'failure', 'cancelled', 'skipped', ''):
            env = {key: 'success' for key in ('SELECT_RESULT', 'INPUTS_RESULT', 'IMAGES_RESULT')}
            env[field] = result
            process = subprocess.run(['sh', '-eu', '-c', guard['run']], env=env, check=False)
            assert (process.returncode == 0) == (result == 'success')
    push = next(step for step in steps if step.get('name') == 'Push to GHCR')
    assert "github.event_name == 'workflow_dispatch'" in push['if']
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in push['if']
    assert 'pull_request' not in push['if']
    refs = next(step['run'] for step in build['steps'] if step.get('id') == 'refs')
    output = tmp_path / 'outputs'
    subprocess.run(['sh', '-eu', '-c', refs],
                   env={'GITHUB_OUTPUT': str(output), 'GITHUB_REPOSITORY_OWNER': 'MixedOwner',
                        'GITHUB_SHA': 'a' * 40}, check=True)
    assert output.read_text().splitlines() == [
        'sha=' + 'a' * 40, 'ingest=ghcr.io/mixedowner/qdrant-pdf-rag-ingest:' + 'a' * 40,
        'agent=ghcr.io/mixedowner/qdrant-pdf-rag-agent:' + 'a' * 40]


def test_installer_artifact_corruption_stops_before_installation(tmp_path):
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    installers = [step['run'] for job in workflow['jobs'].values() for step in job['steps']
                  if step.get('name', '').startswith(('Install checksum-pinned', 'Install pinned kubectl', 'Install pinned kind'))]
    assert installers
    # Exercise the actual shell gates with corrupt downloaded bytes. The curl
    # function avoids the network; sudo records any unsafe attempt to install.
    harness = r"""curl() {
        while [ "$#" -gt 0 ]; do
            if [ "$1" = -o ]; then shift; printf corrupt > "$1"; return 0; fi
            shift
        done
        return 1
    }
    sudo() { touch "$RUNNER_TEMP/install-attempt"; return 0; }
    """
    for index, installer in enumerate(installers):
        directory = tmp_path / str(index)
        directory.mkdir()
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', harness + installer],
            env={**os.environ, 'RUNNER_TEMP': str(directory)}, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert 'FAILED' in result.stdout + result.stderr
        assert not (directory / 'install-attempt').exists()




# --------------------------------------------------------------------------
# Rerank stand-in (issue #815): the CI model stand-in serves both rerank wire
# legs behind the test gateway. These tests drive the REAL HttpReranker and
# probe_reranker against it over loopback HTTP, so a stand-in whose wire shape
# the product client cannot read fails here, before a Kind run.

def _start_rerank_mock(tag, fault="healthy"):
    import importlib.util
    import threading
    from http.server import ThreadingHTTPServer

    saved = os.environ.get("MOCK_RERANK_FAULT")
    os.environ["MOCK_RERANK_FAULT"] = fault
    try:
        spec = importlib.util.spec_from_file_location(f"mock_vllm_rerank_{tag}", ROOT / "scripts/mock_vllm.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            os.environ.pop("MOCK_RERANK_FAULT", None)
        else:
            os.environ["MOCK_RERANK_FAULT"] = saved
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def rerank_mock():
    started = []

    def start(fault="healthy"):
        server, url = _start_rerank_mock(len(started), fault)
        started.append(server)
        return url

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _mock_stats(url):
    import httpx2

    return httpx2.get(url + "/stats", timeout=5).json()


def _rerank_settings(url, order):
    from mainframe_rag.config import Settings

    return Settings(rerank_base_url=url + "/v1", rerank_model="mock-rerank",
                    rerank_endpoint_order=order, rerank_batch_size=32, _env_file=None)


@pytest.mark.parametrize("order", ["score_first", "rerank_first"])
def test_rerank_stand_in_reverses_candidates_on_either_leg_and_counts_one_call(rerank_mock, order):
    import httpx2

    from mainframe_rag.retrieve.rerank import HttpReranker

    url = rerank_mock()
    reranker = HttpReranker(_rerank_settings(url, order), client=httpx2.Client())
    scores = reranker.score("how do operators reissue a rejected command", ["a", "b", "c", "d"])
    # The stand-in's only opinion: later candidates score higher, so reranking
    # exactly reverses the order it was handed. It says nothing about relevance.
    assert scores == sorted(scores) and len(set(scores)) == 4
    used, unused = ("score", "rerank") if order == "score_first" else ("rerank", "score")
    stats = _mock_stats(url)
    assert stats[used] == {"search": 1, "probe": 0}
    assert stats[unused] == {"search": 0, "probe": 0}


def test_rerank_stand_in_separates_the_readiness_probe_from_search_calls(rerank_mock):
    import httpx2

    from mainframe_rag.retrieve.rerank import HttpReranker, probe_reranker

    url = rerank_mock()
    reranker = HttpReranker(_rerank_settings(url, "score_first"), client=httpx2.Client())
    assert probe_reranker(reranker) is None
    assert _mock_stats(url)["score"] == {"search": 0, "probe": 1}
    reranker.score("probe", ["probe", "other"])  # a real batch that merely starts like the probe
    assert _mock_stats(url)["score"] == {"search": 1, "probe": 1}


def test_rerank_stand_in_score_route_unavailable_is_served_by_the_other_leg(rerank_mock):
    import httpx2

    from mainframe_rag.retrieve.rerank import HttpReranker

    healthy, faulted = rerank_mock(), rerank_mock("score_unavailable")
    texts = ["a", "b", "c"]
    expected = HttpReranker(_rerank_settings(healthy, "rerank_first"), client=httpx2.Client()).score("q", texts)
    got = HttpReranker(_rerank_settings(faulted, "score_first"), client=httpx2.Client()).score("q", texts)
    assert got == expected
    stats = _mock_stats(faulted)
    assert stats["score"] == {"search": 1, "probe": 0} and stats["rerank"] == {"search": 1, "probe": 0}
    assert _mock_stats(faulted).get("rerank_fault") == "score_unavailable"


def test_rerank_stand_in_unavailable_exhausts_both_legs_without_echoing_the_request(rerank_mock):
    import httpx2

    from mainframe_rag.retrieve.rerank import HttpReranker

    url = rerank_mock("unavailable")
    reranker = HttpReranker(_rerank_settings(url, "score_first"), client=httpx2.Client())
    with pytest.raises(httpx2.HTTPStatusError) as exc:
        reranker.score("secret-looking-query", ["passage-body"])
    assert exc.value.response.status_code == 503
    assert "secret-looking-query" not in exc.value.response.text
    assert "passage-body" not in exc.value.response.text
    stats = _mock_stats(url)
    assert stats["score"]["search"] == 1 and stats["rerank"]["search"] == 1



# --- rerank rehearsal scripts (issue #815): the logic that runs in CI is exercised
# here against scripted stand-ins, so a script bug fails before a Kind run.

def _load_script(name):
    import importlib.util

    spec = importlib.util.spec_from_file_location("rerank_" + name, ROOT / "scripts/ci" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observation(order_ids, scores, *, rerank=True, kind="nl"):
    return {"query": "q", "scope": {"product": "z/OS", "version": "V9R9"}, "query_kind": kind,
            "hits": [{"chunk_id": cid, "score": fused, "rerank_score": (rr if rerank else None),
                      "doc_id": "d", "page_label": "1"}
                     for cid, (fused, rr) in zip(order_ids, scores, strict=True)]}


# Fusion order c1 > c2 > c3 > c4; the stand-in scores position i of n as (i+1)/n.
FUSED = [(0.9, 0.25), (0.7, 0.5), (0.5, 0.75), (0.3, 1.0)]


def _off_on():
    off = _observation(["c1", "c2", "c3", "c4"], [(f, None) for f, _ in FUSED], rerank=False)
    on = _observation(["c4", "c3", "c2", "c1"], list(reversed(FUSED)))
    return on, off


def test_rerank_compare_accepts_exactly_the_stand_in_reversal():
    checks = _load_script("rerank_checks")
    on, off = _off_on()
    checks.compare(on, off)


@pytest.mark.parametrize("mutate,message", [
    (lambda on, off: off["hits"][0].update(rerank_score=0.5), "rerank off but hits carry"),
    (lambda on, off: on["hits"][0].update(rerank_score=None), "no rerank_score"),
    (lambda on, off: on["hits"].reverse(), "same order"),
    (lambda on, off: on["hits"][0].update(rerank_score=0.99), "positional scores"),
    (lambda on, off: [on["hits"].__delitem__(slice(1, None)), off["hits"].__delitem__(slice(1, None))], "need 2"),
    (lambda on, off: on.update(query_kind="identifier"), "bypasses rerank"),
    (lambda on, off: on["hits"][0].update(chunk_id="x9"), "different candidate sets"),
    (lambda on, off: on.update(scope={"product": "other"}), "not the same scoped query"),
])
def test_rerank_compare_refuses_each_false_proof(mutate, message):
    import re

    checks = _load_script("rerank_checks")
    on, off = _off_on()
    mutate(on, off)
    with pytest.raises(checks.CheckFailed) as exc:
        checks.compare(on, off)
    assert re.search(message, str(exc.value))


def test_rerank_compare_refuses_an_unchanged_order_even_with_stand_in_scores():
    checks = _load_script("rerank_checks")
    on, off = _off_on()
    # Scores that do not invert the fusion order (rank-aligned) cannot be explained by the stand-in.
    on["hits"] = [dict(hit, rerank_score=rr) for hit, rr in zip(off["hits"], [0.25, 0.5, 0.75, 1.0], strict=True)]
    with pytest.raises(checks.CheckFailed, match="invert|same order"):
        checks.compare(on, off)


def test_rerank_delta_and_same_order_checks(tmp_path):
    checks = _load_script("rerank_checks")
    before = {"score": {"search": 3, "probe": 9}, "rerank": {"search": 0, "probe": 2}}
    after = {"score": {"search": 4, "probe": 12}, "rerank": {"search": 0, "probe": 2}}
    checks.delta(before, after, ["score.search=1", "rerank.search=0"])
    with pytest.raises(checks.CheckFailed, match="expected 0 request"):
        checks.delta(before, after, ["score.search=0"])
    on, _ = _off_on()
    checks.same_order(on, json.loads(json.dumps(on)))
    other = json.loads(json.dumps(on))
    other["hits"][0]["rerank_score"] = 0.123
    with pytest.raises(checks.CheckFailed, match="fallback leg"):
        checks.same_order(on, other)
    # The CLI prints a fixed verdict and exits non-zero, never a traceback or contents.
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"unexpected": 1}))
    assert checks.main(["compare", str(path), str(path)]) == 1


class _RerankStack:
    """Scripted application + gateway for rerank_contracts.py. `down` turns the
    rerank routes off the way the unavailable stand-in does."""

    KEY, LLM, EMBED = "sk-rerank-leg", "sk-llm-leg", "sk-embed-leg"

    def __init__(self):
        import httpx2

        self.down = False
        self.accept_wrong_key = False
        self.leak = False
        self.requests = []
        self._client = httpx2.Client
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, request):
        import httpx2

        self.requests.append((request.method, str(request.url)))
        path = request.url.path
        auth = request.headers.get("authorization", "")
        if request.url.host == "test-gateway":
            if auth == "Bearer " + self.KEY or (self.accept_wrong_key and auth == "Bearer sk-wrong"):
                key = {"/v1/score": "data", "/v1/rerank": "results"}[path]
                return httpx2.Response(200, json={key: [{"index": 0}, {"index": 1}]})
            body = {"error": "denied " + (self.KEY if self.leak else "")}
            return httpx2.Response(403 if auth == "Bearer " + self.LLM else 401, json=body)
        if path == "/healthz":
            if self.down:
                return httpx2.Response(503, json={"status": "degraded", "rerank": False})
            return httpx2.Response(200, json={"status": "ok", "rerank": True})
        if path == "/v1/search":
            payload = json.loads(request.content)
            if self.down and payload["query"].islower():
                return httpx2.Response(502, json={"code": "upstream_error", "message": "retrieval failed"})
            scored = not payload["query"].isupper() and not self.down and "IEA500I" not in payload["query"]
            hits = [{"chunk_id": f"c{i}", "score": 1 - i / 10, "rerank_score": (i + 1) / 3 if scored else None,
                     "doc_id": "d", "page_label": str(i), "product": "z/OS", "version": "V9R9"} for i in range(3)]
            return httpx2.Response(200, json={"query_kind": "nl", "hits": hits})
        return httpx2.Response(404)

    def install(self, monkeypatch, module):
        import httpx2

        monkeypatch.setattr(httpx2, "Client",
                            lambda **kw: self._client(transport=self.transport, **kw))
        monkeypatch.setattr(module.time, "sleep", lambda _s: None)
        for name, value in (("RERANK_BASE_URL", "https://test-gateway:4000/v1"), ("RERANK_MODEL", "mock-rerank"),
                            ("RERANK_API_KEY", self.KEY), ("LLM_API_KEY", self.LLM), ("EMBED_API_KEY", self.EMBED)):
            monkeypatch.setenv(name, value)


def test_rerank_contracts_gateway_auth_passes_and_prints_no_key(monkeypatch, capsys):
    module = _load_script("rerank_contracts")
    stack = _RerankStack()
    stack.install(monkeypatch, module)
    assert module.main(["gateway-auth"]) == 0
    out = capsys.readouterr().out
    assert out.count("PASS:") == 8
    for secret in (stack.KEY, stack.LLM, stack.EMBED):
        assert secret not in out


@pytest.mark.parametrize("fault,label", [
    ("accept_wrong_key", "wrong key refused"), ("leak", "echoes a key"),
])
def test_rerank_contracts_gateway_auth_fails_on_each_gateway_fault(monkeypatch, capsys, fault, label):
    module = _load_script("rerank_contracts")
    stack = _RerankStack()
    setattr(stack, fault, True)
    stack.install(monkeypatch, module)
    assert module.main(["gateway-auth"]) == 1
    out = capsys.readouterr().out
    assert "RERANK CONTRACT FAILED: gateway-auth" in out and label in out and stack.KEY not in out


@pytest.mark.parametrize("env", [
    {"RERANK_API_KEY": "sk-unused-rerank-disabled"}, {"RERANK_API_KEY": _RerankStack.LLM},
])
def test_rerank_contracts_refuses_a_placeholder_or_shared_rerank_key(monkeypatch, capsys, env):
    module = _load_script("rerank_contracts")
    stack = _RerankStack()
    stack.install(monkeypatch, module)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert module.main(["gateway-auth"]) == 1
    assert "rerank key wiring" in capsys.readouterr().out


def test_rerank_contracts_degraded_asserts_fixed_failure_bypass_and_unready(monkeypatch, capsys):
    module = _load_script("rerank_contracts")
    stack = _RerankStack()
    stack.install(monkeypatch, module)
    stack.down = True
    assert module.main(["degraded", "--scope-json", '{"product": "z/OS", "version": "V9R9"}']) == 0
    assert capsys.readouterr().out.count("PASS:") == 3
    # A stack that kept serving 200 is not the degraded contract.
    stack.down = False
    assert module.main(["degraded", "--scope-json", "{}"]) == 1


def test_rerank_contracts_search_observation_carries_scores_not_text(monkeypatch, capsys):
    module = _load_script("rerank_contracts")
    stack = _RerankStack()
    stack.install(monkeypatch, module)
    assert module.main(["discover-scope"]) == 0
    scope = capsys.readouterr().out
    assert json.loads(scope) == {"product": "z/OS", "version": "V9R9"}
    assert module.main(["search", "--scope-json", scope]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["query_kind"] == "nl" and observed["scope"] == json.loads(scope)
    assert {key for hit in observed["hits"] for key in hit} == {
        "chunk_id", "score", "rerank_score", "doc_id", "page_label"}
    # The rehearsal query must stay lowercase natural language: an identifier-shaped
    # query would be bypassed by rerank and prove nothing.
    from mainframe_rag.retrieve.filters import parse_query, query_kind
    assert query_kind(parse_query(module.QUERY)) == "nl"
    assert query_kind(parse_query(module.IDENTIFIER_QUERY)) == "identifier"



def test_rerank_lane_is_the_only_lane_that_enables_rerank(tmp_path):
    """Issue #815: the operator airgap.env step is the one place a lane's RERANK_*
    comes from. Run its real shell for every matrix lane: only `rerank` turns the
    leg on (with the gateway URL and stand-in model); every other lane keeps the
    rerank-off configuration and its expected results."""
    import textwrap

    workflow = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text())
    job = workflow["jobs"]["kind-live-rehearsal"]
    lanes = job["strategy"]["matrix"]["lane"]
    assert "rerank" in lanes and not any(lane.startswith("gateway-") for lane in lanes if lane == "rerank")
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Operator airgap.env"))
    for lane in lanes:
        root = tmp_path / lane
        (root / "gapbox/qdrant-pdf-rag").mkdir(parents=True)
        (root / "gapbox/qdrant-pdf-rag/airgap.env").write_text(
            (ROOT / "airgap.env.example").read_text())
        script = step["run"].replace("${{ matrix.lane }}", lane)
        script = script.replace("${{ env.SHA }}", "a" * 40)
        result = subprocess.run(["bash", "-e", "-c", textwrap.dedent(script)], cwd=root, capture_output=True,
                                text=True, check=False,
                                env={**os.environ, "SHA": "a" * 40, "IMAGE_SHA": "a" * 40,
                                     "GITHUB_ENV": str(root / "github_env")})
        assert result.returncode == 0, (lane, result.stderr)
        values = {}
        for line in (root / "gapbox/qdrant-pdf-rag/airgap.env").read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.startswith("#"):
                values[name] = value  # last assignment wins, as when the file is sourced
        if lane == "rerank":
            assert values["RERANK_ENABLED"] == "true"
            assert values["RERANK_BASE_URL"] == "https://test-gateway:4000/v1"
            assert values["RERANK_MODEL"] == "mock-rerank"
            assert values["GATEWAY_API_KEY_SECRET"] == "test-gateway-keys"
            assert "GATEWAY_API_KEY_SECRET_KEY" not in values  # per-leg keys: rerank-api-key is used
        else:
            assert values["RERANK_ENABLED"] == "false", lane
            assert "RERANK_BASE_URL" not in values and "RERANK_MODEL" not in values, lane


def test_rerank_rehearsal_step_runs_only_in_its_lane_behind_the_egress_gate():
    job = yaml.safe_load((ROOT / ".github/workflows/e2e.yml").read_text())["jobs"]["kind-live-rehearsal"]
    steps = [s for s in job["steps"] if "rerank_rehearsal.sh" in s.get("run", "")]
    assert len(steps) == 1
    assert steps[0]["if"] == "matrix.lane == 'rerank'"
    assert steps[0]["shell"] == "sh rehearsal-infra/scripts/ci/egress_gate.sh run bash -e {0}"
    names = [s.get("name") for s in job["steps"]]
    assert names.index(steps[0]["name"]) > names.index("Close the network boundary (block public egress, prove it)")


def test_test_gateway_serves_the_rerank_stand_in_on_both_wire_legs():
    documents = list(yaml.safe_load_all((ROOT / "scripts/ci/test-gateway.yaml").read_text()))
    config = yaml.safe_load(next(d for d in documents if d["kind"] == "ConfigMap")["data"]["config.yaml"])
    model = next(m for m in config["model_list"] if m["model_name"] == "mock-rerank")
    assert model["litellm_params"]["model"] == "hosted_vllm/mock-rerank"
    assert model["litellm_params"]["api_base"] == "http://vllm-mock:8000/v1"
    routes = {p["path"]: p for p in config["general_settings"]["pass_through_endpoints"]}
    assert routes["/v1/score"]["target"] == "http://vllm-mock:8000/v1/score"
    assert routes["/v1/score"]["methods"] == ["POST"]
    # The pass-through guard reapplies the key's model scope to this route too.
    assert "scoped_passthrough.guard" in config["litellm_settings"]["callbacks"]


def test_minted_rerank_key_is_a_real_key_scoped_to_the_rerank_model_alone(capsys, monkeypatch):
    import urllib.error

    mint = _load_script("mint_gateway_keys")
    generated = []

    def call(path, key, payload=None, *, timeout=30):
        if key in ("", "sk-wrong"):
            raise urllib.error.HTTPError(path, 401, "denied", None, None)
        if path == "/key/generate":
            generated.append(payload["models"])
            return {"key": f"sk-minted-{len(generated)}"}
        return {}

    monkeypatch.setenv("GATEWAY_MASTER_KEY", "sk-master")
    monkeypatch.setattr(mint, "call", call)
    monkeypatch.setattr(mint, "wait_for_service", lambda _master: None)
    mint.main()
    keys = json.loads(capsys.readouterr().out)
    assert generated.count(["mock-rerank"]) == 1
    rerank = keys["rerank-api-key"]
    assert rerank.startswith("sk-minted-") and "unused" not in rerank
    assert rerank not in {v for name, v in keys.items() if name != "rerank-api-key"}
    # The shared key keeps its two-model scope: the rerank rehearsal is per-leg.
    assert ["mock-reasoning", "mock-embed"] in generated


FAKE_RERANK_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_STATE"])
argv = sys.argv[1:]
with (root / "calls").open("a") as out:
    out.write(" ".join(argv) + "\n")
state = json.loads((root / "state").read_text()) if (root / "state").exists() else {
    "fault": "healthy", "rerank": "on", "score": 0, "rerank_calls": 0}
def save():
    (root / "state").write_text(json.dumps(state))
if "set" in argv and "env" in argv:
    for item in argv:
        if item.startswith("MOCK_RERANK_FAULT="):
            state["fault"] = item.split("=", 1)[1]
        if item == "RERANK_ENABLED=false":
            state["rerank"] = "off"
    save(); sys.exit(0)
if "rollout" in argv:
    sys.exit(0)
if "exec" not in argv:
    sys.exit(0)
words = argv[argv.index("-") + 1:] if "-" in argv else []
sys.stdin.read()
if "deploy/test-gateway" in argv:
    if words[:1] == ["stats"]:
        print(json.dumps({"score": {"search": state["score"], "probe": 7},
                          "rerank": {"search": state["rerank_calls"], "probe": 0}, "rerank_fault": state["fault"]}))
    else:
        print("RERANK MOCK STATE READY " + words[1])
    sys.exit(0)
mode = words[0]
if mode == os.environ.get("FAIL_MODE"):
    print("RERANK CONTRACT FAILED: " + mode); sys.exit(1)
fused = [0.9, 0.7, 0.5, 0.3]
def obs(order, scored):
    hits = [{"chunk_id": f"c{i + 1}", "score": fused[i], "doc_id": "d", "page_label": "1",
             "rerank_score": ((i + 1) / 4 if scored else None)} for i in order]
    return {"query": "q", "scope": {"product": "z/OS", "version": "V9R9"}, "query_kind": "nl", "hits": hits}
if mode == "gateway-auth":
    state["score"] += 1; state["rerank_calls"] += 1
elif mode == "discover-scope":
    print(json.dumps({"product": "z/OS", "version": "V9R9"}))
elif mode == "search":
    if state["rerank"] == "on":
        if state["fault"] == "healthy":
            state["score"] += 1
        else:
            state["score"] += 1; state["rerank_calls"] += 1
        print(json.dumps(obs([3, 2, 1, 0], True)))
    else:
        print(json.dumps(obs([0, 1, 2, 3], False)))
else:
    print("PASS: " + mode)
save()
'''


def run_rerank_rehearsal(tmp_path, fail_mode=""):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_RERANK_KUBECTL)
    kubectl.chmod(0o755)
    result = subprocess.run(["sh", str(ROOT / "scripts/ci/rerank_rehearsal.sh"), "ns"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_STATE": str(tmp_path),
             "FAIL_MODE": fail_mode}, capture_output=True, text=True, check=False)
    calls = (tmp_path / "calls").read_text().splitlines()
    return result, calls


def test_rerank_rehearsal_sequence_checks_every_claim_and_restores_the_stand_in(tmp_path):
    result, calls = run_rerank_rehearsal(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RERANK REHEARSAL PASSED" in result.stdout
    faults = [c.rsplit("MOCK_RERANK_FAULT=", 1)[1] for c in calls if "MOCK_RERANK_FAULT=" in c]
    assert faults == ["score_unavailable", "unavailable", "healthy", "healthy"]
    agent_modes = [c.split(" - ", 1)[1].split()[0] for c in calls if "exec -i deploy/rag-agent" in c]
    assert agent_modes == ["ready", "gateway-auth", "discover-scope", "search", "search", "degraded",
                           "recovered", "off", "search"]
    # Rerank is switched off only after every rerank-on observation was taken.
    off = next(i for i, c in enumerate(calls) if "RERANK_ENABLED=false" in c)
    assert next(c for c in calls[off:] if "exec -i deploy/rag-agent" in c).endswith("- off")
    assert not any("sk-" in c for c in calls)


@pytest.mark.parametrize("fail_mode", ["gateway-auth", "degraded", "recovered", "off"])
def test_rerank_rehearsal_fails_on_a_failed_contract_and_still_restores_the_stand_in(tmp_path, fail_mode):
    result, calls = run_rerank_rehearsal(tmp_path, fail_mode)
    assert result.returncode != 0 and "RERANK REHEARSAL PASSED" not in result.stdout
    assert [c for c in calls if "MOCK_RERANK_FAULT=" in c][-1].endswith("MOCK_RERANK_FAULT=healthy")


def test_rerank_rehearsal_fails_when_rerank_off_would_still_call_the_stand_in(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_RERANK_KUBECTL.replace(
        'print(json.dumps(obs([0, 1, 2, 3], False)))',
        'state["score"] += 1; print(json.dumps(obs([0, 1, 2, 3], False)))'))
    kubectl.chmod(0o755)
    result = subprocess.run(["sh", str(ROOT / "scripts/ci/rerank_rehearsal.sh"), "ns"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_STATE": str(tmp_path),
             "FAIL_MODE": ""}, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "RERANK CHECK FAILED: delta: score.search: expected 0 request(s), stand-in saw 1" in result.stdout

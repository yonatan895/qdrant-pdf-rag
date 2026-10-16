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
                        if not line.startswith(("VLLM_BASE_URL=", "EMBED_BASE_URL=", "LLM_BASE_URL=")))
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



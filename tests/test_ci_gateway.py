"""CI gateway wiring: explicit model legs, authenticated routing, failure propagation."""
import json
import os
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


@pytest.mark.parametrize("resource,key", [
    ("agent", "LLM_API_KEY"), ("agent", "EMBED_API_KEY"), ("agent", "RERANK_API_KEY"),
    ("ingest", "EMBED_API_KEY"), ("ingest", "CONTEXT_LLM_API_KEY"),
])
@pytest.mark.parametrize("fault", ["name", "key", "missing", "optional", "plaintext"])
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


@pytest.mark.parametrize("resource,key", [
    ("agent", "LLM_BASE_URL"), ("agent", "EMBED_BASE_URL"), ("agent", "RERANK_BASE_URL"),
    ("ingest", "EMBED_BASE_URL"), ("ingest", "CONTEXT_LLM_BASE_URL"),
])
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


@pytest.mark.parametrize("resource", ["agent", "ingest"])
def test_gateway_check_rejects_placeholders_with_attribution(gateway_rendered, resource):
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


def test_kind_uses_real_gateway_for_both_legs():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    steps = workflow['jobs']['kind-live-rehearsal']['steps']
    env_step = next(s['run'] for s in steps if s.get('name', '').startswith('Operator airgap.env'))
    assert 'VLLM_BASE_URL=https://test-gateway:4000' in env_step
    assert 'EMBED_BASE_URL=https://test-gateway:4000/v1' in env_step
    assert 'LLM_BASE_URL=https://test-gateway:4000/v1' in env_step
    assert 'LLM_MODEL_REASONING=mock-reasoning' in env_step
    assert 'GATEWAY_API_KEY_SECRET=test-gateway-keys' in env_step
    assert 'GATEWAY_CA_CONFIGMAP=test-gateway-ca' in env_step
    assert 'DENSE_DIM=1024' in env_step
    # Issue #391 F1: vllm mode needs an explicit stable revision; the mock
    # gateway alias is mutable and never a weights identity.
    assert 'EMBED_MODEL_REVISION=mock-embed@ci' in env_step
    assert 'RERANK_ENABLED=false' in env_step
    gateway_index = next(i for i, s in enumerate(steps) if 'deploy_test_gateway.sh' in s.get('run', ''))
    pipeline_index = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('airgap-pipeline LIVE'))
    assert gateway_index < pipeline_index
    assert any('--require-reasoning --stream' in s.get('run', '') for s in steps)


def test_shared_gateway_lane_proves_fallback_and_single_key():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    steps = workflow['jobs']['kind-live-rehearsal']['steps']
    assert 'shared-gateway' in workflow['jobs']['kind-live-rehearsal']['strategy']['matrix']['lane']
    env_step = next(s['run'] for s in steps if s.get('name', '').startswith('Operator airgap.env'))
    # Shared lane selects the PR #552 contract: one base URL plus one key.
    assert 'GATEWAY_BASE_URL=https://test-gateway:4000/v1' in env_step
    assert 'GATEWAY_API_KEY_SECRET_KEY=api-key' in env_step
    # Explicit per-operation URLs must stay unset in that branch so
    # common.sh fallback is exercised, not bypassed.
    assert "sed -i -e '/^VLLM_BASE_URL=/d'" in env_step
    assert 'shared-gateway' in env_step
    assertion = next(s['run'] for s in steps
                     if s.get('name', '').startswith('Assert shared-gateway rendering'))
    assert '--shared-url https://test-gateway:4000/v1' in assertion
    assert '--secret-name test-gateway-keys --secret-key api-key' in assertion
    assert 'scripts/ci/check_gateway_rendering.py' in assertion
    assert 'dist/agent-rendered.yaml' in assertion
    assert 'dist/ingest-rendered.yaml' in assertion
    assert '--diagnostics "$GITHUB_WORKSPACE/diagnostics/shared-gateway-rendering.json"' in assertion
    assert steps[[s.get('name', '') for s in steps].index(
        'Assert shared-gateway rendering (no explicit URLs, one key)')]['if'] == \
        "matrix.lane == 'shared-gateway'"


def test_gateway_fixture_keeps_local_pins_and_real_routing():
    docs = list(yaml.safe_load_all((ROOT / 'scripts/ci/test-gateway.yaml').read_text()))
    launcher = (ROOT / 'scripts/run_local_gateway.sh').read_text()
    for doc in docs:
        if doc['kind'] == 'Deployment':
            image = doc['spec']['template']['spec']['containers'][0]['image']
            assert image in launcher
            assert '@sha256:' in image
    config = yaml.safe_load(docs[0]['data']['config.yaml'])
    assert {m['model_name'] for m in config['model_list']} == {'mock-reasoning', 'mock-embed'}
    assert all(m['litellm_params']['api_base'] == 'http://vllm-mock:8000/v1' for m in config['model_list'])
    assert config['router_settings']['num_retries'] == 0
    assert config['litellm_settings']['custom_provider_map'] == [
        {'provider': 'strict_openai', 'custom_handler': 'strict_finish.strict_openai'}
    ]
    assert config['model_list'][0]['litellm_params']['model'] == 'strict_openai/mock-reasoning'
    assert config['model_list'][1]['litellm_params']['model'] == 'openai/mock-embed'


@pytest.mark.parametrize('fail_at', ['', 'rollout', 'exec'])
def test_gateway_setup_reaches_keys_only_after_readiness(tmp_path, fail_at):
    log = tmp_path / 'calls.jsonl'
    stub = tmp_path / 'kubectl'
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALL_LOG'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ['FAIL_AT'] and os.environ['FAIL_AT'] in sys.argv:
    sys.exit(1)
if 'exec' in sys.argv:
    print(json.dumps({'llm-api-key': 'sk-test-llm', 'embed-api-key': 'sk-test-embed',
                      'api-key': 'sk-test-shared',
                      'context-llm-api-key': 'sk-test-llm', 'rerank-api-key': 'sk-unused'}))
''')
    stub.chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/deploy_test_gateway.sh'), 'test-gateway-lane'],
                            env={**os.environ, 'PATH': f'{tmp_path}:' + os.environ['PATH'],
                                 'CALL_LOG': str(log), 'FAIL_AT': fail_at},
                            capture_output=True, text=True, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode == 0) == (not fail_at)
    key_creation = [c for c in calls if 'test-gateway-keys' in c]
    assert bool(key_creation) == (not fail_at)
    assert 'sk-test' not in result.stdout + result.stderr
    hooks = next(c for c in calls if 'test-gateway-hooks' in c)
    hook_arg = next(arg for arg in hooks if arg.startswith('--from-file='))
    assert Path(hook_arg.split('=', 2)[2]).resolve() == ROOT / 'scripts/gateway/strict_finish.py'


def test_gateway_setup_refuses_unknown_arguments():
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/deploy_test_gateway.sh'), 'test', '--skip'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


def test_rehearsal_lanes_share_published_bundle_and_bound_jobs():
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    jobs = workflow['jobs']
    kind = jobs['kind-live-rehearsal']
    assert kind['strategy']['matrix']['lane'] == ['pipeline', 'gateway-faults', 'lifecycle', 'shared-gateway']
    assert kind['strategy']['fail-fast'] is False
    lab = jobs['airgap-rehearsal']
    assert 'airgap-package' in lab['needs']
    assert any('download-artifact@' in s.get('uses', '') for s in lab['steps'])
    assert not any('make airgap-pack' in s.get('run', '') for s in lab['steps'])
    smokes = '\n'.join(s.get('run', '') for s in lab['steps'])
    assert '--query "IEA500I operator message" --expect "IEA500I"' in smokes
    assert '--query "torque the widget screws" --expect "torque"' in smokes
    for job in jobs.values():
        assert job['permissions'] and job['timeout-minutes'] > 0
        assert 'github.run_id' in job['concurrency']['group']
        assert job['env']['SHARE'] == 'false'


@pytest.mark.parametrize('script', ['gateway_faults.sh', 'check_lifecycle.sh'])
def test_acceptance_scripts_refuse_missing_namespace(script):
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci' / script)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


@pytest.mark.parametrize('bad_probe', ['false-success', 'unrelated-failure', 'state-not-ready', 'expected-failure'])
def test_fault_lane_requires_contract_failure_and_recovery(tmp_path, bad_probe):
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
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/gateway_faults.sh'), 'test'],
        env={**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH'], 'FAKE_STATE': str(tmp_path), 'PROBE_MODE': bad_probe},
        capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (bad_probe == 'expected-failure'), result.stderr
    lines = log.read_text().splitlines()
    assert any('MOCK_CHAT_FAULT=healthy MOCK_EMBED_FAULT=healthy MOCK_TTFT_MS=0' in line for line in lines)
    if not result.returncode:
        assert sum('exec deploy/rag-agent' in line for line in lines) >= 17


def test_installer_artifact_corruption_stops_before_installation(tmp_path):
    workflow = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())
    installers = [step['run'] for job in workflow['jobs'].values() for step in job['steps']
                  if step.get('name', '').startswith(('Install checksum-pinned', 'Install pinned kubectl', 'Install pinned kind'))]
    # The retired direct-manifest OpenShift lane duplicated one installer.
    assert len(installers) == 9
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


def test_generated_gateway_certificates_require_the_right_ca_and_hostname(tmp_path):
    script = ROOT / 'scripts/ci/create_test_tls.sh'
    roots = [tmp_path / 'right', tmp_path / 'wrong']
    for root in roots:
        subprocess.run(['sh', str(script), str(root), 'test-gateway'], check=True, capture_output=True)
    root, wrong = roots
    system_roots = Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()
    assert (root / 'ca-bundle.crt').read_bytes() == system_roots + (root / 'ca.crt').read_bytes()
    for authority, hostname, valid in [(root, 'test-gateway', True),
                                      (wrong, 'test-gateway', False),
                                      (root, 'test-gateway-wrong-name', False)]:
        result = subprocess.run(['openssl', 'verify', '-x509_strict', '-purpose', 'sslserver',
            '-verify_hostname', hostname, '-CAfile', str(authority / 'ca.crt'), str(root / 'tls.crt')],
            capture_output=True, text=True, check=False)
        assert (result.returncode == 0) is valid
    assert (root / 'tls.key').stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('args', [[], ['dir'], ['dir', 'bad/name'], ['dir', 'name', '--skip']])
def test_test_certificate_cli_fails_closed(args):
    result = subprocess.run(['sh', str(ROOT / 'scripts/ci/create_test_tls.sh'), *args],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


def test_kind_tls_precedes_validation_and_first_pull():
    steps = yaml.safe_load((ROOT / '.github/workflows/e2e.yml').read_text())['jobs']['kind-live-rehearsal']['steps']
    def index(prefix):
        return next(i for i, step in enumerate(steps) if step.get('name', '').startswith(prefix))
    assert index('Black-box handoff') < index('Start authenticated TLS registry')
    assert index('Create registry pull secret') < index('Deploy mock vLLM')
    assert index('Real test gateway') < index('airgap-validate LIVE')
    registry = steps[index('Start authenticated TLS registry')]['run']
    assert 'registry.mirrors' not in registry
    assert 'REGISTRY_AUTH=htpasswd' in registry
    assert '/etc/containerd/certs.d' in registry
    assert 'tls-verify=false' not in registry and 'skip_verify' not in registry
    env = steps[index('Operator airgap.env')]['run']
    assert 'INSECURE_REGISTRY=true' not in env
    docs = list(yaml.safe_load_all((ROOT / 'scripts/ci/test-gateway.yaml').read_text()))
    pod = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'test-gateway')['spec']['template']['spec']
    container = pod['containers'][0]
    assert '--ssl_certfile_path' in container['args']
    assert '--ssl_keyfile_path' in container['args']
    assert container['readinessProbe']['httpGet']['scheme'] == 'HTTPS'
    assert all('ca.key' not in str(v) for v in pod['volumes'])


def test_gateway_configuration_and_provider_share_one_projected_directory():
    docs = list(yaml.safe_load_all((ROOT / 'scripts/ci/test-gateway.yaml').read_text()))
    deployment = next(d for d in docs if d['kind'] == 'Deployment'
                      and d['metadata']['name'] == 'test-gateway')
    pod = deployment['spec']['template']['spec']
    container = pod['containers'][0]
    config_path = Path(container['args'][container['args'].index('--config') + 1])
    mounts = [m for m in container['volumeMounts']
              if Path(m['mountPath']) == config_path.parent
              or config_path.parent in Path(m['mountPath']).parents]
    assert len(mounts) == 1, 'nested ConfigMap subPath mounts fail before gateway startup'
    mount = mounts[0]
    assert mount['readOnly'] is True and 'subPath' not in mount
    volume = next(v for v in pod['volumes'] if v['name'] == mount['name'])
    sources = volume['projected']['sources']
    assert sources == [{'configMap': {'name': 'test-gateway'}},
                       {'configMap': {'name': 'test-gateway-hooks'}}]
    assert config_path.name in docs[0]['data']
    assert (ROOT / 'scripts/gateway/strict_finish.py').is_file()

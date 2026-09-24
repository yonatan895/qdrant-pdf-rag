"""Doctor tests run without Docker, GPU, product dependencies or real configuration."""
import contextlib
import hashlib
import io
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import agent_doctor as doctor


class DoctorTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = {
            'pyproject.toml': '[project]\nrequires-python=">=3.14"\n',
            'requirements.lock.txt': 'qdrant-client==1.19.0\n',
            'images.txt': 'docker.io/qdrant/qdrant:v1.19.0-unprivileged sha256:example\n',
            'bm25-weights.sha256': 'example', 'charts/qdrant-1.19.0.tgz': 'example',
            '.venv/bin/python': 'fixture', 'airgap.env.example': '# public example',
            'scripts/airgap/common.sh': '# fixture',
            'scripts/tools/task-pin.txt': 'version: v3.53.1\nasset: task_linux_amd64.tar.gz\nsha256: ' + hashlib.sha256(b'archive').hexdigest() + '\nbinary-sha256: ' + hashlib.sha256(b'fixture').hexdigest() + '\n',
            '.tools/bin/task': 'fixture',
            '.tools/cache/task_linux_amd64.tar.gz': 'archive',
            'bin/helm': 'helm-fixture',
            'scripts/tools/helm-pin.txt': 'version: v4.3.0\nbinary-sha256: ' + hashlib.sha256(b'helm-fixture').hexdigest() + '\n',
            'charts/mainframe-rag/Chart.yaml': 'fixture',
            'charts/mainframe-rag/values.schema.json': 'fixture',
        }
        for name, value in files.items():
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        # A small independent lock fixture exercises the same full-profile format.
        entries = {name: {"version": version, "wheel": f"{name.replace('-', '_')}-{version}-py3-none-any.whl",
                          "sha256": "a" * 64}
                   for name, version in {"qdrant-client": "1.19.0", "pytest": "9", "ruff": "1", "mypy": "1"}.items()}
        requirements = "".join(f"{name}=={entry['version']} --hash=sha256:{entry['sha256']}\n"
                               for name, entry in sorted(entries.items()))
        (self.root/'requirements.dev.lock.txt').write_text(requirements)
        (self.root/'requirements.build.lock.txt').write_text('# fixture\n')
        (self.root/'locks').mkdir()
        (self.root/'locks/cp314-linux-x86_64.json').write_text(json.dumps({
            "schema_version": 1, "target": "cp314-gil-linux-x86_64-glibc2.34",
            "resolver": {"name": "pip", "version": "26.2.1"}, "packages": entries,
            "profiles": {"dev": {"requirements": "requirements.dev.lock.txt",
                                  "sha256": hashlib.sha256(requirements.encode()).hexdigest(),
                                  "packages": sorted(entries)}},
        }))
        (self.root/'.tools/bin/task').chmod(0o755)
        self.runtime = {'implementation': 'CPython', 'version': [3, 14, 5],
                        'gil_disabled': False, 'jit_enabled': False,
                        'packages': {'qdrant-client': '1.19.0', 'pytest': '9', 'ruff': '1', 'mypy': '1'}}
        tools_patch = patch.object(doctor.shutil, 'which', side_effect=lambda name: str(self.root/'bin/helm') if name == 'helm' else '/tools/'+name)
        self.runtime_patch = patch.object(doctor, 'inspect_runtime', side_effect=lambda *_: self.runtime)
        inventory_patch = patch.object(doctor, 'inspect_locked_environment', return_value=True)
        self.inventory_probe = inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        self.which = tools_patch.start()
        self.probe = self.runtime_patch.start()
        self.addCleanup(tools_patch.stop)
        self.addCleanup(self.runtime_patch.stop)

    def test_ready_profiles_no_daemon_calls(self):
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('no service or Task calls')):
            for profile in ('unit', 'deploy'):
                findings = doctor.diagnose(self.root, profile)
                self.assertTrue(all(f.status == 'ready' for f in findings), findings)
                self.assertNotIn('make', {f.subject for f in findings})

    def test_task_runner_requires_verified_workspace_binary(self):
        local = self.root/'.tools/bin/task'
        # An unrelated PATH program must never be executed or accepted.
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('never execute Task')):
            self.assertEqual(doctor.inspect_task(self.root).status, 'ready')
            local.write_text('corrupt executable')
            self.assertEqual(doctor.inspect_task(self.root).status, 'missing prerequisite')
            local.unlink()
            self.assertEqual(doctor.inspect_task(self.root).status, 'missing prerequisite')
            (self.root/'scripts/tools/task-pin.txt').unlink()
            self.assertEqual(doctor.inspect_task(self.root).status, 'unable to verify')

    def test_missing_or_tampered_cached_task_archive_is_not_ready(self):
        archive = self.root/'.tools/cache/task_linux_amd64.tar.gz'
        archive.write_bytes(b'tampered')
        self.assertEqual(doctor.inspect_task(self.root).subject, 'Task archive')
        self.assertEqual(doctor.inspect_task(self.root).status, 'missing prerequisite')
        archive.unlink()
        self.assertEqual(doctor.inspect_task(self.root).status, 'missing prerequisite')

    def test_helm_missing_foreign_or_tampered_is_rejected_without_execution(self):
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('never execute Helm')):
            self.assertEqual(doctor.inspect_helm(self.root).status, 'ready')
            (self.root/'bin/helm').write_text('foreign-or-tampered-binary')
            self.assertEqual(doctor.inspect_helm(self.root).status, 'missing prerequisite')
            self.which.side_effect = lambda _: None
            self.assertEqual(doctor.inspect_helm(self.root).status, 'missing prerequisite')

    def test_prepared_ci_interpreter_does_not_require_local_venv(self):
        (self.root/'.venv/bin/python').unlink()
        python = self.root/'ci-python'
        target = self.root/'ci-python-target'
        target.write_text('fixture')
        python.symlink_to(target)
        findings = doctor.diagnose(self.root, python=python)
        self.assertTrue(all(f.status == 'ready' for f in findings), findings)
        self.assertEqual(self.probe.call_args.args[0], python)

    def test_complete_inventory_failure_is_not_ready_despite_matching_named_versions(self):
        self.inventory_probe.return_value = False
        findings = doctor.diagnose(self.root)
        self.assertTrue(any(f.subject == 'complete development inventory' and f.status == 'missing prerequisite'
                            for f in findings))

    def test_missing_tools_and_environment(self):
        self.which.side_effect = lambda name: None if name == 'docker' else str(self.root/'bin/helm') if name == 'helm' else '/tools/'+name
        (self.root/'.venv/bin/python').unlink()
        findings = doctor.diagnose(self.root, 'sim')
        missing = {f.subject for f in findings if f.status == 'missing prerequisite'}
        self.assertEqual(missing, {'docker', 'development environment', 'prepared Qdrant image', 'prepared BM25 cache'})

    def test_incompatible_python_gil_jit_and_package_version(self):
        for field, value in [('version', [3, 13, 9]), ('version', [3, 15, 0]), ('implementation', 'PyPy'),
                             ('gil_disabled', True), ('jit_enabled', True)]:
            with self.subTest(field=field):
                old = self.runtime[field]
                self.runtime[field] = value
                self.assertTrue(any(f.subject == 'development environment' and f.status == 'missing prerequisite'
                                    for f in doctor.diagnose(self.root)))
                self.runtime[field] = old
        self.runtime['packages']['qdrant-client'] = '1.18.0'
        self.assertTrue(any('differs' in f.detail for f in doctor.diagnose(self.root)))

    def test_optional_docker_unavailable_timeout_and_success(self):
        for outcome in [subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired('docker', 5),
                        subprocess.CompletedProcess([], 0)]:
            with self.subTest(outcome=type(outcome)), patch.object(doctor.subprocess, 'run') as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                status = doctor.diagnose(self.root, 'sim', True)[-1].status
                self.assertEqual(status, 'ready' if getattr(outcome, 'returncode', 1) == 0 else 'unable to verify')
                argv = run.call_args.args[0]
                self.assertEqual(argv[1:3], ['--host', 'unix:///var/run/docker.sock'])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_exit_codes_and_redaction(self):
        # Private files are neither parsed nor executed, even if executable-looking.
        secret = 'PRIVATE-SENTINEL-123'
        (self.root/'airgap.env').write_text('TOKEN='+secret+'\n$(touch should-not-exist)')
        (self.root/'.env').write_text('TOKEN='+secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                patch.object(doctor.subprocess, 'run', side_effect=AssertionError('no service or Task calls')):
            self.assertEqual(doctor.main(['--root', str(self.root)]), 0)
            self.probe.side_effect = None
            self.probe.return_value = None
            self.assertEqual(doctor.main(['--root', str(self.root)]), 2)
            self.probe.side_effect = RuntimeError(secret)
            self.assertEqual(doctor.main(['--root', str(self.root)]), 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertFalse((self.root/'should-not-exist').exists())

    def test_actual_probe_protocol_and_redaction(self):
        # Exercise real probe parser; fake only the OS process boundary.
        self.runtime_patch.stop()
        with patch.object(doctor.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps(self.runtime), 'secret')
            self.assertEqual(doctor.inspect_runtime(Path('/fixture/python'), ['pytest']), self.runtime)
            self.assertEqual(run.call_args.args[0][1], '-I')
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            for stdout in ('SECRET invalid JSON', '[]', '{"version":[3,14,0]}'):
                run.return_value = subprocess.CompletedProcess([], 0, stdout, 'SECRET')
                self.assertIsNone(doctor.inspect_runtime(Path('/fixture/python'), []))
            run.side_effect = subprocess.TimeoutExpired('SECRET', 5)
            self.assertIsNone(doctor.inspect_runtime(Path('/fixture/python'), []))

    def test_missing_and_unsupported_config(self):
        (self.root/'images.txt').unlink()
        self.assertTrue(any(f.status == 'unable to verify' for f in doctor.diagnose(self.root)))
        (self.root/'pyproject.toml').write_text('[project]\nrequires-python="~=3.14"')
        self.assertTrue(any('unsupported requirement' in f.detail for f in doctor.diagnose(self.root)))


class HelmPreparationTests(TestCase):
    """Offline installation proves bytes before execution or destination mutation."""

    def setUp(self):
        import shutil
        import tarfile

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root/'scripts/tools'
        scripts.mkdir(parents=True)
        shutil.copy(Path(__file__).parents[1]/'scripts/tools/install-helm.sh', scripts)
        self.marker = self.root/'executed'
        self.tool = self.root/'linux-amd64/helm'
        self.tool.parent.mkdir()
        self.tool.write_text(f'#!/bin/sh\ntouch "{self.marker}"\nprintf "v4.3.0+fixture\\n"\n')
        self.tool.chmod(0o755)
        self.archive = self.root/'helm.tgz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(self.tool, arcname='linux-amd64/helm')
        self.pin = scripts/'helm-pin.txt'
        self.pin.write_text(
            'version: v4.3.0\nsha256: '+hashlib.sha256(self.archive.read_bytes()).hexdigest()+
            '\nbinary-sha256: '+hashlib.sha256(self.tool.read_bytes()).hexdigest()+'\n')
        self.destination = self.root/'tools'
        self.destination.mkdir()
        (self.destination/'helm').write_text('previous approved tool')

    def install(self, archive=None):
        return subprocess.run([
            'sh', str(self.root/'scripts/tools/install-helm.sh'),
            '--archive', str(archive or self.archive), '--bin-dir', str(self.destination),
        ], capture_output=True, text=True, timeout=10, check=False)

    def test_offline_archive_installs_verified_tool(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertEqual((self.destination/'helm').read_bytes(), self.tool.read_bytes())

    def test_missing_or_tampered_archive_preserves_previous_tool(self):
        for archive in (self.root/'absent.tgz', self.archive):
            if archive == self.archive:
                archive.write_bytes(b'tampered')
            result = self.install(archive)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.marker.exists())
            self.assertEqual((self.destination/'helm').read_text(), 'previous approved tool')

    def test_wrong_member_digest_never_executes_or_installs(self):
        self.pin.write_text(self.pin.read_text().replace(
            hashlib.sha256(self.tool.read_bytes()).hexdigest(), '0'*64))
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertEqual((self.destination/'helm').read_text(), 'previous approved tool')


def test_bm25_cache_checks_the_selected_revision_and_exact_bytes(tmp_path, monkeypatch):
    import pytest
    from scripts.fetch_bm25_weights import prepared_bm25_cache

    cache = tmp_path / 'bundles/bm25-weights'
    model = cache / 'models--Qdrant--bm25'
    snapshot = model / 'snapshots/original'
    snapshot.mkdir(parents=True)
    (snapshot / 'weights.txt').write_bytes(b'original synthetic weights')
    (model / 'refs').mkdir()
    reference = model / 'refs/main'
    reference.write_text('original')
    digest = hashlib.sha256(b'original synthetic weights').hexdigest()
    (tmp_path / 'bm25-weights.sha256').write_text(f'{digest}  weights.txt\n')
    monkeypatch.delenv('SIM_BM25_CACHE_DIR', raising=False)
    assert prepared_bm25_cache(tmp_path) == cache
    reference.write_text('original\n')
    with pytest.raises(SystemExit, match='selected revision'):
        prepared_bm25_cache(tmp_path)
    reference.write_text('other')
    with pytest.raises(SystemExit, match='selected revision'):
        prepared_bm25_cache(tmp_path)
    reference.write_text('original')
    (model / 'snapshots/other').mkdir()
    with pytest.raises(SystemExit, match='unambiguous'):
        prepared_bm25_cache(tmp_path)
    (model / 'snapshots/other').rmdir()
    (snapshot / 'weights.txt').write_bytes(b'tampered')
    with pytest.raises(SystemExit):
        prepared_bm25_cache(tmp_path)
    (snapshot / 'weights.txt').write_bytes(b'original synthetic weights')
    monkeypatch.setenv('SIM_BM25_CACHE_DIR', str(tmp_path / 'absent'))
    with pytest.raises(SystemExit):
        prepared_bm25_cache(tmp_path)  # cannot fall back to the healthy default cache
    monkeypatch.setenv('SIM_BM25_CACHE_DIR', '')
    with pytest.raises(ValueError, match='must not be empty'):
        prepared_bm25_cache(tmp_path)


def test_simulator_missing_image_never_starts_or_pulls(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest
    from scripts import qdrant_sim

    (tmp_path / 'images.txt').write_text('docker.io/qdrant/qdrant:v1 sha256:' + 'a' * 64)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0 if command == ['docker', 'info'] else 1, stdout='')
    monkeypatch.setattr(qdrant_sim.shutil, 'which', lambda _: '/bin/docker')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(qdrant_sim.QdrantSimError, match='explicit'):
        qdrant_sim.start_simulator(tmp_path)
    assert calls == [['docker', 'info'], ['docker', 'image', 'inspect',
                      'docker.io/qdrant/qdrant@sha256:' + 'a' * 64]]


def load_tests(loader, tests, pattern):
    # Intentional zero-test discovery for the disposable M0 acceptance trial.
    from unittest import TestSuite
    return TestSuite()

"""Doctor tests run without Docker, GPU, product dependencies or real configuration."""
import contextlib
import hashlib
import io
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest import TestCase
from unittest.mock import patch

from scripts import agent_doctor as doctor

# Fix commands a refusal must name (#1118 C7), written out independently of the
# doctor's constants: a weaker model in the gap copies them literally.
TASK_CONNECTED = 'connected fix: sh scripts/tools/install-task.sh;'
TASK_AIRGAP = 'air-gap fix: sh scripts/tools/install-task.sh --archive /absolute/path/to/bundle/task_linux_amd64.tar.gz'
HELM_CONNECTED = 'connected fix: sh scripts/tools/install-helm.sh;'
HELM_AIRGAP = 'air-gap fix: sh scripts/tools/install-helm.sh --archive /absolute/path/to/helm-v4.3.0-linux-amd64.tar.gz'
HELM_PATH = 'then select it: export PATH="$PWD/.tools/bin:$PATH"'
VENV_CONNECTED = 'connected fix: sh scripts/tools/run-task.sh dev:setup PY=python3.14;'
VENV_AIRGAP = ('air-gap fix: python3.14 scripts/prepare_python.py --venv .venv '
               '--wheelhouse /absolute/path/to/approved/dev-wheels')
RECREATE_CONNECTED = 'connected fix: rm -rf .venv && sh scripts/tools/run-task.sh dev:setup PY=python3.14;'
RECREATE_AIRGAP = ('air-gap fix: rm -rf .venv && python3.14 scripts/prepare_python.py --venv .venv '
                   '--wheelhouse /absolute/path/to/approved/dev-wheels')
PREPARED_CONNECTED = ('connected fix: INTERPRETER scripts/prepare_python.py --python INTERPRETER '
                      '--wheelhouse .tools/python-wheels/dev --connected;')
PREPARED_AIRGAP = ('air-gap fix: INTERPRETER scripts/prepare_python.py --python INTERPRETER '
                   '--wheelhouse /absolute/path/to/approved/dev-wheels')


class DoctorTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        files = {
            'pyproject.toml': '[project]\nrequires-python=">=3.14"\n',
            'requirements.lock.txt': 'qdrant-client==1.19.0\n',
            'images.txt': 'docker.io/qdrant/qdrant:v1.19.2-unprivileged sha256:example\n',
            'bm25-weights.sha256': 'example', 'charts/qdrant-1.19.2.tgz': 'example',
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
            "resolver": {"name": "pip", "version": "26.2.2"}, "packages": entries,
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

    def test_qdrant_pin_allows_patch_skew_but_not_minor_skew(self):
        # Skill rule: server and client agree on major.minor; the chart tracks the server.
        def pin_findings():
            return {f.subject: f.status for f in doctor.diagnose(self.root, 'deploy')}
        self.assertEqual(pin_findings()['Qdrant pin'], 'ready')
        self.assertEqual(pin_findings()['Qdrant chart'], 'ready')
        (self.root/'images.txt').write_text('docker.io/qdrant/qdrant:v1.20.0-unprivileged sha256:example\n')
        self.assertEqual(pin_findings()['Qdrant pin'], 'missing prerequisite')
        self.assertEqual(pin_findings()['Qdrant chart'], 'missing prerequisite')

    def test_server_patch_without_its_chart_fails_on_chart(self):
        # Server 1.19.2 with only the 1.19.0 chart (same minor): pin ok, chart mismatch.
        (self.root/'charts/qdrant-1.19.2.tgz').rename(self.root/'charts/qdrant-1.19.0.tgz')
        found = {f.subject: f.status for f in doctor.diagnose(self.root, 'deploy')}
        self.assertEqual(found['Qdrant pin'], 'ready')
        self.assertEqual(found['Qdrant chart'], 'missing prerequisite')

    def test_deploy_profile_requires_its_inputs_and_a_cluster_cli(self):
        with patch.object(doctor.subprocess, 'run', side_effect=AssertionError('no service or Task calls')):
            findings = doctor.diagnose(self.root, 'deploy')
            self.assertTrue(all(f.status == 'ready' for f in findings), findings)
            helm = str(self.root/'bin/helm')
            self.which.side_effect = lambda name: helm if name == 'helm' else None if name in ('oc', 'kubectl') else '/tools/'+name
            (self.root/'airgap.env.example').unlink()
            missing = {f.subject for f in doctor.diagnose(self.root, 'deploy') if f.status == 'missing prerequisite'}
        self.assertEqual(missing, {'oc or kubectl', 'airgap.env.example'})

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

    # Refusal fix commands (#1118 C7): every non-ready finding names its fix.
    def finding(self, subject, *args, **kwargs):
        found = [f for f in doctor.diagnose(self.root, *args, **kwargs)
                 if f.subject == subject and f.status != 'ready']
        self.assertEqual(len(found), 1, found)
        return found[0].detail

    def assertFix(self, detail, *commands):
        for command in commands:
            self.assertIn(command, detail)

    def test_ready_findings_name_no_fix_and_refusals_point_at_the_checkout_root(self):
        findings = doctor.diagnose(self.root, 'deploy')
        self.assertTrue(all(f.status == 'ready' and 'fix:' not in f.detail for f in findings), findings)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(doctor.main(['--root', str(self.root)]), 0)
        self.assertNotIn('Run each fix', output.getvalue())
        (self.root/'.tools/bin/task').unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(doctor.main(['--root', str(self.root)]), 2)
        self.assertIn('Run each fix from the checkout root, then rerun this check.', output.getvalue())

    def test_task_refusals_name_the_installer_for_both_hosts(self):
        local = self.root/'.tools/bin/task'
        local.write_text('corrupt executable')
        self.assertFix(doctor.inspect_task(self.root).detail, TASK_CONNECTED, TASK_AIRGAP)
        local.unlink()
        detail = doctor.inspect_task(self.root).detail
        self.assertTrue(detail.startswith('pinned .tools/bin/task absent; '), detail)
        self.assertFix(detail, TASK_CONNECTED, TASK_AIRGAP)
        (self.root/'scripts/tools/task-pin.txt').unlink()
        self.assertFix(doctor.inspect_task(self.root).detail, 'fix: git checkout -- scripts/tools/task-pin.txt')

    def test_task_executable_unreadable_names_the_installer(self):
        with patch.object(doctor.hashlib, 'file_digest', side_effect=PermissionError('SECRET')):
            finding = doctor.inspect_task(self.root)
        self.assertEqual(finding.status, 'unable to verify')
        self.assertFix(finding.detail, TASK_CONNECTED, TASK_AIRGAP)
        self.assertNotIn('SECRET', finding.detail)

    def test_task_archive_refusals_name_the_installer_that_caches_it(self):
        archive = self.root/'.tools/cache/task_linux_amd64.tar.gz'
        archive.write_bytes(b'tampered')
        self.assertFix(doctor.inspect_task(self.root).detail, TASK_CONNECTED, TASK_AIRGAP)
        archive.unlink()
        self.assertFix(doctor.inspect_task(self.root).detail, TASK_CONNECTED, TASK_AIRGAP)

    def test_helm_refusals_name_the_pinned_installer_and_path_selection(self):
        (self.root/'bin/helm').write_text('foreign-or-tampered-binary')
        self.assertFix(doctor.inspect_helm(self.root).detail, HELM_CONNECTED, HELM_AIRGAP, HELM_PATH)
        self.which.side_effect = lambda _: None
        self.assertFix(doctor.inspect_helm(self.root).detail, HELM_CONNECTED, HELM_AIRGAP, HELM_PATH)
        (self.root/'scripts/tools/helm-pin.txt').unlink()
        self.assertFix(doctor.inspect_helm(self.root).detail, 'fix: git checkout -- scripts/tools/helm-pin.txt')

    def test_helm_unreadable_names_the_installer(self):
        with patch.object(doctor.hashlib, 'file_digest', side_effect=PermissionError('SECRET')):
            detail = doctor.inspect_helm(self.root).detail
        self.assertFix(detail, HELM_CONNECTED, HELM_AIRGAP, HELM_PATH)
        self.assertNotIn('SECRET', detail)

    def test_tracked_input_refusals_restore_the_tracked_file(self):
        (self.root/'airgap.env.example').unlink()
        self.assertFix(self.finding('airgap.env.example', 'deploy'), 'fix: git checkout -- airgap.env.example')
        (self.root/'images.txt').write_text('docker.io/qdrant/qdrant:v1.20.0-unprivileged sha256:example\n')
        self.assertFix(self.finding('Qdrant pin'),
                       'fix: git checkout -- images.txt requirements.dev.lock.txt locks/cp314-linux-x86_64.json')
        self.assertFix(self.finding('Qdrant chart'), 'fix: git checkout -- charts/qdrant-1.20.0.tgz')
        with patch.object(doctor, 'load_dependency_lock', return_value=(None, {})):
            self.assertFix(self.finding('requirements.lock.txt'),
                           'fix: git checkout -- requirements.dev.lock.txt locks/cp314-linux-x86_64.json')
        (self.root/'images.txt').unlink()
        self.assertFix(self.finding('tracked configuration'), 'fix: git checkout -- pyproject.toml images.txt '
                       'requirements.dev.lock.txt locks/cp314-linux-x86_64.json')
        (self.root/'pyproject.toml').write_text('[project]\nrequires-python="~=3.14"')
        self.assertFix(self.finding('Python requirement'), 'requires-python must read >=MAJOR.MINOR',
                       'fix: git checkout -- pyproject.toml')

    def test_missing_cli_refusals_name_the_check_command(self):
        self.which.side_effect = lambda name: (str(self.root/'bin/helm') if name == 'helm'
                                               else None if name in ('oc', 'kubectl', 'skopeo') else '/tools/'+name)
        self.assertFix(self.finding('oc or kubectl', 'deploy'), 'then check: command -v oc')
        self.assertFix(self.finding('skopeo', 'deploy'),
                       'fix: no repository installer; install skopeo with the host package manager, '
                       'then check: command -v skopeo')

    def test_local_venv_refusals_name_setup_for_both_hosts(self):
        # Task targets select the venv as --python .venv/bin/python: same fix.
        for python in (None, self.root/'.venv/bin/python'):
            with self.subTest(python=python):
                self.runtime['version'] = [3, 13, 9]
                self.assertFix(self.finding('development environment', python=python),
                               RECREATE_CONNECTED, RECREATE_AIRGAP)
                self.runtime['version'] = [3, 14, 5]
                self.inventory_probe.return_value = False
                self.assertFix(self.finding('complete development inventory', python=python),
                               RECREATE_CONNECTED, RECREATE_AIRGAP)
                self.inventory_probe.return_value = True
                self.runtime['packages']['qdrant-client'] = '1.18.0'
                self.assertFix(self.finding('qdrant-client', python=python), RECREATE_CONNECTED, RECREATE_AIRGAP)
                del self.runtime['packages']['qdrant-client']
                self.assertFix(self.finding('qdrant-client', python=python), RECREATE_CONNECTED, RECREATE_AIRGAP)
                self.runtime['packages']['qdrant-client'] = '1.19.0'
        self.probe.side_effect = lambda python, _: None if python == self.root/'.venv/bin/python' else self.runtime
        self.assertFix(self.finding('development environment'), RECREATE_CONNECTED, RECREATE_AIRGAP)
        self.probe.side_effect = lambda *_: self.runtime
        (self.root/'.venv/bin/python').unlink()
        detail = self.finding('development environment')
        self.assertFix(detail, VENV_CONNECTED, VENV_AIRGAP)
        self.assertNotIn('rm -rf', detail)

    def test_prepared_interpreter_refusals_name_prepare_python(self):
        python = self.root/'ci-python'
        detail = self.finding('development environment', python=python)
        self.assertFix(detail, 'fix: point --python at an existing interpreter, or omit --python')
        self.assertNotIn('rm -rf', detail)
        python.write_text('fixture')
        self.inventory_probe.return_value = False
        self.assertFix(self.finding('complete development inventory', python=python),
                       PREPARED_CONNECTED, PREPARED_AIRGAP)

    def test_interpreter_outside_the_checkout_never_gets_a_local_venv_fix(self):
        # Review B2 (#1139): a path ending in .venv/bin/python is not this
        # checkout's venv unless the directory holding .venv is this checkout.
        other = TemporaryDirectory()
        self.addCleanup(other.cleanup)
        foreign = Path(other.name)/'other-checkout/.venv/bin/python'
        detail = self.finding('development environment', python=foreign)
        self.assertFix(detail, 'fix: point --python at an existing interpreter, or omit --python')
        foreign.parent.mkdir(parents=True)
        foreign.write_text('fixture')
        self.runtime['version'] = [3, 13, 9]
        self.inventory_probe.return_value = False
        for subject in ('development environment', 'complete development inventory'):
            with self.subTest(case='another checkout venv', subject=subject):
                detail = self.finding(subject, python=foreign)
                self.assertFix(detail, "fix: that is another checkout's .venv", 'omit --python')
                self.assertNotIn('rm -rf', detail)
                self.assertNotIn('--python INTERPRETER', detail)
        # A wrong-version interpreter outside the checkout needs a different
        # interpreter: preparing it cannot change its version.
        outside = Path(other.name)/'python3.13'
        outside.write_text('fixture')
        detail = self.finding('development environment', python=outside)
        self.assertFix(detail, 'fix: give --python a CPython 3.14 GIL interpreter (python3.14, not python3.14t)')
        self.assertNotIn('rm -rf', detail)
        self.assertNotIn('prepare_python.py', detail)
        self.runtime['version'] = [3, 14, 5]
        self.assertFix(self.finding('complete development inventory', python=outside),
                       PREPARED_CONNECTED, PREPARED_AIRGAP)

    def test_symlinked_venv_or_checkout_stays_local(self):
        self.runtime['version'] = [3, 13, 9]
        link = Path(self.temp.name + '-link')
        link.symlink_to(self.root)
        self.addCleanup(link.unlink)
        target = self.root/'shared-venv'
        (self.root/'.venv').rename(target)
        (self.root/'.venv').symlink_to(target)
        for python in (self.root/'.venv/bin/python', link/'.venv/bin/python'):
            with self.subTest(python=python):
                self.assertFix(self.finding('development environment', python=python),
                               RECREATE_CONNECTED, RECREATE_AIRGAP)

    def test_checker_runtime_refusal_names_a_gil_interpreter(self):
        self.probe.side_effect = lambda python, _: (dict(self.runtime, jit_enabled=True)
                                                   if python == Path(doctor.sys.executable) else self.runtime)
        self.assertFix(self.finding('checker runtime'),
                       'fix: env -u PYTHON_JIT python3.14 scripts/agent_doctor.py')

    def test_simulation_preparation_refusals_name_their_tasks(self):
        self.which.side_effect = lambda name: str(self.root/'bin/helm') if name == 'helm' else '/tools/'+name
        self.assertFix(self.finding('prepared Qdrant image', 'sim'),
                       'connected fix: sh scripts/tools/run-task.sh artifacts:qdrant;',
                       'air gap: no supported preparation')
        self.assertFix(self.finding('prepared BM25 cache', 'sim'),
                       'connected fix: sh scripts/tools/run-task.sh artifacts:bm25',
                       'air-gap fix: export SIM_BM25_CACHE_DIR=/absolute/path/to/offline-prepared/bm25-weights',
                       'python3 scripts/fetch_bm25_weights.py --out "$SIM_BM25_CACHE_DIR" '
                       '--verify-manifest bm25-weights.sha256 --verify-only')

    def test_docker_probe_refusals_name_the_check_command(self):
        with patch.object(doctor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            self.assertFix(self.finding('local Docker probe', 'unit', True),
                           'then check: docker --host unix:///var/run/docker.sock version')
        with patch.object(doctor.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 5)):
            self.assertFix(self.finding('local Docker probe', 'unit', True),
                           'then check: docker --host unix:///var/run/docker.sock version')
        self.which.side_effect = lambda name: str(self.root/'bin/helm') if name == 'helm' else None if name == 'docker' else '/tools/'+name
        self.assertFix(self.finding('local Docker probe', 'unit', True), 'then check: command -v docker')


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



CONNECTED_FETCH = 'curl -fsSL --retry 3 --retry-all-errors -o "$ARCHIVE"'


class ConnectedPreparationRetryTests(TestCase):
    """Connected installs retry transport failures only; a checksum mismatch is fatal (#1118).

    Real curl against a loopback server: the first responses fail, then the
    pinned bytes or tampered bytes are served.
    """

    TOOLS: ClassVar[dict[str, tuple[str, str, str]]] = {'helm': ('linux-amd64/helm', 'printf "v4.3.0+fixture\\n"', 'v4.3.0'),
             'oras': ('oras', 'printf "Version: 1.3.4\\n"', 'v1.3.4')}

    def setUp(self):
        import shutil
        import tarfile
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = []
        self.failures = 0
        self.body = b''
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if len(test.requests) <= test.failures:
                    self.send_response(503)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(test.body)))
                self.end_headers()
                self.wfile.write(test.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.archives = {}
        scripts = self.root / 'scripts/tools'
        scripts.mkdir(parents=True)
        for name, (member, version_output, version) in self.TOOLS.items():
            shutil.copy(Path(__file__).parents[1] / f'scripts/tools/install-{name}.sh', scripts)
            tool = self.root / 'build' / name / member
            tool.parent.mkdir(parents=True)
            tool.write_text(f'#!/bin/sh\n{version_output}\n')
            tool.chmod(0o755)
            archive = self.root / 'build' / f'{name}.tgz'
            with tarfile.open(archive, 'w:gz') as packed:
                packed.add(tool, arcname=member)
            self.archives[name] = archive.read_bytes()
            (scripts / f'{name}-pin.txt').write_text(
                f'version: {version}\nsha256: {hashlib.sha256(self.archives[name]).hexdigest()}\n'
                f'binary-sha256: {hashlib.sha256(tool.read_bytes()).hexdigest()}\n'
                f'origin: http://127.0.0.1:{self.server.server_port}/{name}.tgz\n')

    def install(self, name):
        destination = self.root / 'tools' / name
        result = subprocess.run(['sh', str(self.root / f'scripts/tools/install-{name}.sh'),
                                 '--bin-dir', str(destination)],
                                capture_output=True, text=True, timeout=60, check=False)
        return result, destination / name

    def test_transient_failures_are_retried_then_the_pinned_bytes_install(self):
        for name in self.TOOLS:
            with self.subTest(tool=name):
                self.requests.clear()
                self.failures, self.body = 2, self.archives[name]
                result, installed = self.install(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.requests), 3)
                self.assertTrue(installed.is_file())

    def test_retries_are_bounded(self):
        self.failures, self.body = 10, self.archives['helm']
        result, installed = self.install('helm')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests), 4)  # one attempt plus three retries
        self.assertFalse(installed.exists())

    def test_checksum_mismatch_is_fatal_and_never_retried(self):
        for name in self.TOOLS:
            with self.subTest(tool=name):
                self.requests.clear()
                self.failures, self.body = 1, b'tampered bytes'
                result, installed = self.install(name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('archive checksum mismatch', result.stderr)
                self.assertEqual(len(self.requests), 2)  # the transient failure, then one transfer
                self.assertFalse(installed.exists())

    def test_task_installer_uses_the_same_fetch_and_refuses_tampered_bytes_once(self):
        import shutil

        repo = Path(__file__).parents[1]
        for name in ('install-task.sh', 'install-helm.sh', 'install-oras.sh'):
            self.assertEqual((repo / 'scripts/tools' / name).read_text().count('curl -fsSL'), 1, name)
            self.assertIn(CONNECTED_FETCH, (repo / 'scripts/tools' / name).read_text(), name)
        workspace = self.root / 'task-workspace'
        (workspace / 'scripts/tools').mkdir(parents=True)
        for name in ('install-task.sh', 'task-artifact.sh', 'task-pin.txt'):
            shutil.copy(repo / 'scripts/tools' / name, workspace / 'scripts/tools')
        stub = self.root / 'stub-bin'
        stub.mkdir()
        log = self.root / 'curl.log'
        # Records every invocation and delivers tampered bytes to the -o path.
        (stub / 'curl').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_LOG"\n'
                                   'while [ "$1" != -o ]; do shift; done\nprintf tampered > "$2"\n')
        (stub / 'curl').chmod(0o755)
        result = subprocess.run(['sh', str(workspace / 'scripts/tools/install-task.sh'),
                                 '--bin-dir', str(self.root / 'task-bin')],
                                env={'PATH': f'{stub}:/usr/bin:/bin', 'CURL_LOG': str(log)},
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Task archive checksum mismatch', result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('-fsSL --retry 3 --retry-all-errors -o '), calls[0])
        self.assertFalse((self.root / 'task-bin/task').exists())


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

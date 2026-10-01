"""Hash-bound profiles, offline acquisition boundaries and installed inventory."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from scripts import dependency_lock as locks
from scripts import prepare_python


def _report(items):
    return {
        "version": "1", "pip_version": "26.2.1",
        "environment": {"python_version": "3.14", "platform_python_implementation": "CPython",
                        "sys_platform": "linux", "platform_machine": "x86_64"},
        "install": items,
    }


@pytest.fixture
def locked(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    items = {}
    for name, version in (("example", "1.0"), ("pip", "26.2.1")):
        wheel = f"{name}-{version}-py3-none-any.whl"
        content = f"original synthetic wheel stand-in: {name}".encode()
        (wheels / wheel).write_bytes(content)
        items[name] = {"metadata": {"name": name, "version": version},
                       "download_info": {"url": "https://files.pythonhosted.org/packages/" + wheel,
                                         "archive_info": {"hashes": {"sha256": hashlib.sha256(content).hexdigest()}}}}
    for profile, names in (("runtime", ("example",)), ("dev", ("example", "pip")), ("build", ("pip",))):
        (reports / f"{profile}.json").write_text(json.dumps(_report([items[n] for n in names])))
    locks.generate(tmp_path, reports)
    return tmp_path, reports, wheels


@pytest.mark.parametrize("wheel,compatible", [
    ("pkg-1.0-py3-none-any.whl", True),
    ("pkg-1.0-py3-none-manylinux_2_17_x86_64.whl", True),
    ("pkg-1.0-cp310-abi3-manylinux_2_28_x86_64.whl", True),
    ("pkg-1.0-cp314-cp314-manylinux_2_34_x86_64.whl", True),
    ("pkg-1.0-cp314t-cp314t-manylinux_2_28_x86_64.whl", False),
    ("pkg-1.0-cp315-cp315-manylinux_2_28_x86_64.whl", False),
    ("pkg-1.0-cp314-cp314-manylinux_2_35_x86_64.whl", False),
    ("pkg-1.0-cp314-cp314-manylinux_2_28_aarch64.whl", False),
    ("pkg-1.0.tar.gz", False),
    ("../pkg-1.0-py3-none-any.whl", False),
])
def test_supported_wheel_target(wheel, compatible):
    assert locks.compatible_wheel(wheel) is compatible


def test_lockfile_and_manifest_must_agree(locked):
    root, _, _ = locked
    _, packages = locks.load(root, "dev")
    assert {name: entry["version"] for name, entry in packages.items()} == {"example": "1.0", "pip": "26.2.1"}
    requirement = root / "requirements.dev.lock.txt"
    requirement.write_text(requirement.read_text().replace("example==1.0", "example==2.0"))
    with pytest.raises(locks.LockError, match="disagree"):
        locks.load(root, "dev")


def test_wheelhouse_rejects_missing_extra_tampered_and_symlink(locked):
    root, _, wheels = locked
    receipt = locks.verify_wheelhouse(root, "dev", wheels)
    assert set(receipt["wheels"]) == {"example-1.0-py3-none-any.whl", "pip-26.2.1-py3-none-any.whl"}
    target = wheels / "example-1.0-py3-none-any.whl"
    original = target.read_bytes()
    target.unlink()
    with pytest.raises(locks.LockError, match="missing"):
        locks.verify_wheelhouse(root, "dev", wheels)
    target.write_bytes(original + b"tampered")
    with pytest.raises(locks.LockError, match="checksum"):
        locks.verify_wheelhouse(root, "dev", wheels)
    target.write_bytes(original)
    extra = wheels / "unselected-1.0-py3-none-any.whl"
    extra.write_bytes(b"extra")
    with pytest.raises(locks.LockError, match="unselected"):
        locks.verify_wheelhouse(root, "dev", wheels)
    extra.unlink()
    saved = root / "outside-wheel"
    target.rename(saved)
    target.symlink_to(saved)
    with pytest.raises(locks.LockError, match="symlink"):
        locks.verify_wheelhouse(root, "dev", wheels)


def test_offline_preparation_fails_before_any_mutation_or_process(locked, monkeypatch):
    root, _, wheels = locked
    (wheels / "example-1.0-py3-none-any.whl").write_bytes(b"corrupt")
    monkeypatch.setattr(prepare_python.subprocess, "run", lambda *a, **kw: pytest.fail("process started"))
    monkeypatch.setattr(locks.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network used"))
    environment = root / "new-venv"
    with pytest.raises(locks.LockError, match="checksum"):
        prepare_python.prepare(root, wheels, None, environment, False)
    assert not environment.exists()


def test_report_cannot_change_resolver_or_target(locked):
    root, reports, _ = locked
    path = reports / "runtime.json"
    original = json.loads(path.read_text())
    for field, value in (("pip_version", "0.1"), ("version", "99")):
        changed = {**original, field: value}
        path.write_text(json.dumps(changed))
        with pytest.raises(locks.LockError, match="qualified"):
            locks.generate(root, reports)
    changed = {**original, "environment": {**original["environment"], "python_version": "3.15"}}
    path.write_text(json.dumps(changed))
    with pytest.raises(locks.LockError, match="qualified"):
        locks.generate(root, reports)


def _distribution(name, version, location):
    return SimpleNamespace(metadata={"Name": name}, version=version, locate_file=lambda _: location)


def test_installed_only_or_wrong_version_dependency_fails(locked, monkeypatch):
    root, _, _ = locked
    correct = _distribution("example", "1.0", root)
    monkeypatch.setattr(locks.importlib.metadata, "distributions", lambda: [correct])
    assert locks.verify_installed(root, "runtime")["packages"] == {"example": "1.0"}
    for distributions in ([correct, _distribution("injected", "1.0", root)],
                          [_distribution("example", "2.0", root)], [],
                          [correct, _distribution("example", "1.0", root / "other")]):
        monkeypatch.setattr(locks.importlib.metadata, "distributions", lambda d=distributions: d)
        with pytest.raises(locks.LockError):
            locks.verify_installed(root, "runtime")


def test_connected_cache_hit_does_not_download_and_bad_fetch_cannot_replace(locked, monkeypatch):
    import io

    root, _, wheels = locked
    monkeypatch.setattr(locks.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected network"))
    locks.acquire(root, "dev", wheels)
    target = wheels / "example-1.0-py3-none-any.whl"
    target.write_bytes(b"retained previous bytes")
    monkeypatch.setattr(locks.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad download"))
    with pytest.raises(locks.LockError, match="downloaded wheel"):
        locks.acquire(root, "dev", wheels)
    assert target.read_bytes() == b"retained previous bytes"
    assert not list(wheels.glob(".wheel-*"))


def test_parallel_acquisition_overlaps_and_verifies_each_download_before_reuse(locked, monkeypatch, capsys):
    import io
    import threading
    from pathlib import Path
    from urllib.parse import urlsplit

    root, _, wheels = locked
    contents = {path.name: path.read_bytes() for path in wheels.glob('*.whl')}
    for path in wheels.glob('*.whl'):
        path.unlink()
    barrier = threading.Barrier(2)
    calls = []
    def fetch(url, *, timeout):
        calls.append((url, timeout))
        barrier.wait(timeout=5)
        return io.BytesIO(contents[Path(urlsplit(url).path).name])
    monkeypatch.setattr(locks.urllib.request, 'urlopen', fetch)
    result = locks.prepare(root, 'dev', wheels, workers=2)
    assert len(calls) == 2 and all(timeout == 60 for _, timeout in calls)
    assert {path.name: path.read_bytes() for path in wheels.glob('*.whl')} == contents
    assert locks.verify_wheelhouse(root, 'dev', wheels) == result
    assert not list(root.glob('.wheels.prepare-*'))
    assert not list(wheels.glob('.wheel-*'))
    assert 'Verified wheel:' in capsys.readouterr().err
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *arguments, **keywords: pytest.fail('cache hit downloaded'))
    assert locks.prepare(root, 'dev', wheels, workers=2) == result


@pytest.mark.parametrize('failure', ['checksum', 'transport'])
def test_parallel_failure_drains_workers_preserves_cache_and_next_prepare_recovers(locked, monkeypatch, failure):
    import io
    import threading
    from pathlib import Path
    from urllib.parse import urlsplit

    root, _, wheels = locked
    contents = {path.name: path.read_bytes() for path in wheels.glob('*.whl')}
    for path in wheels.glob('*.whl'):
        path.write_bytes(b'retained previous bytes')
    before = {path.name: path.read_bytes() for path in wheels.iterdir()}
    barrier = threading.Barrier(2)
    finished = threading.Event()
    class Stream(io.BytesIO):
        def close(self):
            super().close()
            finished.set()
    def fetch(url, *, timeout):
        name = Path(urlsplit(url).path).name
        barrier.wait(timeout=5)
        if name.startswith('example-'):
            if failure == 'transport':
                raise OSError('synthetic controlled transport failure')
            return io.BytesIO(b'incorrect bytes')
        return Stream(contents[name])
    monkeypatch.setattr(locks.urllib.request, 'urlopen', fetch)
    with pytest.raises((locks.LockError, OSError)):
        locks.prepare(root, 'dev', wheels, workers=2)
    assert finished.is_set()
    assert {path.name: path.read_bytes() for path in wheels.iterdir()} == before
    assert not (wheels / '.task-complete').exists()
    assert not list(root.glob('.wheels.prepare-*'))
    monkeypatch.setattr(locks.urllib.request, 'urlopen',
                        lambda url, **kw: io.BytesIO(contents[Path(urlsplit(url).path).name]))
    locks.prepare(root, 'dev', wheels, workers=2)
    assert {path.name: path.read_bytes() for path in wheels.glob('*.whl')} == contents
    assert json.loads((wheels / '.task-complete').read_text())['profile'] == 'dev'
    assert locks.verify_wheelhouse(root, 'dev', wheels)['profile'] == 'dev'


@pytest.mark.parametrize('workers', [0, -1, 17, True, '4'])
def test_invalid_download_parallelism_fails_before_acquisition_or_environment_mutation(locked, monkeypatch, workers):
    root, _, wheels = locked
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *arguments, **keywords: pytest.fail('network used'))
    monkeypatch.setattr(prepare_python.subprocess, 'run', lambda *arguments, **keywords: pytest.fail('process started'))
    for operation in (locks.acquire, locks.prepare):
        with pytest.raises(locks.LockError, match='workers'):
            operation(root, 'dev', wheels, workers=workers)
    with pytest.raises(locks.LockError, match='workers'):
        prepare_python.prepare(root, wheels, None, root / 'new-env', True, download_workers=workers)
    assert not (root / 'new-env').exists()


def test_parallel_preparation_is_explicitly_connected_only(locked, monkeypatch):
    root, _, wheels = locked
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *arguments, **keywords: pytest.fail('network used'))
    monkeypatch.setattr(prepare_python.subprocess, 'run', lambda *arguments, **keywords: pytest.fail('process started'))
    with pytest.raises(locks.LockError, match='explicit connected'):
        prepare_python.prepare(root, wheels, None, root / 'new-env', False, download_workers=2)
    assert prepare_python.main(['--root', str(root), '--wheelhouse', str(wheels),
                                '--venv', str(root / 'new-env'), '--internal-index',
                                '--download-workers', '2']) == 2
    assert not (root / 'new-env').exists()


def test_internal_index_has_no_inherited_extra_index_or_public_fallback(locked, monkeypatch):
    root, _, wheels = locked
    monkeypatch.setattr(prepare_python.importlib.metadata, 'version', lambda _: '26.2.1')
    monkeypatch.setenv('PIP_EXTRA_INDEX_URL', 'https://pypi.org/simple')
    monkeypatch.setenv('PIP_TRUSTED_HOST', 'untrusted.invalid')
    calls = []
    monkeypatch.setattr(prepare_python.subprocess, 'run', lambda *a, **kw: calls.append((a, kw)))
    for value in ('', 'https://pypi.org/simple', 'http://mirror.invalid/simple'):
        monkeypatch.setenv('PIP_INDEX_URL', value)
        with pytest.raises(locks.LockError, match='internal HTTPS'):
            prepare_python.acquire_internal(root, wheels)
    assert calls == []
    monkeypatch.setenv('PIP_INDEX_URL', 'https://mirror.invalid/simple')
    prepare_python.acquire_internal(root, wheels)
    argv = calls[0][0][0]
    assert argv[argv.index('--index-url') + 1] == 'https://mirror.invalid/simple'
    assert '--isolated' in argv and '--require-hashes' in argv and '--only-binary=:all:' in argv
    assert calls[0][1]['capture_output'] is True
    assert {k: v for k, v in calls[0][1]['env'].items() if k.startswith('PIP_')} == {'PIP_CONFIG_FILE': '/dev/null'}


def test_preparation_uses_verified_pip_and_no_isolated_build_resolution(locked, monkeypatch):
    root, _, wheels = locked
    python = root / 'python'
    python.touch()
    calls = []
    monkeypatch.setattr(prepare_python.subprocess, 'run', lambda *a, **kw: calls.append((a[0], kw)))
    prepare_python.prepare(root, wheels, python, None, False)
    install = calls[1][0]
    assert str(wheels / 'pip-26.2.1-py3-none-any.whl') in install
    assert '--require-hashes' in install and '--no-index' in install
    project = calls[2][0]
    assert project[-2:] == ['-e', str(root)]
    assert {'--no-deps', '--no-build-isolation', '--no-index'} <= set(project)
    assert calls[-1][0][-3:] == ['--profile', 'dev', '--project']


@pytest.mark.parametrize("compressed", [False, True])
def test_actual_image_layers_and_receipt_reconcile(locked, compressed):
    from scripts.image_inventory import inventory

    from tests.helpers_image_inventory import image_files, write_image

    root, _, _ = locked
    archive = root / 'image.tar'
    files = image_files(root)
    write_image(archive, [files], compressed=compressed)
    assert inventory(root, archive)['packages'] == {'example': '1.0', 'pip': '24.2'}
    # Same-name overwrite must win over the lower layer's valid metadata.
    metadata = next(p for p in files if '/example-' in p)
    write_image(archive, [files, {metadata: b'Name: example\nVersion: 9.0\n'}])
    with pytest.raises(locks.LockError, match='actual installed'):
        inventory(root, archive)
    # Replacing the metadata file with a directory cannot preserve the old
    # file's valid inventory as though it remained readable.
    write_image(archive, [files, {metadata: None}])
    with pytest.raises(locks.LockError, match='bounded regular file'):
        inventory(root, archive)
    # Ordinary and opaque whiteouts remove lower-layer metadata; same-layer
    # replacement survives an opaque whiteout regardless of tar member order.
    directory = metadata.rsplit('/', 1)[0]
    for whiteout in (directory + '/.wh.METADATA', directory + '/.wh..wh..opq'):
        write_image(archive, [files, {whiteout: b''}])
        with pytest.raises(locks.LockError, match='actual installed'):
            inventory(root, archive)
        write_image(archive, [files, {metadata: files[metadata], whiteout: b''}])
        assert inventory(root, archive)['packages']['example'] == '1.0'
    # An injected package that is subsequently removed must not persist in
    # the inventory. This verifies final filesystem state, not all layers' union.
    extra_dir = 'opt/app-root/lib/python3.14/site-packages/injected-1.0.dist-info'
    write_image(archive, [files, {extra_dir + '/METADATA': b'Name: injected\nVersion: 1.0\n'},
                          {extra_dir.rsplit('/', 1)[0] + '/.wh.injected-1.0.dist-info': b''}])
    assert inventory(root, archive)['packages'] == {'example': '1.0', 'pip': '24.2'}


def test_image_layer_tamper_is_detected_even_when_metadata_stays_valid(locked):
    import tarfile

    from scripts.image_inventory import inventory

    from tests.helpers_image_inventory import image_files, tar_bytes, write_image

    root, _, _ = locked
    archive = root / 'image.tar'
    write_image(archive, [image_files(root)])
    with tarfile.open(archive) as source:
        members = {item.name: source.extractfile(item).read() for item in source if item.isfile()}
    # Append a harmless-looking tar padding block; unchanged package names
    # cannot excuse a layer whose actual bytes no longer match its diff_id.
    members['0/layer.tar'] += b'\0' * 512
    archive.write_bytes(tar_bytes(members))
    with pytest.raises(locks.LockError, match='layer digest'):
        inventory(root, archive)


def test_serialized_sbom_omission_is_rejected_against_actual_archives(locked):
    from scripts.image_inventory import inventory, verify_sbom

    from tests.helpers_image_inventory import image_files, write_image

    root, _, _ = locked
    commit = 'a' * 40
    for role in ('agent', 'ingest'):
        write_image(root / f'app-{role}-{commit}.tar', [image_files(root)])
    sbom = {'image_sha': commit,
            'wheels': [{'name': name, **entry} for name, entry in sorted(locks.load(root, 'runtime')[1].items())],
            'installed_python': {role: inventory(root, root / f'app-{role}-{commit}.tar')
                                 for role in ('agent', 'ingest')}}
    path = root / 'sbom.json'
    path.write_text(json.dumps(sbom))
    verify_sbom(root, root, commit)
    del sbom['installed_python']['agent']['packages']['example']
    path.write_text(json.dumps(sbom))
    with pytest.raises(locks.LockError, match='serialized SBOM'):
        verify_sbom(root, root, commit)


def test_preparation_revalidates_identity_without_reacquiring_approved_bytes(locked, monkeypatch):
    root, _, wheels = locked
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('unexpected acquisition'))
    expected = locks.verify_wheelhouse(root, 'dev', wheels)
    assert locks.prepare(root, 'dev', wheels) == expected
    marker = wheels / '.task-complete'
    first = json.loads(marker.read_text())
    monkeypatch.setattr(locks.platform, 'python_version', lambda: '3.14.99')
    assert locks.prepare(root, 'dev', wheels) == expected
    second = json.loads(marker.read_text())
    assert second['python'] == '3.14.99'
    assert first != second
    before = marker.stat().st_mtime_ns
    assert locks.prepare(root, 'dev', wheels) == expected
    assert marker.stat().st_mtime_ns == before
    marker.unlink()
    assert locks.prepare(root, 'dev', wheels) == expected
    assert marker.exists()


def test_staged_acquisition_failure_preserves_previous_cache_then_recovers(locked, monkeypatch):
    import io
    root, reports, wheels = locked
    locks.prepare(root, 'dev', wheels)
    before = {p.name: p.read_bytes() for p in wheels.iterdir()}
    replacement = b'new original synthetic approved wheel bytes'
    for report in reports.glob('*.json'):
        value = json.loads(report.read_text())
        for entry in value['install']:
            if entry['metadata']['name'] == 'example':
                entry['download_info']['archive_info']['hashes']['sha256'] = hashlib.sha256(replacement).hexdigest()
        report.write_text(json.dumps(value))
    locks.generate(root, reports)
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'incorrect acquisition'))
    with pytest.raises(locks.LockError, match='downloaded wheel'):
        locks.prepare(root, 'dev', wheels)
    assert {p.name: p.read_bytes() for p in wheels.iterdir()} == before
    assert not list(root.glob('.wheels.prepare-*'))
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(replacement))
    locks.prepare(root, 'dev', wheels)
    assert (wheels / 'example-1.0-py3-none-any.whl').read_bytes() == replacement
    assert locks.verify_wheelhouse(root, 'dev', wheels)['profile'] == 'dev'
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('unexpected acquisition'))
    assert locks.prepare(root, 'dev', wheels)['profile'] == 'dev'


@pytest.mark.parametrize('kind', ['extra', 'directory', 'member-link', 'root-link', 'ancestor-link', 'marker-link'])
def test_preparation_refuses_unowned_and_symlink_destinations(locked, monkeypatch, kind):
    root, _, wheels = locked
    sentinel = root / 'sentinel'
    sentinel.write_bytes(b'unrelated data')
    destination = wheels
    if kind == 'extra':
        (wheels / 'notes.txt').write_bytes(b'not owned')
    elif kind == 'directory':
        (wheels / 'nested').mkdir()
    elif kind in ('member-link', 'marker-link'):
        member = wheels / ('example-1.0-py3-none-any.whl' if kind == 'member-link' else '.task-complete')
        member.unlink(missing_ok=True)
        member.symlink_to(sentinel)
    else:
        link = root / 'alias'
        link.symlink_to(wheels if kind == 'root-link' else root, target_is_directory=True)
        destination = link if kind == 'root-link' else link / 'wheels'
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('acquisition before refusal'))
    with pytest.raises(locks.LockError, match='symlink|unowned'):
        locks.prepare(root, 'dev', destination)
    assert sentinel.read_bytes() == b'unrelated data'
    assert not (wheels / '.task-complete').is_file() or kind == 'marker-link'


def test_interrupted_member_publication_has_no_marker_and_next_prepare_recovers(locked, monkeypatch):
    from pathlib import Path
    root, _, wheels = locked
    locks.prepare(root, 'dev', wheels)
    marker = wheels / '.task-complete'
    marker.write_text('{}')
    original = Path.replace
    def interrupt(path, target):
        if path.name.startswith('pip-'):
            raise OSError('synthetic publication interruption')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', interrupt)
    with pytest.raises(OSError, match='publication interruption'):
        locks.prepare(root, 'dev', wheels)
    assert not marker.exists()
    assert not list(root.glob('.wheels.prepare-*'))
    monkeypatch.setattr(Path, 'replace', original)
    assert locks.prepare(root, 'dev', wheels)['profile'] == 'dev'
    assert json.loads(marker.read_text())['profile'] == 'dev'
    assert locks.verify_wheelhouse(root, 'dev', wheels)['profile'] == 'dev'


def test_preparers_serialize_and_second_reuses_complete_result(locked, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    root, _, wheels = locked
    entered = threading.Event()
    release = threading.Event()
    second_lock_attempt = threading.Event()
    original_acquire, original_flock = locks.acquire, locks.fcntl.flock
    calls = []
    attempts = []
    def acquire(*args):
        calls.append('acquire')
        entered.set()
        assert release.wait(5)
        return original_acquire(*args)
    def flock(*args):
        attempts.append('lock')
        if len(attempts) == 2:
            second_lock_attempt.set()
        return original_flock(*args)
    monkeypatch.setattr(locks, 'acquire', acquire)
    monkeypatch.setattr(locks.fcntl, 'flock', flock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(locks.prepare, root, 'dev', wheels)
        try:
            assert entered.wait(5)
            second = pool.submit(locks.prepare, root, 'dev', wheels)
            assert second_lock_attempt.wait(5)
            assert not second.done()
            assert calls == ['acquire']
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert calls == ['acquire']


@pytest.mark.parametrize('mutation', ['missing', 'corrupt', 'stamp', 'mtime', 'requirements'])
def test_preparation_freshness_uses_content_and_repair_is_explicit(locked, monkeypatch, mutation):
    import io
    root, _, wheels = locked
    locks.prepare(root, 'dev', wheels)
    wheel = wheels / 'example-1.0-py3-none-any.whl'
    approved = wheel.read_bytes()
    downloaded = []
    def fetch(*args, **kwargs):
        downloaded.append(args[0])
        return io.BytesIO(approved)
    monkeypatch.setattr(locks.urllib.request, 'urlopen', fetch)
    marker = wheels / '.task-complete'
    previous = marker.read_bytes()
    if mutation == 'missing':
        wheel.unlink()
    elif mutation == 'corrupt':
        wheel.write_bytes(b'corrupted')
    elif mutation == 'stamp':
        marker.unlink()
    elif mutation == 'mtime':
        (root / 'requirements.dev.lock.txt').touch()
    else:
        req = root / 'requirements.dev.lock.txt'
        req.write_text(req.read_text() + '# harmless identity change\n')
        manifest = root / locks.MANIFEST
        data = json.loads(manifest.read_text())
        data['profiles']['dev']['sha256'] = locks.digest(req)
        manifest.write_text(json.dumps(data))
    if mutation in ('missing', 'corrupt'):
        with pytest.raises(locks.LockError):
            locks.verify_wheelhouse(root, 'dev', wheels)
        assert downloaded == []
    locks.prepare(root, 'dev', wheels)
    assert wheel.read_bytes() == approved
    assert len(downloaded) == (1 if mutation in ('missing', 'corrupt') else 0)
    assert (marker.read_bytes() != previous) == (mutation == 'requirements')


def test_task_wheelhouse_reaches_real_preparation_owner_with_literal_path():
    import sys

    from tests.test_taskfile_contracts import TaskContractsTests
    case = TaskContractsTests()
    try:
        case.setUp()
        case.make_artifact_fixtures()
        python = case.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        literal = 'cache space אב;$(touch SENTINEL)'
        destination = case.root / literal / 'wheelhouse'
        destination.mkdir(parents=True)
        wheel = destination / 'fake_pkg-1.0.0-py3-none-any.whl'
        wheel.write_bytes(b'fake-wheel-content\n')
        run = case.run_task('artifacts:wheelhouse', f'BUNDLE_DIR={literal}', extra_env={'BUNDLE_DIR': 'ambient'})
        assert run.returncode == 0, run.stdout
        marker = destination / '.task-complete'
        assert json.loads(marker.read_text())['profile'] == 'runtime'
        assert wheel.read_bytes() == b'fake-wheel-content\n'
        assert not (case.root / 'ambient').exists()
        assert not (case.root / 'SENTINEL').exists()
        # Owner's mixed-file refusal must survive Task unchanged, with no deletion.
        unrelated = destination / 'notes.txt'
        unrelated.write_bytes(b'unrelated')
        run = case.run_task('artifacts:wheelhouse', f'BUNDLE_DIR={literal}')
        assert run.returncode != 0
        assert unrelated.read_bytes() == b'unrelated'
        assert wheel.read_bytes() == b'fake-wheel-content\n'
        unrelated.unlink()
        assert case.run_task('artifacts:wheelhouse', f'BUNDLE_DIR={literal}').returncode == 0
    finally:
        case.doCleanups()

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

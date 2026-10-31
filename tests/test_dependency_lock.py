"""Hash-bound profiles, offline acquisition boundaries and installed inventory."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from packaging.requirements import Requirement
from packaging.version import Version
from scripts import dependency_lock as locks
from scripts import prepare_python
from scripts.image_inventory import check_containerfile_bases, inventory, verify_sbom

from tests.helpers_image_inventory import (
    DOCKER_MANIFEST,
    image_files,
    oci_archive,
    tar_bytes,
    write_docker_archive,
    write_image,
)


def _report(items):
    return {
        "version": "1", "pip_version": "26.2.2",
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
    for name, version in (("example", "1.0"), ("pip", "26.2.2")):
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
    ("pkg-1.0-cp310-abi3-manylinux_2_28_x86_64.whl", True),
    ("pkg-1.0-cp314-cp314-manylinux_2_34_x86_64.whl", True),
    ("pkg-1.0-cp314t-cp314t-manylinux_2_28_x86_64.whl", False),
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
    assert {name: entry["version"] for name, entry in packages.items()} == {"example": "1.0", "pip": "26.2.2"}
    requirement = root / "requirements.dev.lock.txt"
    requirement.write_text(requirement.read_text().replace("example==1.0", "example==2.0"))
    with pytest.raises(locks.LockError, match="disagree"):
        locks.load(root, "dev")


def test_urllib3_security_pin_covers_project_and_runtime_profiles():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    requirements = [Requirement(value) for value in project["dependencies"]]
    urllib3 = next(requirement for requirement in requirements if requirement.name == "urllib3")
    for profile in ("runtime", "dev"):
        _, packages = locks.load(root, profile)
        version = packages["urllib3"]["version"]
        assert Version(version) >= Version("2.8.0")
        assert str(urllib3.specifier) == f"=={version}"
        assert packages["urllib3"]["wheel"] == f"urllib3-{version}-py3-none-any.whl"


OLEFILE_GUARD = (
    "olefile makes Pillow's FPX decoder importable (GHSA-5x8w-8jj5-q52h)"
)


def _raw_requirement_names(path: Path) -> set[str]:
    """Names in a raw requirements file, independent of manifest coverage."""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        requirement = re.split(r"\s+--hash=", line, maxsplit=1)[0].strip()
        if not requirement or requirement.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0]
        names.add(re.sub(r"[-_.]+", "-", name).lower())
    return names


def _assert_no_olefile(root: Path) -> None:
    """Fail closed if any profile selects olefile, even through a hand edit."""
    for profile, filename in locks.PROFILE_FILES.items():
        raw = _raw_requirement_names(root / filename)
        try:
            _, packages = locks.load(root, profile)
        except locks.LockError:
            # An inconsistent hand edit is already a lock failure; prefer the
            # fixed olefile diagnostic when olefile is the injected line.
            if "olefile" not in raw:
                raise
            packages = {}
        if "olefile" in raw | set(packages):
            raise AssertionError(f"{profile} lock profile contains olefile; {OLEFILE_GUARD}")


def test_olefile_is_absent_from_every_lock_profile():
    """GHSA-5x8w stays unreachable while no profile can import Pillow's FPX plugin."""
    _assert_no_olefile(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("profile", list(locks.PROFILE_FILES))
@pytest.mark.parametrize("injection", ["resolved", "hand-added"])
def test_olefile_guard_rejects_injected_lock_copy(tmp_path, profile, injection):
    root = Path(__file__).resolve().parents[1]
    for name in (locks.MANIFEST, *locks.PROFILE_FILES.values()):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes((root / name).read_bytes())
    _assert_no_olefile(tmp_path)  # the untouched copy passes
    requirement = tmp_path / locks.PROFILE_FILES[profile]
    pin = f"olefile==0.47 --hash=sha256:{'0' * 64}"
    if injection == "resolved":
        manifest = json.loads((tmp_path / locks.MANIFEST).read_text())
        manifest["packages"]["olefile"] = {
            "version": "0.47", "wheel": "olefile-0.47-py2.py3-none-any.whl",
            "sha256": "0" * 64,
            "url": "https://files.pythonhosted.org/packages/olefile-0.47-py2.py3-none-any.whl",
        }
        manifest["profiles"][profile]["packages"] = sorted(
            [*manifest["profiles"][profile]["packages"], "olefile"])
        lines = requirement.read_text(encoding="utf-8").splitlines()
        header = [text for text in lines if text.startswith("#")]
        entries = [text for text in lines if text and not text.startswith("#")]
        ordered = sorted([*entries, pin], key=lambda text: text.split("==", 1)[0])
        requirement.write_text("\n".join([*header, *ordered]) + "\n", encoding="utf-8")
        manifest["profiles"][profile]["sha256"] = locks.digest(requirement)
        (tmp_path / locks.MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _, packages = locks.load(tmp_path, profile)
        assert "olefile" in packages  # the injected copy is internally consistent
    else:
        requirement.write_text(requirement.read_text(encoding="utf-8") + pin + "\n",
                               encoding="utf-8")
    with pytest.raises(AssertionError) as raised:
        _assert_no_olefile(tmp_path)
    assert str(raised.value) == f"{profile} lock profile contains olefile; {OLEFILE_GUARD}"


def test_hypothesis_is_a_dev_only_dependency():
    """Property tests (#817) must not reach the runtime wheelhouse, either image or the build profile."""
    root = Path(__file__).resolve().parents[1]
    _, dev = locks.load(root, "dev")
    assert dev["hypothesis"]["wheel"] == f"hypothesis-{dev['hypothesis']['version']}-py3-none-any.whl"
    assert "sortedcontainers" in dev
    for profile in ("runtime", "build"):
        _, packages = locks.load(root, profile)
        assert not {"hypothesis", "sortedcontainers"} & set(packages)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert "hypothesis" not in {Requirement(value).name for value in project["dependencies"]}
    assert "hypothesis" in {Requirement(value).name for value in project["optional-dependencies"]["dev"]}


def test_wheelhouse_rejects_missing_extra_tampered_and_symlink(locked):
    root, _, wheels = locked
    receipt = locks.verify_wheelhouse(root, "dev", wheels)
    assert set(receipt["wheels"]) == {"example-1.0-py3-none-any.whl", "pip-26.2.2-py3-none-any.whl"}
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


def test_parallel_acquisition_overlaps_and_verifies_each_download_before_reuse(locked, monkeypatch, capsys):

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
    assert len(calls) == 2 and all(timeout == locks.DOWNLOAD_SOCKET_TIMEOUT_S for _, timeout in calls)
    assert {path.name: path.read_bytes() for path in wheels.glob('*.whl')} == contents
    assert locks.verify_wheelhouse(root, 'dev', wheels) == result
    assert not list(root.glob('.wheels.prepare-*'))
    assert not list(wheels.glob('.wheel-*'))
    assert 'Verified wheel:' in capsys.readouterr().err
    monkeypatch.setattr(locks.urllib.request, 'urlopen', lambda *arguments, **keywords: pytest.fail('cache hit downloaded'))
    assert locks.prepare(root, 'dev', wheels, workers=2) == result


@pytest.mark.parametrize('failure', ['checksum', 'transport'])
def test_parallel_failure_drains_workers_preserves_cache_and_next_prepare_recovers(locked, monkeypatch, failure):

    root, _, wheels = locked
    contents = {path.name: path.read_bytes() for path in wheels.glob('*.whl')}
    for path in wheels.glob('*.whl'):
        path.write_bytes(b'retained previous bytes')
    before = {path.name: path.read_bytes() for path in wheels.iterdir()}
    monkeypatch.setattr(locks, 'DOWNLOAD_ATTEMPTS', 1)
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


class _StallingWheelServer:
    """Local HTTP server: the first `stalls` requests accept and never answer."""

    def __init__(self, content, stalls, mode='silent'):
        self.requests = 0
        self.release = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests += 1
                if outer.requests <= stalls:
                    if mode == 'trickle':
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(content) + 1000))
                        self.end_headers()
                        while not outer.release.wait(0.05):
                            try:
                                self.wfile.write(b'x')
                                self.wfile.flush()
                            except OSError:
                                return
                        return
                    outer.release.wait(30)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/wheel'


def _single_wheel_entry(content):
    return {'wheel': 'x-1-py3-none-any.whl', 'sha256': hashlib.sha256(content).hexdigest(),
            'url': 'https://files.pythonhosted.org/packages/x-1-py3-none-any.whl'}


def _redirect_acquire(locked, monkeypatch, server_url, timeout=0.3, deadline=120):
    """Route the approved origin to the local server through real urllib sockets."""
    root, _, wheels = locked
    real = locks.urllib.request.urlopen
    seen = []

    def redirected(url, *, timeout):
        seen.append(timeout)
        return real(server_url, timeout=timeout)
    monkeypatch.setattr(locks.urllib.request, 'urlopen', redirected)
    monkeypatch.setattr(locks, 'DOWNLOAD_SOCKET_TIMEOUT_S', timeout)
    monkeypatch.setattr(locks, 'DOWNLOAD_DEADLINE_S', deadline)
    monkeypatch.setattr(locks, 'DOWNLOAD_BACKOFF_S', 0)
    sleeps = []
    monkeypatch.setattr(locks.time, 'sleep', sleeps.append)
    for path in wheels.glob('*.whl'):
        path.unlink()
    return root, wheels, seen, sleeps


@pytest.mark.parametrize('workers', [1, 2])
def test_stalled_download_times_out_retries_and_succeeds(locked, monkeypatch, workers):
    root, _, wheels = locked
    contents = {path.name: path.read_bytes() for path in wheels.glob('*.whl')}
    content = contents['example-1.0-py3-none-any.whl']
    with _StallingWheelServer(content, stalls=1) as server:
        _, _, _seen, sleeps = _redirect_acquire(locked, monkeypatch, server.url)
        real = locks.urllib.request.urlopen
        def by_name(url, *, timeout):
            name = url.rsplit('/', 1)[1]
            body = contents[name]
            if name.startswith('example-'):
                return real(server.url, timeout=timeout)
            return io.BytesIO(body)
        monkeypatch.setattr(locks.urllib.request, 'urlopen', by_name)
        locks.acquire(root, 'dev', wheels, workers=workers)
        assert server.requests == 2          # one stalled attempt, one retry
    assert (wheels / 'example-1.0-py3-none-any.whl').read_bytes() == content
    assert not list(wheels.glob('.wheel-*'))
    assert sleeps == [0]                     # exactly one backoff before the retry


def test_download_that_always_stalls_fails_closed_after_bounded_retries(locked, monkeypatch):
    root, _, wheels = locked
    content = (wheels / 'example-1.0-py3-none-any.whl').read_bytes()
    with _StallingWheelServer(content, stalls=10**6) as server:
        root, wheels, _seen, sleeps = _redirect_acquire(locked, monkeypatch, server.url)
        with pytest.raises(locks.LockError) as raised:
            locks.acquire(root, 'dev', wheels)
        assert server.requests == locks.DOWNLOAD_ATTEMPTS
    assert str(raised.value) == 'wheel download failed after bounded retries'
    assert len(sleeps) == locks.DOWNLOAD_ATTEMPTS - 1
    assert not list(wheels.glob('.wheel-*')) and not list(wheels.glob('example-*'))


def test_trickling_download_is_cut_off_by_the_attempt_deadline(locked, monkeypatch):
    root, _, wheels = locked
    content = (wheels / 'example-1.0-py3-none-any.whl').read_bytes()
    with _StallingWheelServer(content, stalls=10**6, mode='trickle') as server:
        root, wheels, _seen, _sleeps = _redirect_acquire(locked, monkeypatch, server.url,
                                                       timeout=5, deadline=0.3)
        monkeypatch.setattr(locks, 'DOWNLOAD_ATTEMPTS', 2)
        with pytest.raises(locks.LockError, match='after bounded retries'):
            locks.acquire(root, 'dev', wheels)
        assert server.requests == 2


def test_retried_download_still_fails_on_wrong_bytes_without_retrying(locked, monkeypatch):
    root, _, wheels = locked
    for path in wheels.glob('*.whl'):
        path.unlink()
    calls = []
    monkeypatch.setattr(locks.time, 'sleep', lambda _: None)
    monkeypatch.setattr(locks.urllib.request, 'urlopen',
                        lambda *a, **kw: calls.append(1) or io.BytesIO(b'incorrect bytes'))
    with pytest.raises(locks.LockError, match='checksum mismatch'):
        locks.acquire(root, 'dev', wheels)
    assert len(calls) == 1
    assert not list(wheels.glob('.wheel-*'))


def test_prepare_python_exits_nonzero_with_fixed_message_when_downloads_stall(locked, monkeypatch, capsys):
    root, _, wheels = locked
    for path in wheels.glob('*.whl'):
        path.unlink()
    def refuse(*a, **kw):
        raise TimeoutError('upstream detail https://secret.example/token')
    monkeypatch.setattr(locks.urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(locks.time, 'sleep', lambda _: None)
    monkeypatch.setattr(prepare_python.subprocess, 'run', lambda *a, **kw: pytest.fail('process started'))
    code = prepare_python.main(['--root', str(root), '--wheelhouse', str(wheels), '--python', sys.executable,
                                '--connected', '--download-workers', '2'])
    err = capsys.readouterr().err
    assert code == 2
    assert 'prepare-python: wheel download failed after bounded retries' in err
    assert 'secret' not in err


@pytest.mark.parametrize('workers', [0, 17, True, '4'])
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
    monkeypatch.setattr(prepare_python.importlib.metadata, 'version', lambda _: '26.2.2')
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
    assert str(wheels / 'pip-26.2.2-py3-none-any.whl') in install
    assert '--require-hashes' in install and '--no-index' in install
    project = calls[2][0]
    assert project[-2:] == ['-e', str(root)]
    assert {'--no-deps', '--no-build-isolation', '--no-index'} <= set(project)
    assert calls[-1][0][-3:] == ['--profile', 'dev', '--project']


def _kit_image(root: Path, *, dev_lock: bytes | None = None) -> tuple[Path, Path]:
    """A synthetic dev kit image: /opt/devkit with its two lock files and no wheelhouse,
    and the image's installed environment, whose dists equal the dev lock (example, pip),
    with a scripts directory holding a native tool, a Python entry point and an
    interpreter link.

    pip sits in a directory the image's own .pth file adds, as a real image's .pth
    files add theirs: the kit venv must apply them, or pip is missing. The image's
    `example` is an importable package; the checkout provides only its own package."""
    kit = root / "kit-image/opt/devkit"
    for name in prepare_python.KIT_LOCK_FILES:
        (kit / "locks" / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / name, kit / "locks" / name)
    if dev_lock is not None:
        (kit / "locks/requirements.dev.lock.txt").write_bytes(dev_lock)
    site_dir = root / "kit-image/opt/app-root/lib/python3.14/site-packages"
    nested = root / "kit-image/opt/app-root/nested"
    _dist(site_dir, "example", "1.0", top_level="example")
    (site_dir / "example").mkdir()
    (site_dir / "example/__init__.py").write_text("")
    _dist(nested, "pip", "26.2.2")
    (site_dir / "nested.pth").write_text(f"{nested}\n")
    scripts = root / "kit-image/opt/app-root/bin"
    scripts.mkdir(parents=True)
    (scripts / "native-tool").write_bytes(b"\x7fELF synthetic stand-in")
    (scripts / "entry-point").write_text("#!/opt/app-root/bin/python3\n")
    (scripts / "python3").symlink_to(scripts / "native-tool")
    # The checkout: its pyproject and the verifier the kit runs inside the new venv.
    (root / "pyproject.toml").write_text('[project]\nname = "synthetic-checkout"\nversion = "0.1"\n')
    (root / "src/synthetic_checkout").mkdir(parents=True)
    (root / "src/synthetic_checkout/__init__.py").write_text("")
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copy(Path(locks.__file__), root / "scripts/dependency_lock.py")
    return kit, site_dir


def _dist(directory: Path, name: str, version: str, *, top_level: str | None = None) -> Path:
    info = directory / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    if top_level is not None:
        (info / "top_level.txt").write_text(f"{top_level}\n")
    return info


def _kit_run(monkeypatch, root: Path, image_site: Path) -> list[list[str]]:
    """Real venv creation and real installed-set verification; pip is the one stand-in
    (the synthetic image has no runnable pip): its editable install writes what setuptools
    writes for `-e <checkout>` with a src/ layout, the dist-info and a static
    `__editable__.*.pth` naming src/."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[2:5] == ["-m", "pip", "--isolated"]:
            if "install" in argv:
                venv_site = next(Path(argv[0]).parents[1].glob("lib/python*/site-packages"))
                info = _dist(venv_site, "synthetic-checkout", "0.1", top_level="synthetic_checkout")
                (info / "direct_url.json").write_text(json.dumps(
                    {"url": Path(argv[-1]).as_uri(), "dir_info": {"editable": True}}))
                (venv_site / "__editable__.synthetic_checkout-0.1.pth").write_text(f"{Path(argv[-1]) / 'src'}\n")
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, **kwargs)

    scripts = root / "kit-image/opt/app-root/bin"
    monkeypatch.setattr(prepare_python, "image_environment", lambda: ([image_site], scripts))
    monkeypatch.setattr(prepare_python.subprocess, "run", run)
    monkeypatch.setattr(locks.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network used"))
    return calls


def test_kit_mode_layers_a_venv_over_the_image_environment_without_any_wheelhouse(locked, monkeypatch):
    """#1118 D-7 option B: the kit venv sees the image's installed environment, adds only
    the checkout (editable, no deps, no index) and equals the dev lock exactly."""
    root, _, _ = locked
    kit, image_site = _kit_image(root)
    venv = root / "kit-venv"
    calls = _kit_run(monkeypatch, root, image_site)
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), "--venv", str(venv)]) == 0
    assert not (kit / "wheelhouse").exists() and not list(root.glob("kit-image/**/*.whl"))
    assert calls[0][1:] == ["-m", "venv", "--without-pip", str(venv.absolute())]
    install, check, verify, top_level = calls[1:]
    assert install[install.index("install") + 1:] == [
        "--no-index", "--no-cache-dir", "--no-deps", "--no-build-isolation", "-e", str(root)]
    assert check[-1] == "check"
    assert verify[-6:] == ["--root", str(root), "installed", "--profile", "dev", "--project"]
    assert top_level[-1] == prepare_python.TOP_LEVEL_NAMES
    # The verifier ran for real in the new venv; the same interpreter reports the layered set.
    python = venv / "bin/python"
    probe = "import importlib.metadata as m; print(sorted(d.metadata['Name'] for d in m.distributions()))"
    listed = subprocess.run([str(python), "-I", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert listed.strip() == "['example', 'pip', 'synthetic-checkout']"
    # `python -m ruff` finds its binary beside the interpreter: native tools are linked,
    # Python entry points (the image's python) and the venv's own interpreter are not.
    bin_dir = venv / "bin"
    assert (bin_dir / "native-tool").readlink() == root / "kit-image/opt/app-root/bin/native-tool"
    assert not (bin_dir / "entry-point").exists()
    assert (bin_dir / "python3").resolve() != (root / "kit-image/opt/app-root/bin/native-tool").resolve()
    # Import order (#1158 review K1): the layer sorts before the checkout's editable .pth,
    # so a same-named package added under src/ later still loses to the locked one.
    assert min(p.name for p in venv.glob("lib/python*/site-packages/*.pth")) == prepare_python.KIT_LAYER
    (root / "src/example").mkdir()
    (root / "src/example/__init__.py").write_text("")
    probe = "import example, synthetic_checkout; print(example.__file__); print(synthetic_checkout.__file__)"
    found = subprocess.run([str(python), "-I", "-c", probe], capture_output=True, text=True, check=True).stdout.split()
    assert found == [str(image_site / "example/__init__.py"), str(root / "src/synthetic_checkout/__init__.py")]


@pytest.mark.parametrize("shadow", ["example/__init__.py", "example.py"])
def test_kit_mode_refuses_a_checkout_module_that_shadows_a_locked_distribution(locked, monkeypatch, capsys,
                                                                                shadow):
    """#1158 review K1: a top-level src/ name that a locked distribution provides is refused."""
    root, _, _ = locked
    kit, image_site = _kit_image(root)
    (root / "src" / shadow).parent.mkdir(parents=True, exist_ok=True)
    (root / "src" / shadow).write_text("")
    _kit_run(monkeypatch, root, image_site)
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), "--venv", str(root / "kit-venv")]) == 2
    assert ("prepare-python: the checkout's src/example shadows a locked distribution's top-level module "
            "example; rename it") in capsys.readouterr().err


def test_kit_mode_refuses_to_run_from_inside_a_kit_venv(locked, monkeypatch):
    """#1158 review K3: --kit run by a kit venv's interpreter names the cause, before any change."""
    root, _, _ = locked
    kit, image_site = _kit_image(root)
    venv = root / "kit-venv"
    _kit_run(monkeypatch, root, image_site)
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), "--venv", str(venv)]) == 0
    shutil.copy(Path(prepare_python.__file__), root / "scripts/prepare_python.py")
    nested = subprocess.run([str(venv / "bin/python"), str(root / "scripts/prepare_python.py"), "--root", str(root),
                             "--kit", str(kit), "--venv", str(root / "inner-venv")],
                            capture_output=True, text=True, cwd=root, check=False)
    assert nested.returncode == 2
    assert nested.stderr == ("prepare-python: --kit is running inside a kit venv; "
                             "run it with the image's python3\n")
    assert not (root / "inner-venv").exists()


@pytest.mark.parametrize("change", ["extra", "missing"])
def test_kit_mode_refuses_an_image_environment_that_differs_from_the_dev_lock(locked, monkeypatch, capfd, change):
    root, _, _ = locked
    kit, image_site = _kit_image(root)
    if change == "extra":
        _dist(image_site, "stray", "1.0")
    else:
        shutil.rmtree(image_site / "example-1.0.dist-info")
    _kit_run(monkeypatch, root, image_site)
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), "--venv", str(root / "kit-venv")]) == 2
    err = capfd.readouterr().err
    assert "installed inventory differs from the selected lock/base/project profile" in err
    assert "prepare-python: preparation failed; environment is not qualified" in err


@pytest.mark.parametrize("damage", ["symlink-venv", "no-image-environment", "newline-in-site-dir"])
def test_kit_mode_refuses_an_unusable_target_or_image_environment_before_any_process(locked, monkeypatch,
                                                                                    capsys, damage):
    root, _, _ = locked
    kit, image_site = _kit_image(root)
    calls = _kit_run(monkeypatch, root, image_site)
    venv = root / "kit-venv"
    if damage == "symlink-venv":
        (root / "elsewhere").mkdir()
        venv.symlink_to(root / "elsewhere")
    scripts = root / "kit-image/opt/app-root/bin"
    layers = {"no-image-environment": [], "newline-in-site-dir": [root / "odd\nsite"]}.get(damage, [image_site])
    monkeypatch.setattr(prepare_python, "image_environment", lambda: (layers, scripts))
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), "--venv", str(venv)]) == 2
    expected = {"symlink-venv": "refusing to prepare a symlink environment"}.get(
        damage, "the dev kit image's installed environment is unavailable")
    assert expected in capsys.readouterr().err
    assert calls == [] and not list((root / "elsewhere").iterdir() if damage == "symlink-venv" else [])
    assert damage == "symlink-venv" or not venv.exists()


def test_kit_image_environment_is_the_running_interpreters_site_and_scripts():
    """Unpatched: the layers are this interpreter's site directories, as `site` reports them."""
    import site
    import sysconfig
    layers, scripts = prepare_python.image_environment()
    assert layers == [Path(path) for path in site.getsitepackages() if Path(path).is_dir()] and layers
    assert scripts == Path(sysconfig.get_path("scripts")) and (scripts / "python").exists()


def test_kit_mode_refuses_an_image_built_for_another_dev_lock_naming_the_kit(tmp_path):
    root = Path(__file__).resolve().parents[1]
    kit = tmp_path / "kit-image"
    for name in prepare_python.KIT_LOCK_FILES:
        (kit / "locks" / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / name, kit / "locks" / name)
    (kit / "locks/requirements.dev.lock.txt").write_bytes(b"# another dev lock\n")
    from scripts.devkit_key import key
    with pytest.raises(locks.LockError, match=f"another dev lock; request mfr-devkit-{key(root)}$"):
        prepare_python.check_kit(root, kit)


@pytest.mark.parametrize("damage", ["dev-lock", "connected", "internal-index", "python-target"])
def test_kit_mode_fails_closed_before_any_process(locked, monkeypatch, capsys, damage):
    root, _, _ = locked
    kit, _ = _kit_image(root, dev_lock=b"# another dev lock\n" if damage == "dev-lock" else None)
    monkeypatch.setattr(prepare_python.subprocess, "run", lambda *a, **kw: pytest.fail("process started"))
    monkeypatch.setattr(locks.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("network used"))
    environment = root / "new-venv"
    target = ["--python", sys.executable] if damage == "python-target" else ["--venv", str(environment)]
    extra = {"connected": ["--connected"], "internal-index": ["--internal-index"]}.get(damage, [])
    assert prepare_python.main(["--root", str(root), "--kit", str(kit), *target, *extra]) == 2
    expected = {"dev-lock": "this dev kit image was built for another dev lock; request",
                "connected": "--kit is offline only",
                "internal-index": "--kit is offline only",
                "python-target": "--kit prepares its own environment: use --venv"}[damage]
    assert expected in capsys.readouterr().err
    assert not environment.exists()


@pytest.mark.parametrize("compressed", [False, True])
def test_actual_image_layers_and_receipt_reconcile(locked, compressed):


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



    root, _, _ = locked
    archive = root / 'image.tar'
    raw = json.loads(write_image(archive, [image_files(root)]))
    layer = raw['layers'][0]['digest']
    with tarfile.open(archive) as source:
        members = {item.name: source.extractfile(item).read() if item.isfile() else None for item in source}
    # Append a harmless-looking tar padding block; unchanged package names
    # cannot excuse a blob whose actual bytes no longer match its descriptor.
    members[f'blobs/sha256/{layer[7:]}'] += b'\0' * 512
    archive.write_bytes(tar_bytes(members))
    with pytest.raises(locks.LockError, match='blob does not match its digest'):
        inventory(root, archive)
    # A self-consistent OCI archive (blob digests rewritten to match) is still
    # refused when the layer is not the rootfs its config's diff_id binds.
    padded = members[f'blobs/sha256/{layer[7:]}']
    config = json.dumps({'os': 'linux', 'architecture': 'amd64', 'rootfs': {
        'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(padded[:-512]).hexdigest()]}}).encode()
    oci_archive(archive, config, [padded], manifest_type=DOCKER_MANIFEST)
    with pytest.raises(locks.LockError, match='layer digest'):
        inventory(root, archive)


def test_image_inventory_reads_only_single_image_oci_archives(locked):



    root, _, _ = locked
    archive = root / 'image.tar'
    # Pre-#1066 member format: the SBOM reader no longer accepts docker-archive.
    write_docker_archive(archive, [image_files(root)])
    with pytest.raises(locks.LockError, match='exactly one OCI image'):
        inventory(root, archive)
    write_image(archive, [image_files(root)])
    with tarfile.open(archive) as source:
        members = {item.name: source.extractfile(item).read() if item.isfile() else None for item in source}
    index = json.loads(members['index.json'])
    index['manifests'].append(dict(index['manifests'][0]))
    archive.write_bytes(tar_bytes({**members, 'index.json': json.dumps(index).encode()}))
    with pytest.raises(locks.LockError, match='exactly one OCI image'):
        inventory(root, archive)


def test_serialized_sbom_omission_is_rejected_against_actual_archives(locked):


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


@pytest.mark.parametrize('kind', ['extra', 'member-link', 'root-link', 'ancestor-link', 'marker-link'])
def test_preparation_refuses_unowned_and_symlink_destinations(locked, monkeypatch, kind):
    root, _, wheels = locked
    sentinel = root / 'sentinel'
    sentinel.write_bytes(b'unrelated data')
    destination = wheels
    if kind == 'extra':
        (wheels / 'notes.txt').write_bytes(b'not owned')
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


@pytest.mark.parametrize('mutation', ['corrupt', 'mtime', 'requirements'])
def test_preparation_freshness_uses_content_and_repair_is_explicit(locked, monkeypatch, mutation):
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
    if mutation == 'corrupt':
        wheel.write_bytes(b'corrupted')
    elif mutation == 'mtime':
        (root / 'requirements.dev.lock.txt').touch()
    else:
        req = root / 'requirements.dev.lock.txt'
        req.write_text(req.read_text() + '# harmless identity change\n')
        manifest = root / locks.MANIFEST
        data = json.loads(manifest.read_text())
        data['profiles']['dev']['sha256'] = locks.digest(req)
        manifest.write_text(json.dumps(data))
    if mutation == 'corrupt':
        with pytest.raises(locks.LockError):
            locks.verify_wheelhouse(root, 'dev', wheels)
        assert downloaded == []
    locks.prepare(root, 'dev', wheels)
    assert wheel.read_bytes() == approved
    assert len(downloaded) == (1 if mutation == 'corrupt' else 0)
    assert (marker.read_bytes() != previous) == (mutation == 'requirements')


def test_task_wheelhouse_reaches_real_preparation_owner_with_literal_path():

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


def _containerfile_tree(tmp_path: Path) -> Path:
    """The real images.txt, lock and Containerfiles: the inputs the base check reads."""
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "locks").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "images.txt").write_bytes((repo / "images.txt").read_bytes())
    (tmp_path / locks.MANIFEST).write_bytes((repo / locks.MANIFEST).read_bytes())
    for path in (repo / "images").glob("Containerfile.*"):
        (tmp_path / "images" / path.name).write_bytes(path.read_bytes())
    return tmp_path


def test_containerfile_check_admits_the_devkit_tool_context_and_no_lookalike(tmp_path):
    """#1118: the dev workspace image reads its pinned tool archives from a named build
    context. The base check admits that name, and a lookalike, which BuildKit would
    resolve as a Docker Hub image, stays refused."""
    root = _containerfile_tree(tmp_path)
    devkit = root / "images" / "Containerfile.devkit"
    recipe = devkit.read_text()
    # The wheelhouse is bind-mounted for the install only, never copied (D-7).
    assert "from=devkit-tools," in recipe and "--mount=type=bind,from=wheelhouse," in recipe
    assert "COPY --from=wheelhouse" not in recipe
    check_containerfile_bases(root)
    for context in ("devkit-tools", "wheelhouse"):
        devkit.write_text(recipe.replace(f"from={context},", f"from={context[:-1]},"))
        with pytest.raises(locks.LockError, match="non-stage image"):
            check_containerfile_bases(root)
    devkit.write_text(recipe.replace(locks.BASE_IMAGE.rpartition("@")[2], "sha256:" + "0" * 64, 1))
    with pytest.raises(locks.LockError, match="base digest differs"):
        check_containerfile_bases(root)

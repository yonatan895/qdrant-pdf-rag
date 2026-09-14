"""Qdrant least-privilege authorization battery (issue #366, marker:
``integration``, run via ``make sim`` neighbors — not part of ``make check``).

Behavioral proof against the pinned Qdrant image (not Secret-name
assertions): the read-only key from the chart's ``readOnlyApiKey`` value
permits every serving read (query/search/info/exists/snapshots-list) and is
denied every mutation, while the full-access key ingests. Rotation proves a
new keyset works and the old one is rejected without data loss.

Ephemeral docker container with per-run random keys (``secrets`` module).
Keys never reach test output: assertions compare status codes and payload
shapes only, and failure messages carry no credentials. Skips cleanly when
docker (or the pinned image) is unavailable, like the sim tier.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx2
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTION = "authz"
VEC = [0.1, 0.2, 0.3, 0.4]


def _pinned_image() -> str:
    for line in (REPO_ROOT / "images.txt").read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        fields = line.split()
        if fields and "qdrant" in fields[0]:
            return fields[0]
    raise ValueError("no qdrant image pin found in images.txt")


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _have_image(pin: str) -> str | None:
    """Image reference the local daemon actually serves. The pin is the
    canonical ``docker.io/``-qualified ref, but a daemon fed from the local
    registry mirror may tag it short — ``docker images -q`` does not
    normalize the prefix, so probe both before attempting a pull."""
    candidates = [pin]
    if pin.startswith("docker.io/"):
        candidates.append(pin[len("docker.io/"):])
    for cand in candidates:
        if _docker("images", "-q", cand, timeout=30).stdout.strip():
            return cand
    return None


def _start(auth: dict[str, str], volume: str | None = None) -> tuple[str, str]:
    """Start an ephemeral Qdrant with the given service keys. Returns
    (base_url, container_id). Skips when docker cannot serve."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    if _docker("info", timeout=15).returncode != 0:
        pytest.skip("docker daemon not reachable")
    try:
        pin = _pinned_image()
    except ValueError as exc:
        pytest.skip(str(exc))
    image = _have_image(pin)
    if image is None:
        if _docker("pull", pin, timeout=300).returncode != 0:
            pytest.skip(f"cannot pull pinned Qdrant image {pin}")
        image = pin
    cmd = ["run", "-d", "--rm", "-p", "127.0.0.1::6333"]
    for env_key, env_val in auth.items():
        cmd += ["-e", f"{env_key}={env_val}"]
    if volume is not None:
        cmd += ["-v", f"{volume}:/qdrant/storage"]
    cmd.append(image)
    proc = _docker(*cmd, timeout=60)
    if proc.returncode != 0:
        pytest.skip(f"docker run failed: {proc.stderr.strip()[-200:]}")
    cid = proc.stdout.strip()
    port = _docker("port", cid, "6333/tcp", timeout=15).stdout.strip()
    base = f"http://127.0.0.1:{port.split(':')[-1]}"
    for _ in range(60):
        try:
            resp = httpx2.get(f"{base}/readyz", timeout=3.0)
            if resp.status_code == 200 and "all shards are ready" in resp.text.lower():
                return base, cid
        except Exception:  # noqa: BLE001, S110 — still starting
            pass
        time.sleep(1)
    _docker("stop", cid, timeout=30)
    pytest.skip("ephemeral Qdrant did not become ready")


def _stop(cid: str) -> None:
    _docker("stop", cid, timeout=30)


def _new_keys() -> dict[str, str]:
    return {
        "QDRANT__SERVICE__API_KEY": secrets.token_hex(16),
        "QDRANT__SERVICE__READ_ONLY_API_KEY": secrets.token_hex(16),
    }


def _headers(key: str) -> dict[str, str]:
    return {"api-key": key, "Content-Type": "application/json"}


def _seed(base: str, write_key: str) -> None:
    """Create + fill the synthetic collection with the write key. Every
    call below asserts success: a failed authorization setup must fail the
    test, never count as a pass."""
    resp = httpx2.put(
        f"{base}/collections/{COLLECTION}",
        headers=_headers(write_key),
        json={"vectors": {"size": 4, "distance": "Cosine"}},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code
    resp = httpx2.put(
        f"{base}/collections/{COLLECTION}/points",
        headers=_headers(write_key),
        json={
            "points": [
                {"id": 1, "vector": VEC, "payload": {"doc_id": "d1"}},
                {"id": 2, "vector": [0.4, 0.3, 0.2, 0.1], "payload": {"doc_id": "d2"}},
            ]
        },
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code


@pytest.fixture(scope="session")
def auth_cluster():
    """One keyed server per session: denied operations change no state and
    the write-key roundtrip only adds a collection, so sharing is safe and
    keeps the battery fast."""
    keys = _new_keys()
    base, cid = _start(keys)
    try:
        _seed(base, keys["QDRANT__SERVICE__API_KEY"])
        yield base, keys
    finally:
        _stop(cid)


def _ro(auth: tuple[str, dict[str, str]]) -> tuple[str, str]:
    base, keys = auth
    return base, keys["QDRANT__SERVICE__READ_ONLY_API_KEY"]


def test_readonly_key_permits_serving_reads(auth_cluster):
    """Every Qdrant call the serving agent makes (retrieve query path,
    collection info/exists probes, snapshot listing, /readyz) succeeds."""
    base, ro = _ro(auth_cluster)
    # query_points shape as retrieve issues it (vector + filter + payload).
    resp = httpx2.post(
        f"{base}/collections/{COLLECTION}/points/query",
        headers=_headers(ro),
        json={
            "query": VEC,
            "limit": 2,
            "with_payload": True,
            "filter": {"must": [{"key": "doc_id", "match": {"value": "d1"}}]},
        },
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code
    assert [p["id"] for p in resp.json()["result"]["points"]] == [1]
    for method, path, body in (
        ("POST", f"/collections/{COLLECTION}/points/search", {"vector": VEC, "limit": 1}),
        ("GET", f"/collections/{COLLECTION}", None),
        ("GET", f"/collections/{COLLECTION}/exists", None),
        ("GET", "/collections", None),
        ("GET", f"/collections/{COLLECTION}/snapshots", None),
        ("POST", f"/collections/{COLLECTION}/points/count", {"exact": True}),
    ):
        resp = httpx2.request(
            method, f"{base}{path}", headers=_headers(ro), json=body, timeout=10.0
        )
        assert resp.status_code == 200, (method, path, resp.status_code)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/collections/authz-new", {"vectors": {"size": 4, "distance": "Cosine"}}),
        ("PUT", f"/collections/{COLLECTION}/points", {"points": []}),
        ("POST", f"/collections/{COLLECTION}/points/delete", {"points": [1]}),
        (
            "POST",
            f"/collections/{COLLECTION}/points/payload",
            {"payload": {"x": 1}, "points": [1]},
        ),
        (
            "PUT",
            f"/collections/{COLLECTION}/index",
            {"field_name": "doc_id", "field_schema": "keyword"},
        ),
        ("POST", f"/collections/{COLLECTION}/snapshots", None),
        ("DELETE", f"/collections/{COLLECTION}", None),
    ],
)
def test_readonly_key_denied_mutations(auth_cluster, method, path, body):
    """Mutations and admin operations are forbidden (403) — a serving
    compromise or programming error cannot mutate the corpus."""
    base, ro = _ro(auth_cluster)
    resp = httpx2.request(
        method, f"{base}{path}", headers=_headers(ro), json=body, timeout=10.0
    )
    assert resp.status_code == 403, (method, path, resp.status_code)


@pytest.mark.parametrize("key", ["wrong-key", ""])
def test_wrong_and_missing_keys_rejected(auth_cluster, key):
    """Wrong or absent credentials are unauthorized (401) on reads and
    writes alike."""
    base, _ = auth_cluster
    headers = _headers(key) if key else {"Content-Type": "application/json"}
    resp = httpx2.post(
        f"{base}/collections/{COLLECTION}/points/query",
        headers=headers,
        json={"query": VEC, "limit": 1},
        timeout=10.0,
    )
    assert resp.status_code == 401, resp.status_code
    resp = httpx2.put(
        f"{base}/collections/{COLLECTION}/points",
        headers=headers,
        json={"points": []},
        timeout=10.0,
    )
    assert resp.status_code == 401, resp.status_code


def test_write_key_ingests_and_serves_roundtrip(auth_cluster):
    """The ingest credential retains full write access on the same server."""
    base, keys = auth_cluster
    write = keys["QDRANT__SERVICE__API_KEY"]
    resp = httpx2.put(
        f"{base}/collections/roundtrip",
        headers=_headers(write),
        json={"vectors": {"size": 4, "distance": "Cosine"}},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code
    resp = httpx2.put(
        f"{base}/collections/roundtrip/points",
        headers=_headers(write),
        json={"points": [{"id": 7, "vector": VEC}]},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code
    ro = keys["QDRANT__SERVICE__READ_ONLY_API_KEY"]
    resp = httpx2.post(
        f"{base}/collections/roundtrip/points/query",
        headers=_headers(ro),
        json={"query": VEC, "limit": 1},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.status_code
    assert [p["id"] for p in resp.json()["result"]["points"]] == [7]


def test_rotation_rejects_old_keys_without_data_loss(tmp_path):
    """Chart-native rotation (delete Secret, helm upgrade regenerates both
    keys): a volume-backed server restarted under a new keyset serves the
    old points to the new read-only key while the entire old keyset 401s."""
    voldir = tmp_path / "qdrant-data"
    voldir.mkdir()
    voldir.chmod(0o777)
    old = _new_keys()
    base, cid = _start(old, volume=str(voldir))
    try:
        _seed(base, old["QDRANT__SERVICE__API_KEY"])
    finally:
        _stop(cid)
    new = _new_keys()
    base, cid = _start(new, volume=str(voldir))
    try:
        new_ro = new["QDRANT__SERVICE__READ_ONLY_API_KEY"]
        resp = httpx2.post(
            f"{base}/collections/{COLLECTION}/points/query",
            headers=_headers(new_ro),
            json={"query": VEC, "limit": 5},
            timeout=10.0,
        )
        assert resp.status_code == 200, resp.status_code
        assert sorted(p["id"] for p in resp.json()["result"]["points"]) == [1, 2]
        for stale in (
            old["QDRANT__SERVICE__API_KEY"],
            old["QDRANT__SERVICE__READ_ONLY_API_KEY"],
        ):
            resp = httpx2.post(
                f"{base}/collections/{COLLECTION}/points/query",
                headers=_headers(stale),
                json={"query": VEC, "limit": 1},
                timeout=10.0,
            )
            assert resp.status_code == 401, resp.status_code
    finally:
        _stop(cid)

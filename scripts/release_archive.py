#!/usr/bin/env python3
"""Private release archive (#1118, decision D5); contract in docs/deploy.md#release-archive.

Keeps the newest site releases' signed sneakernet bundles (loose members, as
the site receives them) as private GHCR OCI artifacts pinned by digest, for the
previous-release -> new-release upgrade lane. release-archive.txt is the one
owner record. Every bundle is verified against an out-of-band trusted public
key before a push and again after a pull by digest; the bundled key is never
trusted on its own.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "release-archive.txt"
REPOSITORY = "ghcr.io/yonatan895/qdrant-pdf-rag-release-archive"
REGISTRY_URL = "https://ghcr.io"  # the only registry that ever receives gh credentials
ORAS = ROOT / ".tools" / "bin" / "oras"
ORAS_PIN = ROOT / "scripts" / "tools" / "oras-pin.txt"
ARTIFACT_TYPE = "application/vnd.qdrant-pdf-rag.sneakernet-bundle.v1"
KEEP = 3
REDIRECTS = frozenset({301, 302, 303, 307, 308})
CHUNK = 1 << 20
SIGNATURE = ("SHA256SUMS", "SHA256SUMS.sig")
REQUIRED = ("SHA256SUMS", "SHA256SUMS.sig", "sneakernet-signing.pub", "MANIFEST.txt")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
DATE_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
MEMBER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HEADER = """\
# Private release archive (#1118, decision D5): the newest site releases as
# private GHCR OCI artifacts, pinned by digest. Written only by
# scripts/release_archive.py; contract in docs/deploy.md#release-archive.
# Repository: {repository}
# Columns: release-sha  manifest-digest  signing-key-sha256  packed-date (MANIFEST date, UTC)
"""


class ArchiveError(Exception):
    """A refusal with a fixed operator message; exit status 2."""


@dataclass(frozen=True)
class Row:
    sha: str
    digest: str
    key: str
    date: str

    def line(self) -> str:
        return f"{self.sha}  {self.digest}  {self.key}  {self.date}"


@dataclass(frozen=True)
class Bundle:
    sha: str
    date: str
    key: str
    files: dict[str, tuple[str, int]]  # member name -> (sha256 hex, size)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_field(text: str, name: str) -> str:
    values = [line.split(":", 1)[1].strip() for line in text.splitlines()
              if line.startswith(f"{name}:")]
    if len(values) != 1:
        raise ArchiveError(f"MANIFEST.txt must carry exactly one {name}: line")
    return values[0]


def verify_bundle(bundle: Path, release_sha: str, trusted_pub: Path) -> Bundle:
    """Fail closed unless `bundle` is exactly the signed release `release_sha`.

    The bundled public key must equal the trusted key byte for byte, the
    signature over SHA256SUMS must verify with the trusted key, SHA256SUMS must
    list every other regular file exactly once (nothing unsigned rides along),
    every member must match its checksum, and MANIFEST must name the release.
    """
    if not SHA_RE.fullmatch(release_sha):
        raise ArchiveError("release SHA must be a full lowercase 40-hex commit SHA")
    if not trusted_pub.is_file():
        raise ArchiveError("trusted public key file not found")
    if not bundle.is_dir():
        raise ArchiveError("bundle directory not found")
    entries = sorted(bundle.iterdir())
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise ArchiveError("bundle must contain only regular files (no symlinks or directories)")
    names = {entry.name for entry in entries}
    for name in REQUIRED:
        if name not in names:
            raise ArchiveError(f"unsigned or incomplete bundle: {name} is missing")
    if (bundle / "sneakernet-signing.pub").read_bytes() != trusted_pub.read_bytes():
        raise ArchiveError("bundle public key does not match the trusted key: untrusted bundle")
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(trusted_pub),
         "-signature", str(bundle / "SHA256SUMS.sig"), str(bundle / "SHA256SUMS")],
        capture_output=True, check=False,
    )
    if verified.returncode != 0:
        raise ArchiveError("SHA256SUMS signature verification failed: do not trust this bundle")
    listed: dict[str, str] = {}
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", line)
        if not match or not MEMBER_RE.fullmatch(match[2]) or match[2] in listed:
            raise ArchiveError("SHA256SUMS has a malformed, unsafe or duplicate entry")
        listed[match[2]] = match[1]
    if set(listed) != names - set(SIGNATURE):
        raise ArchiveError("SHA256SUMS must list exactly the bundle members (no unsigned or missing files)")
    files: dict[str, tuple[str, int]] = {}
    for name in sorted(names):
        path = bundle / name
        observed = _sha256_file(path)
        if name in listed and observed != listed[name]:
            raise ArchiveError(f"member checksum mismatch: {name}")
        files[name] = (observed, path.stat().st_size)
    manifest = (bundle / "MANIFEST.txt").read_text(encoding="utf-8")
    if _manifest_field(manifest, "sha") != release_sha:
        raise ArchiveError("MANIFEST sha does not match the release SHA")
    date = _manifest_field(manifest, "date")
    if not DATE_RE.fullmatch(date):
        raise ArchiveError("MANIFEST date must be an ISO-8601 UTC timestamp")
    return Bundle(release_sha, date, _sha256_file(trusted_pub), files)


# ------------------------------------------------------------------ record


def read_record(path: Path) -> list[Row]:
    if not path.exists():
        return []
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split()
        if (len(fields) != 4 or not SHA_RE.fullmatch(fields[0])
                or not DIGEST_RE.fullmatch(fields[1]) or not HEX64_RE.fullmatch(fields[2])
                or not DATE_RE.fullmatch(fields[3])):
            raise ArchiveError("release-archive record has a malformed row")
        rows.append(Row(*fields))
    if len({row.sha for row in rows}) != len(rows) or len({row.digest for row in rows}) != len(rows):
        raise ArchiveError("release-archive record has a duplicate release or digest")
    return rows


def newest_first(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda row: (row.date, row.sha), reverse=True)


def write_record(path: Path, rows: list[Row], repository: str) -> None:
    body = HEADER.format(repository=repository) + "".join(
        row.line() + "\n" for row in newest_first(rows))
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(body)
    os.chmod(temp, 0o644)
    os.replace(temp, path)


# ------------------------------------------------------------------ tools


class Tools:
    """The pinned oras binary (verified before every run) and gh for the
    GitHub Packages API (visibility, versions, deletion)."""

    def __init__(self, oras: Path, pin: Path, repository: str, login: bool,
                 registry_url: str | None = None):
        self.oras = oras
        self.repository = repository
        parts = repository.split("/")
        if len(parts) != 3 or parts[0] != "ghcr.io" or not all(parts):
            raise ArchiveError("repository must be ghcr.io/<owner>/<package>")
        self.owner, self.package = parts[1], parts[2]
        self.registry_url = (registry_url or REGISTRY_URL).rstrip("/")
        if self.registry_url != REGISTRY_URL and not _is_loopback(self.registry_url):
            raise ArchiveError("registry URL must be https://ghcr.io (loopback only for tests)")
        self._gh_token: bytes | None = None  # in memory only; never logged or written by the tool
        self._bearer: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)
        pin_text = pin.read_text(encoding="utf-8") if pin.is_file() else ""
        want = re.search(r"^binary-sha256: *([0-9a-f]{64})$", pin_text, re.MULTILINE)
        version = re.search(r"^version: *v(\S+)$", pin_text, re.MULTILINE)
        if not want or not version:
            raise ArchiveError("invalid oras pin file")
        self._binary_sha, self._version = want[1], version[1]
        self._config_dir = tempfile.TemporaryDirectory(prefix="release-archive-")
        self.config = Path(self._config_dir.name) / "registry-config.json"
        self._login = login

    def close(self) -> None:
        self._config_dir.cleanup()

    def _verify_oras(self) -> None:
        if not self.oras.is_file() or _sha256_file(self.oras) != self._binary_sha:
            raise ArchiveError("oras binary missing or not the pinned build; run scripts/tools/install-oras.sh")

    def run_oras(self, *args: str, stdin: bytes | None = None, cwd: Path | None = None) -> bytes:
        self._verify_oras()
        command = [str(self.oras), *args]
        if args[0] != "version":
            command += ["--registry-config", str(self.config)]
        done = subprocess.run(command, input=stdin, capture_output=True, check=False, cwd=cwd)
        if done.returncode != 0:
            raise ArchiveError(f"oras {args[0]} failed (exit {done.returncode})")
        return done.stdout

    def login(self) -> None:
        version = re.search(r"^Version:\s+(\S+)$", self.run_oras("version").decode(), re.MULTILINE)
        if not version or version[1] != self._version:
            raise ArchiveError("oras version does not match the pin")
        if not self._login:
            self.config.write_text("{}\n")
            return
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, check=False)
        if token.returncode != 0 or not token.stdout.strip():
            raise ArchiveError("gh auth token failed; log in with gh (write:packages)")
        self._gh_token = token.stdout.strip()
        self.run_oras("login", "--username", self.owner, "--password-stdin", "ghcr.io",
                      stdin=self._gh_token + b"\n")

    def gh_api(self, *args: str) -> tuple[int, object]:
        done = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
        try:
            body = json.loads(done.stdout) if done.stdout.strip() else None
        except json.JSONDecodeError:
            body = None
        return done.returncode, body

    def visibility(self) -> str:
        """'absent' (package not created yet), 'private', or the other state."""
        code, body = self.gh_api(f"/user/packages/container/{self.package}")
        if code == 0 and isinstance(body, dict) and isinstance(body.get("visibility"), str):
            return body["visibility"]
        if isinstance(body, dict) and str(body.get("status")) == "404":
            return "absent"
        raise ArchiveError("could not read the package visibility from the GitHub API")

    def versions(self) -> list[dict]:
        code, body = self.gh_api("--paginate", "--slurp",
                                 f"/user/packages/container/{self.package}/versions")
        if code != 0 or not isinstance(body, list):
            raise ArchiveError("could not list package versions from the GitHub API")
        return [version for page in body for version in (page if isinstance(page, list) else [page])]

    def delete_version(self, version_id: int) -> None:
        code, _ = self.gh_api("--method", "DELETE",
                              f"/user/packages/container/{self.package}/versions/{version_id}")
        if code != 0:
            raise ArchiveError("package version deletion failed (gh needs the delete:packages scope)")

    def resolve(self, tag: str) -> str | None:
        try:
            out = self.run_oras("resolve", f"{self.repository}:{tag}").decode().strip()
        except ArchiveError:
            return None
        if not DIGEST_RE.fullmatch(out):
            raise ArchiveError("oras resolve returned an invalid digest")
        return out

    def fetch_manifest(self, digest: str) -> dict:
        raw = self.run_oras("manifest", "fetch", f"{self.repository}@{digest}")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ArchiveError("digest mismatch: the registry manifest does not hash to the requested digest")
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArchiveError("registry manifest is not JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("artifactType") != ARTIFACT_TYPE:
            raise ArchiveError("registry artifact is not a sneakernet bundle")
        return manifest


    # ---------------------------------------------- resumable blob download

    def _token(self, challenge: str) -> str:
        """Registry bearer token from a `WWW-Authenticate: Bearer` challenge."""
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer ") or "realm" not in fields:
            raise ArchiveError("registry sent an unsupported authentication challenge")
        realm = fields.pop("realm")
        # The gh token goes only to the pinned registry's own token endpoint
        # (https://ghcr.io/token): a challenge naming any other origin is refused
        # before anything is sent.
        if _origin(realm) != _origin(self.registry_url):
            raise ArchiveError("registry token realm is not on the registry origin; credentials not sent")
        request = urllib.request.Request(realm + "?" + urllib.parse.urlencode(fields))
        if self._gh_token:
            basic = base64.b64encode(self.owner.encode() + b":" + self._gh_token).decode()
            request.add_unredirected_header("Authorization", f"Basic {basic}")
        try:
            with self._opener.open(request, timeout=60) as response:
                body = json.loads(response.read())
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ArchiveError("registry token request failed") from exc
        token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ArchiveError("registry token response has no token")
        return token

    def _open_blob(self, url: str, offset: int) -> http.client.HTTPResponse:
        """GET with Range from `offset`. Redirects are followed by hand, and the
        Authorization header never goes to another origin (blob storage)."""
        current, same_origin, refreshed = url, True, False
        for _ in range(5):
            request = urllib.request.Request(current)
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            if same_origin and self._bearer:
                request.add_unredirected_header("Authorization", f"Bearer {self._bearer}")
            try:
                return self._opener.open(request, timeout=60)
            except urllib.error.HTTPError as err:
                headers, code = err.headers, err.code
                err.close()
                if code in REDIRECTS and headers.get("Location"):
                    target = urllib.parse.urljoin(current, headers["Location"])
                    _require_safe_url(target)
                    if _origin(target) != _origin(current):
                        same_origin = False
                    current = target
                    continue
                if code == 401 and same_origin and not refreshed:
                    self._bearer = self._token(headers.get("WWW-Authenticate", ""))
                    refreshed = True
                    continue
                if code == 416:
                    raise _RangeRefused from None
                if code in (401, 429) or code >= 500:
                    raise _Transient from None
                raise ArchiveError(f"registry refused the blob request (HTTP {code})") from None
        raise ArchiveError("too many redirects for a blob request")

    def _fetch_once(self, url: str, partial: Path) -> None:
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            response = self._open_blob(url, offset)
        except _RangeRefused:
            partial.unlink(missing_ok=True)  # resume point unusable: restart from byte 0
            raise _Transient from None
        with response:
            mode = "wb"
            if offset and response.status == 206:
                match = re.fullmatch(r"bytes (\d+)-\d+/(?:\d+|\*)",
                                     response.headers.get("Content-Range", ""))
                if not match or int(match[1]) != offset:
                    partial.unlink(missing_ok=True)
                    raise _Transient
                mode = "ab"
            elif response.status not in (200, 206):
                raise ArchiveError(f"registry returned an unexpected status (HTTP {response.status})")
            # A 200 to a ranged request means the server ignored Range: restart cleanly.
            with partial.open(mode) as stream:
                while True:
                    try:
                        chunk = response.read(CHUNK)
                    except http.client.IncompleteRead as exc:
                        stream.write(exc.partial)  # keep what arrived before the drop
                        raise
                    if not chunk:
                        break
                    stream.write(chunk)

    def fetch_blob(self, title: str, digest: str, size: int, partial: Path,
                   attempts: int, backoff: float) -> None:
        """Download one layer blob into `partial`, resuming with HTTP Range after
        a dropped connection. `attempts` bounds consecutive tries without
        progress. The whole file is re-hashed at the end; a mismatch deletes it."""
        url = f"{self.registry_url}/v2/{self.owner}/{self.package}/blobs/sha256:{digest}"
        stalled = 0
        while True:
            before = partial.stat().st_size if partial.exists() else 0
            if before > size:
                partial.unlink()
                before = 0
            if before == size:
                break
            # A dropped stream, 5xx or expired token keeps the partial for the next try.
            with contextlib.suppress(_Transient, OSError, http.client.HTTPException):
                self._fetch_once(url, partial)
            after = partial.stat().st_size if partial.exists() else 0
            if after >= size:
                break
            stalled = 0 if after > before else stalled + 1
            if stalled >= attempts:
                raise ArchiveError(f"blob download made no progress in {attempts} attempts: {title}")
            print(f"release-archive: resuming {title} at byte {after} of {size}", file=sys.stderr)
            time.sleep(min(backoff * 2 ** stalled, 60.0))
        if partial.stat().st_size != size or _sha256_file(partial) != digest:
            partial.unlink()
            raise ArchiveError(f"fetched blob does not match its layer digest: {title}")


class _Transient(Exception):
    """A retryable transport failure (dropped stream, 5xx, 429, expired token)."""


class _RangeRefused(Exception):
    """HTTP 416: the partial file cannot be resumed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as HTTPError so Tools._open_blob decides about headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) with the default port filled in, for exact comparison."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme, 0)
    except ValueError:
        port = -1
    return parts.scheme, (parts.hostname or "").lower(), port


def _is_loopback(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost")
            and not parts.username and parts.path in ("", "/"))


def _require_safe_url(url: str) -> None:
    """HTTPS only; plain HTTP is accepted for loopback test registries alone."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise ArchiveError("registry and token URLs must use HTTPS")


def _layers(manifest: dict) -> dict[str, tuple[str, int]]:
    layers: dict[str, tuple[str, int]] = {}
    for layer in manifest.get("layers") or []:
        title = (layer.get("annotations") or {}).get("org.opencontainers.image.title", "")
        digest = layer.get("digest", "")
        if not MEMBER_RE.fullmatch(title) or title in layers or not DIGEST_RE.fullmatch(digest):
            raise ArchiveError("registry artifact has an unsafe or duplicate layer")
        size = layer.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArchiveError("registry artifact has a layer without a valid size")
        layers[title] = (digest.removeprefix("sha256:"), size)
    return layers


def _require_private(tools: Tools, when: str, allow_absent: bool) -> str:
    state = tools.visibility()
    print(f"release-archive: package visibility {when} push: {state}")
    if state == "private" or (allow_absent and state == "absent"):
        return state
    raise ArchiveError(f"package {tools.package} is {state} {when} push, not private: stop and "
                       "fix its visibility in the GitHub package settings")


# ------------------------------------------------------------------ commands


def cmd_verify(args: argparse.Namespace) -> int:
    bundle = verify_bundle(args.bundle, args.release_sha, args.trusted_pub)
    print(f"release-archive: verified {bundle.sha} ({len(bundle.files)} files, packed {bundle.date}, "
          f"key sha256 {bundle.key})")
    return 0


def cmd_push(args: argparse.Namespace, tools: Tools) -> int:
    bundle = verify_bundle(args.bundle, args.release_sha, args.trusted_pub)
    rows = read_record(args.record)
    if any(row.sha == bundle.sha for row in rows):
        raise ArchiveError("release is already archived in the record; pull it by digest instead")
    tools.login()
    _require_private(tools, "before", allow_absent=True)
    existing = tools.resolve(bundle.sha)
    if existing is not None:
        # Recovery after a push that was not recorded: adopt only identical bytes.
        if _layers(tools.fetch_manifest(existing)) != bundle.files:
            raise ArchiveError("archive tag already exists with different content; not overwriting")
        digest = existing
        print(f"release-archive: tag exists with identical content, adopting {digest}")
    else:
        with tempfile.TemporaryDirectory(prefix="release-archive-push-") as scratch:
            exported = Path(scratch) / "manifest.json"
            # No org.opencontainers.image.source: linking the package to the
            # public repository must not be able to change its visibility.
            tools.run_oras(
                "push", "--no-tty", f"{tools.repository}:{bundle.sha}",
                "--artifact-type", ARTIFACT_TYPE,
                "--annotation", f"org.opencontainers.image.created={bundle.date}",
                "--annotation", f"org.opencontainers.image.revision={bundle.sha}",
                "--annotation", f"io.github.yonatan895.signing-key-sha256={bundle.key}",
                "--export-manifest", str(exported),
                *sorted(bundle.files), cwd=args.bundle,
            )
            digest = "sha256:" + _sha256_file(exported)
        if tools.resolve(bundle.sha) != digest:
            raise ArchiveError("pushed tag does not resolve to the pushed manifest digest")
        if _layers(tools.fetch_manifest(digest)) != bundle.files:
            raise ArchiveError("pushed artifact layers do not match the verified bundle")
    _require_private(tools, "after", allow_absent=False)
    write_record(args.record, [*rows, Row(bundle.sha, digest, bundle.key, bundle.date)],
                 tools.repository)
    print(f"release-archive: archived {bundle.sha} at {tools.repository}@{digest}")
    return 0


def cmd_pull(args: argparse.Namespace, tools: Tools) -> int:
    rows = {row.digest: row for row in read_record(args.record)}
    row = rows.get(args.digest)
    if row is None:
        raise ArchiveError("digest is not in the release-archive record")
    if not args.trusted_pub.is_file():
        raise ArchiveError("trusted public key file not found")
    if _sha256_file(args.trusted_pub) != row.key:
        raise ArchiveError("trusted key does not match the recorded signing key")
    out: Path = args.out
    tools.login()
    layers = _layers(tools.fetch_manifest(row.digest))
    out.mkdir(parents=True, exist_ok=True)
    # Resume: the directory may hold only members of this artifact (an earlier
    # interrupted pull); a member already matching its layer digest is kept.
    entries = list(out.iterdir())
    if not {entry.name for entry in entries} <= set(layers) or any(
            entry.is_symlink() or not entry.is_file() for entry in entries):
        raise ArchiveError("output directory must be empty or hold only this artifact's members")
    # Partial downloads survive an interrupted run beside DIR, never inside it.
    partial_dir = out.parent / f".{out.name}.partial"
    partial_dir.mkdir(mode=0o700, exist_ok=True)
    for title, (digest, size) in sorted(layers.items()):
        target = out / title
        if target.is_file() and target.stat().st_size == size and _sha256_file(target) == digest:
            continue
        # Named by digest: a stale partial of another artifact's same-named member
        # can never be resumed into this one.
        staged = partial_dir / f"{digest}.partial"
        tools.fetch_blob(title, digest, size, staged, args.attempts, args.backoff)
        os.replace(staged, target)
    leftovers = list(partial_dir.iterdir())
    if leftovers:  # partials of other artifacts: not ours to delete
        print(f"release-archive: left {len(leftovers)} unrelated partial file(s) in {partial_dir}",
              file=sys.stderr)
    else:
        partial_dir.rmdir()
    if {entry.name for entry in out.iterdir()} != set(layers):
        raise ArchiveError("pulled files do not match the artifact layers")
    bundle = verify_bundle(out, row.sha, args.trusted_pub)
    if bundle.files != layers:
        raise ArchiveError("pulled files do not match the artifact layer digests")
    print(f"release-archive: pulled {row.sha} by digest {row.digest} and re-verified its signature")
    return 0


def cmd_list(args: argparse.Namespace, tools: Tools | None) -> int:
    rows = newest_first(read_record(args.record))
    for index, row in enumerate(rows):
        state = "keep" if index < args.keep else "prune"
        remote = ""
        if tools is not None:
            resolved = tools.resolve(row.sha)
            remote = "  remote=" + ("missing" if resolved is None else
                                    "ok" if resolved == row.digest else "MISMATCH")
        print(f"{row.line()}  {state}{remote}")
    if tools is not None:
        print(f"release-archive: package visibility: {tools.visibility()}")
    return 0


def cmd_prune(args: argparse.Namespace, tools: Tools) -> int:
    rows = newest_first(read_record(args.record))
    kept, dropped = rows[:args.keep], rows[args.keep:]
    if not dropped:
        print(f"release-archive: {len(kept)} release(s) recorded; nothing to prune")
        return 0
    by_digest = {version.get("name"): version.get("id") for version in tools.versions()}
    remaining = list(rows)
    for row in dropped:
        version_id = by_digest.get(row.digest)
        if version_id is not None:  # absent means an earlier prune already deleted it
            tools.delete_version(int(version_id))
        remaining.remove(row)
        write_record(args.record, remaining, tools.repository)
        print(f"release-archive: pruned {row.sha} ({row.digest})")
    print(f"release-archive: kept {len(kept)} newest release(s)")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    top.add_argument("--record", type=Path, default=RECORD)
    top.add_argument("--repository", default=REPOSITORY)
    top.add_argument("--oras", type=Path, default=ORAS)
    top.add_argument("--oras-pin", type=Path, default=ORAS_PIN)
    top.add_argument("--registry-url", default=None,
                     help="registry API base for blob downloads (default https://ghcr.io)")
    top.add_argument("--no-login", action="store_true",
                     help="use an empty registry config instead of gh credentials")
    sub = top.add_subparsers(dest="command", required=True)
    for name in ("verify", "push"):
        command = sub.add_parser(name)
        command.add_argument("--bundle", type=Path, required=True,
                             help="directory of loose signed bundle members")
        command.add_argument("--release-sha", required=True)
        command.add_argument("--trusted-pub", type=Path, required=True,
                             help="public key obtained out of band (never the bundled one)")
    pull = sub.add_parser("pull")
    pull.add_argument("--digest", required=True)
    pull.add_argument("--out", type=Path, required=True)
    pull.add_argument("--trusted-pub", type=Path, required=True)
    pull.add_argument("--attempts", type=int, default=5,
                      help="consecutive blob fetch attempts without progress before refusing")
    pull.add_argument("--backoff", type=float, default=2.0,
                      help="base seconds of exponential backoff between attempts")
    listing = sub.add_parser("list")
    listing.add_argument("--remote", action="store_true")
    prune = sub.add_parser("prune")
    for command in (listing, prune):
        command.add_argument("--keep", type=int, default=KEEP)
    return top


def _terminate(signum: int, _frame: object) -> None:
    # SIGTERM's default action skips `finally`; exit through it so the
    # token-bearing registry config directory is always removed.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _terminate)
    args = parser().parse_args(argv)
    tools: Tools | None = None
    try:
        if getattr(args, "attempts", 1) < 1 or getattr(args, "backoff", 0.0) < 0:
            raise ArchiveError("--attempts must be at least 1 and --backoff not negative")
        if args.command == "prune" and args.keep < KEEP:
            raise ArchiveError(f"prune keeps at least {KEEP} releases (decision D5)")
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "list" and not args.remote:
            return cmd_list(args, None)
        tools = Tools(args.oras, args.oras_pin, args.repository, login=not args.no_login,
                      registry_url=args.registry_url)
        if args.command == "list":
            tools.login()
            return cmd_list(args, tools)
        if args.command == "push":
            return cmd_push(args, tools)
        if args.command == "pull":
            return cmd_pull(args, tools)
        return cmd_prune(args, tools)
    except ArchiveError as exc:
        print(f"release-archive: {exc}", file=sys.stderr)
        return 2
    finally:
        if tools is not None:
            tools.close()


if __name__ == "__main__":
    raise SystemExit(main())

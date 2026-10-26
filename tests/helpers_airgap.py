"""Shared hermetic harness for scripts/airgap/*.sh tests.

Share builders, pin behavior: per-script env deltas, stub arms, and
fail-close assertions stay explicit at each call site. Only the
mechanical tree/run/sign/stub plumbing lives here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STUB_TOOL = "#!/bin/sh\nexit 0\n"


def set_oauth_proxy_pin(tree: Path, digest: str) -> None:
    """Select an explicit OAuth pin state independently of the production pin."""
    path = tree / "images.txt"
    updated, count = re.subn(
        r"^(registry\.redhat\.io/openshift4/ose-oauth-proxy:\S+)[ \t]+\S+$",
        lambda match: f"{match[1]} {digest}", path.read_text(), flags=re.MULTILINE,
    )
    assert count == 1
    path.write_text(updated)
    record = tree / "licenses" / "inventory.json"
    if record.is_file():  # keep the license record bound to the explicit pin state
        inventory = json.loads(record.read_text())
        for component in inventory["components"]:
            if component["id"] == "image:oauth-proxy":
                component["bind"]["digest"] = digest
        record.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


def set_openwebui_pin(tree: Path, digest: str) -> None:
    """Select an explicit Open WebUI pin state (ADR-0007, #1074), independently
    of the production pin, keeping its license record bound to the same state."""
    path = tree / "images.txt"
    updated, count = re.subn(
        r"^(ghcr\.io/yonatan895/qdrant-pdf-rag-openwebui:\S+)[ \t]+\S+$",
        lambda match: f"{match[1]} {digest}", path.read_text(), flags=re.MULTILINE,
    )
    assert count == 1
    path.write_text(updated)
    record = tree / "licenses" / "inventory.json"
    if record.is_file():
        inventory = json.loads(record.read_text())
        for component in inventory["components"]:
            if component["id"] == "image:openwebui":
                component["bind"]["digest"] = digest
        record.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_stub(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def make_bin_tree(tmp_path: Path, scripts: list[str]) -> Path:
    """Create bin/ + scripts/airgap/ and copy the named scripts from the repo."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "airgap").mkdir(parents=True, exist_ok=True)
    if any(name in scripts for name in ("deploy.sh", "ingest.sh", "validate.sh")):
        shutil.copytree(REPO / "charts/mainframe-rag", tmp_path / "charts/mainframe-rag", dirs_exist_ok=True)
    if "deploy.sh" in scripts or "validate.sh" in scripts:
        # legacy_layout.py: the stored-layout preflight (#1070) runs from both.
        scripts = [*scripts, "check_app_ownership.py", "check_route_exposure.py", "legacy_layout.py"]
    if "common.sh" in scripts:
        # resolve_observability evaluates the values mapper's decisions.
        scripts = [*scripts, "model_config.py", "map_values.py"]
    if any(name in scripts for name in ("pack.sh", "load.sh", "deploy.sh", "ingest.sh")):
        scripts = [*scripts, "image_identity.sh", "image_manifest.py"]
    if any(name in scripts for name in ("deploy.sh", "ingest.sh")):
        scripts = [*scripts, "check_pod_images.py"]
    if "deploy.sh" in scripts:
        scripts = [*scripts, "check_qdrant_render.py", "check_qdrant_volumes.py"]
    if "ingest.sh" in scripts:
        scripts = [*scripts, "check_ingest_patch.py"]
    for f in scripts:
        shutil.copy(REPO / "scripts" / "airgap" / f, tmp_path / "scripts" / "airgap" / f)
    return tmp_path


def copy_chart(tmp_path: Path) -> Path:
    (tmp_path / "charts").mkdir(exist_ok=True)
    dest = tmp_path / "charts" / next(REPO.glob("charts/qdrant-*.tgz")).name
    if not dest.exists():
        shutil.copy(next(REPO.glob("charts/qdrant-*.tgz")), dest)
    return dest


def copy_license_inputs(tmp_path: Path) -> None:
    """Everything scripts/license_inventory.py reads for a pack tree (#376)."""
    # Every vendored console asset, not a fixed list: the checker binds each
    # file in static/vendor, so a new vendored file must reach the tree too.
    vendor = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "src/mainframe_rag/webui/static/vendor").iterdir()
        if p.is_file()
    )
    notices = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "licenses").rglob("*")
        if p.is_file() and p.name != "inventory.json"
    )
    for relative in ("licenses/inventory.json", *notices, "scripts/license_inventory.py", "pyproject.toml", "LICENSE",
                     "LICENSE.qdrant-skills", "NOTICE.qdrant-skills",
                     "LICENSE.mattpocock-skills", "NOTICE.mattpocock-skills",
                     ".agents/mattpocock-skills.sha256",
                     "LICENSE.pstack-skills", "NOTICE.pstack-skills",
                     ".agents/pstack-skills.sha256", "bm25-weights.sha256", *vendor):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, target)


def symlink_tools(tmp_path: Path, tools: tuple[str, ...]) -> None:
    for tool in tools:
        src = shutil.which(tool)
        if src and not (tmp_path / "bin" / tool).exists():
            (tmp_path / "bin" / tool).symlink_to(src)


def write_git_identity_stub(tree: Path, sha: str) -> None:
    """Hermetic executing-checkout identity for fixtures that claim a packed
    release (a MANIFEST present, not dry-run): common.sh::check_checkout_sha
    fails closed on an unresolvable checkout (issue #414), so such fixtures say
    which commit is checked out and that it is clean. Real-git cases live in
    the checkout_guard tests; everything but those reads (HEAD, diff, the
    index-flag listing) is real git."""
    import shlex

    real = shutil.which("git")
    write_stub(tree / "bin" / "git", f"""#!/bin/sh
case "$1" in
    rev-parse) [ "$2" = HEAD ] && {{ echo {shlex.quote(sha)}; exit 0; }} ;;
    diff) exit 0 ;;
    ls-files) exit 0 ;;
esac
exec {shlex.quote(real)} "$@"
""")


_MANIFEST_KEY: Path | None = None


def write_signed_manifest(dist: Path, content: str) -> None:
    """Write dist/MANIFEST.txt plus a real signature chain for it: SHA256SUMS
    listing exactly MANIFEST.txt, SHA256SUMS.sig and sneakernet-signing.pub
    (throwaway RSA key, reused per process). The guard verifies this chain
    before trusting the MANIFEST (issue #414)."""
    global _MANIFEST_KEY
    if _MANIFEST_KEY is None:
        import tempfile

        _MANIFEST_KEY = gen_sign_keypair(Path(tempfile.mkdtemp(prefix="manifest-key-")))
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "MANIFEST.txt").write_text(content)
    (dist / "SHA256SUMS").write_text(f"{sha256_bytes(content.encode())}  MANIFEST.txt\n")
    (dist / "sneakernet-signing.pub").unlink(missing_ok=True)
    sign_sums(dist, _MANIFEST_KEY)


def run_sh(script: Path, env: dict, cwd: Path):
    return subprocess.run(
        ["sh", str(script)], capture_output=True, text=True, env=env, cwd=cwd, check=False
    )


def gen_sign_keypair(artdir: Path, key_name: str = "signing.key") -> Path:
    key = artdir / key_name
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def sign_sums(artdir: Path, key: Path | None = None) -> None:
    key = key or (artdir / "signing.key")
    if not (artdir / "sneakernet-signing.pub").exists():
        subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout",
             "-out", str(artdir / "sneakernet-signing.pub")],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key),
         "-out", str(artdir / "SHA256SUMS.sig"), str(artdir / "SHA256SUMS")],
        check=True,
        capture_output=True,
    )


def gen_other_pub(tmp_path: Path, name: str = "other") -> Path:
    other = tmp_path / f"{name}.key"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(other)],
        check=True,
        capture_output=True,
    )
    pub = tmp_path / f"{name}.pub"
    subprocess.run(
        ["openssl", "pkey", "-in", str(other), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    return pub


def skopeo_stub(digest_char: str, materialize: bool = False) -> str:
    """Canned `inspect` digest + optional oci-archive materialization.

    pack.sh needs the materialize arm (it "pulls" into tar files);
    load.sh only needs inspect + arg logging.
    """
    digest = "sha256:" + digest_char * 64
    materialize_arm = ""
    if materialize:
        materialize_arm = """for arg in "$@"; do
  case "$arg" in
    oci-archive:*)
      dest="${arg#oci-archive:}"
      mkdir -p "$(dirname "$dest")"
      printf 'stub-image-tar\\n' > "$dest"
      ;;
  esac
done
"""
    return f"""#!/bin/sh
if [ "$1" = "inspect" ]; then
  printf '{digest}\\n'
  printf '%s\\n' "$@" >> "$SKOPEO_LOG"
  exit 0
fi
{materialize_arm}printf '%s\\n' "$@" >> "$SKOPEO_LOG"
exit 0
"""


def assert_no_placeholders(text: str) -> None:
    assert "__" not in text


def assert_pull_secret_wired(rendered: str, name: str) -> None:
    assert re.search(
        rf"^([ ]*)imagePullSecrets:\n\1  - name: {re.escape(name)}$", rendered, re.MULTILINE
    )


def install_rendering_helm(tree: Path) -> None:
    """Use real Helm for rendering, record rather than execute live mutations."""
    import shlex

    helm = shutil.which('helm')
    if not helm:
        raise RuntimeError('pinned Helm is required for deployment render tests')
    shutil.copytree(REPO / 'charts/mainframe-rag', tree / 'charts/mainframe-rag', dirs_exist_ok=True)
    write_stub(tree / 'bin/helm', '''#!/bin/sh
if [ -n "${HELM_LOG:-}" ]; then printf '%s\\n' "$@" >> "$HELM_LOG"; fi
case "$1" in
    template|lint|version) exec ''' + shlex.quote(helm) + ''' "$@" ;;
    upgrade) exit 0 ;;
    *) exit 2 ;;
esac
''')


def rendered_container(rendered: str, name: str) -> dict:
    import yaml

    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get('kind') in ('Deployment', 'Job'):
            for container in doc['spec']['template']['spec']['containers']:
                if container['name'] == name:
                    return container
    raise AssertionError(f'container {name} missing from rendered resources')


def rendered_env(rendered: str, name: str) -> dict:
    return {entry['name']: entry.get('value', entry.get('valueFrom'))
            for entry in rendered_container(rendered, name)['env']}


class FakeHttpServer:
    """Loopback HTTP server for trace-backend and Qdrant tests; records every request.

    `respond(request) -> (status, json_body)`, where request is a dict with
    method, path, query (parse_qs), headers and body (decoded JSON or None).
    """

    def __init__(self, respond):
        import http.server
        import threading
        from urllib.parse import parse_qs, urlsplit

        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                request = {
                    "method": self.command, "path": parts.path, "query": parse_qs(parts.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                }
                outer.requests.append(request)
                status, body = respond(request)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class FakeQdrant:
    """In-memory Qdrant REST fake for the layout check and the retire step
    (#1070), faithful to the observed 1.19 behaviour the scripts rely on:
    `GET /aliases`; `exists` is true for an alias name as well as a
    collection; `DELETE` of an absent or alias name answers `result: false`
    and changes nothing; reads of an absent collection are 404; collection
    snapshot listings exist only while the collection does.

    Keys: the read key may only read; every write needs the writer key.
    `faults` maps an operation label (`"<op>:<collection>"` or `"<op>"`) to
    the number of upcoming matching requests that fail with HTTP 500, which
    simulates a crash at that point. `bad_snapshots` lists snapshot
    collections whose listing omits the checksum.
    """

    READ_KEY = "reader-key-0123456789abcdef"
    WRITE_KEY = "writer-key-fedcba9876543210"
    WRITE_OPS = frozenset({"create", "index", "upsert", "delete", "snapshot_create", "aliases_update"})

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.faults: dict[str, int] = {}
        self.bad_snapshots: set[str] = set()
        self.ops: list[str] = []
        self._snap = 0
        self.server = FakeHttpServer(self.respond)
        self.url = self.server.url

    def close(self):
        self.server.close()

    # -- fixtures --------------------------------------------------------
    def add_collection(self, name, points, *, size=4, schema=None):
        self.collections[name] = {
            "config": {"params": {"vectors": {"dense": {"size": size, "distance": "Cosine"}},
                                  "sparse_vectors": {"bm25": {"modifier": "idf"}},
                                  "shard_number": 1, "replication_factor": 1,
                                  "write_consistency_factor": 1, "on_disk_payload": True},
                       "hnsw_config": {"m": 16, "ef_construct": 100}, "quantization_config": None},
            "payload_schema": dict(schema or {}),
            "points": {str(p["id"]): json.loads(json.dumps(p)) for p in points},
            "snapshots": [],
        }

    def ids(self, name):
        return sorted(self.collections[name]["points"])

    # -- server ----------------------------------------------------------
    def _fault(self, label):
        for key in (label, label.split(":", 1)[0]):
            if self.faults.get(key, 0) > 0:
                self.faults[key] -= 1
                return True
        return False

    def respond(self, request):
        method, path = request["method"], request["path"]
        parts = [p for p in path.split("/") if p]
        body = request["body"] or {}
        op, name = self._route(method, parts)
        self.ops.append(f"{op}:{name}" if name else op)
        key = request["headers"].get("api-key")
        if key not in (self.READ_KEY, self.WRITE_KEY):
            return 401, {"status": {"error": "Must provide an API key"}}
        if op in self.WRITE_OPS and key != self.WRITE_KEY:
            return 403, {"status": {"error": "read-only key"}}
        if self._fault(f"{op}:{name}" if name else op):
            return 500, {"status": {"error": "injected"}}
        coll = self.collections.get(name) if name else None
        ok = lambda result: (200, {"result": result, "status": "ok"})
        missing = (404, {"status": {"error": f"Collection `{name}` doesn't exist!"}})
        if op == "aliases_get":
            return ok({"aliases": [{"alias_name": a, "collection_name": c} for a, c in self.aliases.items()]})
        if op == "aliases_update":
            for action in body.get("actions", []):
                if "delete_alias" in action:
                    self.aliases.pop(action["delete_alias"]["alias_name"], None)
                if "create_alias" in action:
                    spec = action["create_alias"]
                    if spec["collection_name"] not in self.collections:
                        return 404, {"status": {"error": "target missing"}}
                    self.aliases[spec["alias_name"]] = spec["collection_name"]
            return ok(True)
        if op == "exists":
            return ok({"exists": name in self.collections or name in self.aliases})
        if op == "delete":
            if name in self.collections:
                del self.collections[name]
                return ok(True)
            return ok(False)
        if op == "create":
            if name in self.collections or name in self.aliases:
                return 409, {"status": {"error": "exists"}}
            self.collections[name] = {"config": {"params": {k: body.get(k) for k in (
                "vectors", "sparse_vectors", "shard_number", "replication_factor",
                "write_consistency_factor", "on_disk_payload")},
                "hnsw_config": body.get("hnsw_config"), "quantization_config": body.get("quantization_config")},
                "payload_schema": {}, "points": {}, "snapshots": []}
            return ok(True)
        if coll is None:
            return missing
        if op == "info":
            return ok({"status": "green", "points_count": len(coll["points"]),
                       "config": coll["config"], "payload_schema": coll["payload_schema"]})
        if op == "index":
            schema = body["field_schema"]
            coll["payload_schema"][body["field_name"]] = (
                {"data_type": schema} if isinstance(schema, str) else {"data_type": schema["type"], "params": schema})
            return ok({"status": "completed"})
        if op == "upsert":
            for point in body["points"]:
                coll["points"][str(point["id"])] = json.loads(json.dumps(point))
            return ok({"status": "completed"})
        if op == "retrieve":
            found = [self._out(coll["points"][str(i)], True) for i in body["ids"] if str(i) in coll["points"]]
            return ok(found)
        if op == "scroll":
            ordered = sorted(coll["points"])
            start = ordered.index(str(body["offset"])) if "offset" in body else 0
            page = ordered[start:start + body.get("limit", 10)]
            following = ordered[start + len(page)] if start + len(page) < len(ordered) else None
            with_data = body.get("with_vector") or body.get("with_payload")
            return ok({"points": [self._out(coll["points"][i], with_data) for i in page],
                       "next_page_offset": following})
        if op == "snapshot_list":
            listed = [dict(s) for s in coll["snapshots"]]
            if name in self.bad_snapshots:
                for entry in listed:
                    entry["checksum"] = None
            return ok(listed)
        if op == "snapshot_create":
            self._snap += 1
            entry = {"name": f"{name}-peer-2026-10-27-{self._snap:02d}.snapshot",
                     "creation_time": f"2026-10-27T00:00:{self._snap:02d}",
                     "size": 4096 + len(coll["points"]),
                     "checksum": hashlib.sha256(f"{name}{self._snap}".encode()).hexdigest()}
            coll["snapshots"].append(entry)
            return ok(dict(entry))
        return 400, {"status": {"error": f"unsupported {method} {path}"}}

    @staticmethod
    def _out(point, with_data):
        if with_data:
            return json.loads(json.dumps(point))
        return {"id": point["id"]}

    @staticmethod
    def _route(method, parts):
        if parts == ["aliases"] and method == "GET":
            return "aliases_get", ""
        if parts == ["collections", "aliases"] and method == "POST":
            return "aliases_update", ""
        if len(parts) < 2 or parts[0] != "collections":
            return "unknown", ""
        name, rest = parts[1], parts[2:]
        table = {
            ("GET", ()): "info", ("PUT", ()): "create", ("DELETE", ()): "delete",
            ("GET", ("exists",)): "exists", ("PUT", ("index",)): "index",
            ("PUT", ("points",)): "upsert", ("POST", ("points",)): "retrieve",
            ("POST", ("points", "scroll")): "scroll",
            ("GET", ("snapshots",)): "snapshot_list", ("POST", ("snapshots",)): "snapshot_create",
        }
        return table.get((method, tuple(rest)), "unknown"), name


# Operator keys release d53595e honoured that later releases ignore (#1070).
# Values are distinctive sentinels so a leak into any output is detectable.
RETIRED_ENV_KEYS = (
    "REGISTRY_INTERNAL",
    "OPENSHIFT_NAMESPACE",
    "VLLM_BASE_URL",
    "JAEGER_ENABLED",
    "JAEGER_QUERY_URL",
)
RETIRED_ENV_VALUES = {key: f"sentinel-{key.lower().replace('_', '-')}" for key in RETIRED_ENV_KEYS}

# An airgap.env shaped like d53595e's example after an operator filled it in:
# every legacy key uncommented next to keys that are still current.
D53595E_SHAPED_ENV = """\
INTERNAL_REGISTRY=registry.example.internal/mainframe-rag
REGISTRY_INTERNAL=sentinel-registry-internal
NAMESPACE=mainframe-rag              # legacy alias: OPENSHIFT_NAMESPACE
OPENSHIFT_NAMESPACE=sentinel-openshift-namespace
GATEWAY_BASE_URL=https://sample-api/v1
VLLM_BASE_URL=sentinel-vllm-base-url
JAEGER_ENABLED=false
JAEGER_QUERY_URL=sentinel-jaeger-query-url
INGEST_ALIAS_PUBLISH=true
"""


def retired_keys_listed(stderr: str) -> list[str]:
    """Keys named in the refusal, read from its fixed one-key-per-line layout."""
    return re.findall(r"(?m)^  ([A-Z][A-Z0-9_]+) \(retired in ", stderr)

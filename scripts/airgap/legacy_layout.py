"""Stored-layout check and legacy-collection retirement (issue #1070, R1).

Runs INSIDE the running agent pod (`kubectl exec -i deploy/rag-agent -- python3
-c BOOT ...`): the Qdrant image has no shell tools or Python, and Qdrant is
ClusterIP-only, so the agent pod is the one place on the cluster network with
Python. The old (d53595e) agent cannot import this release's package, so this
file is stdlib-only and repeats two id derivations; tests pin them to the
package (`publication_metadata_point_id`, `manifest_point_id`).

stdin carries one line with the Qdrant key first (empty = use the pod's own
`QDRANT_API_KEY`, the serving read-only key), then this program. The key never
appears in argv, the environment of another process, or any output.

  check   read-only. Prints one line `LAYOUT <code>`:
          empty          no alias and no collection named L (fresh store)
          ok             alias L -> T and T__completions holds a build-bound
                         publication record (the agent validates the rest)
          flat_legacy    no alias L and a collection L (pre-publication
                         layout, release d53595e): retire it first
          rolled_back    alias L -> the kept d53595e copy (after a rollback):
                         re-run retire to unpoint it before upgrading again
          unbound_alias  alias L -> T whose control has no build binding
                         (pre-build publication): unsupported
          unreadable     the store could not be read (exit 3): never a pass
  retire  writer key. Converges L/LC to: L and LC deleted, their verified
          points-API copies K/KC kept (the rollback data, re-keyed so the
          d53595e agent serves K), and a verified snapshot of L and LC on
          every peer. Each step re-observes the store first, so a re-run
          after a crash at any point continues or reports converged. From
          the rolled-back state it removes the alias L (K stays).
  rollback writer key. One atomic alias call points L at the kept copy K.

Output is fixed text plus collection/snapshot names and counts: never stored
payloads, keys or upstream error text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

COMPLETIONS_SUFFIX = "__completions"
KEEP_SUFFIX = "_d53595e_keep"
MANIFEST_PREFIX = "representation-manifest"
PUBLICATION_PREFIX = "publication-metadata"
BUILD_FIELDS = ("build_schema", "build_id", "logical_alias", "data_collection")
PAGE = 256
CHECKSUM_RE = re.compile(r"[0-9a-f]{64}")
RUNBOOK = "docs/install_and_ops.md#upgrade-from-a-legacy-flat-collection-issue-1070"
EXIT_REFUSED = 1
EXIT_UNREADABLE = 3


class Refusal(Exception):
    """A fixed, operator-facing refusal (exit 1). Nothing further is changed."""


class Unreadable(Exception):
    """A transport or server failure (exit 3). Fixed text only."""


def manifest_point_id(completions: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{MANIFEST_PREFIX}|{completions}"))


def publication_point_id(completions: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{PUBLICATION_PREFIX}|{completions}"))


def ids_digest(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:16]


class Qdrant:
    """Minimal REST client. Paths are built from validated names only."""

    def __init__(self, url: str, key: str, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout

    def call(self, method: str, path: str, body: object = None, *, allow_404: bool = False):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["api-key"] = self.key
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return None
            raise Unreadable(f"Qdrant {method} {_route(path)} returned HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise Unreadable(f"Qdrant {method} {_route(path)} failed (transport)") from None
        try:
            return json.loads(raw or b"null").get("result")
        except (ValueError, AttributeError):
            raise Unreadable(f"Qdrant {method} {_route(path)} returned a malformed body") from None

    def aliases(self) -> dict[str, str]:
        result = self.call("GET", "/aliases") or {}
        return {a["alias_name"]: a["collection_name"] for a in result.get("aliases", [])}

    def exists(self, name: str) -> bool:
        # On Qdrant 1.19 `exists` is also true for an alias name: callers
        # read the aliases first and never take this alone as a collection.
        return bool((self.call("GET", f"/collections/{_q(name)}/exists") or {}).get("exists"))

    def info(self, name: str) -> dict:
        return self.call("GET", f"/collections/{_q(name)}") or {}

    def scroll(self, name: str, *, with_data: bool):
        offset = None
        while True:
            body: dict = {"limit": PAGE, "with_payload": with_data, "with_vector": with_data}
            if offset is not None:
                body["offset"] = offset
            page = self.call("POST", f"/collections/{_q(name)}/points/scroll", body) or {}
            yield from page.get("points", [])
            offset = page.get("next_page_offset")
            if offset is None:
                return

    def ids(self, name: str) -> list[str]:
        return [str(p["id"]) for p in self.scroll(name, with_data=False)]

    def retrieve(self, name: str, point_id: str) -> dict | None:
        found = self.call(
            "POST", f"/collections/{_q(name)}/points",
            {"ids": [point_id], "with_payload": True, "with_vector": True}, allow_404=True,
        )
        return found[0] if found else None

    def upsert(self, name: str, points: list[dict]) -> None:
        self.call("PUT", f"/collections/{_q(name)}/points?wait=true", {"points": points})

    def delete(self, name: str) -> None:
        self.call("DELETE", f"/collections/{_q(name)}?timeout=300")

    def snapshots(self, name: str) -> list[dict]:
        return self.call("GET", f"/collections/{_q(name)}/snapshots") or []

    def create_snapshot(self, name: str) -> dict:
        return self.call("POST", f"/collections/{_q(name)}/snapshots?wait=true") or {}


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def _route(path: str) -> str:
    """The request route without the query, for fixed error text."""
    return path.split("?", 1)[0]


# --- check ------------------------------------------------------------------


def classify(store: Qdrant, logical: str) -> str:
    """The deploy/validate preflight rule (mirrors qdrant_io.live_collection_from
    plus the serving build-binding gate). Aliases are read first."""
    aliases = store.aliases()
    if logical in aliases:
        if aliases[logical] == logical + KEEP_SUFFIX:
            return "rolled_back"
        control = aliases[logical] + COMPLETIONS_SUFFIX
        record = store.retrieve(control, publication_point_id(control))
        payload = (record or {}).get("payload") or {}
        if payload.get("record_type") != PUBLICATION_PREFIX or not set(BUILD_FIELDS) & set(payload):
            return "unbound_alias"
        return "ok"
    return "flat_legacy" if store.exists(logical) else "empty"


# --- retire -----------------------------------------------------------------


def _creation_body(info: dict) -> dict:
    config = info.get("config") or {}
    params = dict(config.get("params") or {})
    if params.get("sharding_method") == "custom":
        raise Refusal("E_RETIRE_CUSTOM_SHARDING: the legacy collection uses custom sharding; not supported here")
    body = {k: params[k] for k in (
        "vectors", "sparse_vectors", "shard_number", "replication_factor",
        "write_consistency_factor", "on_disk_payload",
    ) if params.get(k) is not None}
    for key in ("hnsw_config", "quantization_config"):
        if config.get(key) is not None:
            body[key] = config[key]
    return body


def _same_shape(source: dict, copy: dict) -> bool:
    params = lambda i: (i.get("config") or {}).get("params") or {}
    return all(params(source).get(k) == params(copy).get(k) for k in ("vectors", "sparse_vectors"))


def preserve(store: Qdrant, source: str, copy: str, extra_ids: frozenset[str] = frozenset()) -> int:
    """Points-API copy source -> copy, verified by exact id sets. Resumes a
    partial copy (ids(copy) - extra must be a subset of ids(source)); any
    other existing copy is refused, never overwritten or deleted."""
    source_info = store.info(source)
    if store.exists(copy):
        if not _same_shape(source_info, store.info(copy)):
            raise Refusal(f"E_RETIRE_KEEP_MISMATCH: {copy} exists with a different vector shape; inspect it")
        present = set(store.ids(copy)) - extra_ids
        if not present <= set(store.ids(source)):
            raise Refusal(f"E_RETIRE_KEEP_MISMATCH: {copy} holds points that {source} does not; inspect it")
    else:
        store.call("PUT", f"/collections/{_q(copy)}", _creation_body(source_info))
        for field, schema in sorted((source_info.get("payload_schema") or {}).items()):
            field_schema = schema.get("params") or schema.get("data_type")
            store.call("PUT", f"/collections/{_q(copy)}/index?wait=true",
                       {"field_name": field, "field_schema": field_schema})
    batch: list[dict] = []
    for point in store.scroll(source, with_data=True):
        batch.append({"id": point["id"], "vector": point.get("vector") or {}, "payload": point.get("payload") or {}})
        if len(batch) >= PAGE:
            store.upsert(copy, batch)
            batch = []
    if batch:
        store.upsert(copy, batch)
    return verify_copy(store, source, copy, extra_ids)


def verify_copy(store: Qdrant, source: str, copy: str, extra_ids: frozenset[str] = frozenset()) -> int:
    source_ids = store.ids(source)
    if set(store.ids(copy)) - extra_ids != set(source_ids):
        raise Refusal(f"E_RETIRE_COPY_UNVERIFIED: {copy} does not hold exactly the points of {source}; nothing deleted")
    return len(source_ids)


def rekey_manifest(store: Qdrant, legacy_control: str, keep_control: str) -> None:
    """Copy the d53595e representation manifest to the id derived from the
    kept control's name, verbatim (payload and vector): the d53595e agent
    then rates the kept generation `compatible`, not `legacy`."""
    target_id = manifest_point_id(keep_control)
    if store.retrieve(keep_control, target_id) is not None:
        return
    record = store.retrieve(keep_control, manifest_point_id(legacy_control))
    if record is None:
        raise Refusal(f"E_RETIRE_NO_MANIFEST: {legacy_control} has no representation manifest; nothing deleted")
    store.upsert(keep_control, [{"id": target_id, "vector": record.get("vector") or {},
                                 "payload": record.get("payload") or {}}])
    if store.retrieve(keep_control, target_id) is None:
        raise Refusal(f"E_RETIRE_NO_MANIFEST: the re-keyed manifest is not readable in {keep_control}")


def _valid_snapshot(entry: dict) -> bool:
    return (isinstance(entry.get("size"), int) and entry["size"] > 0
            and isinstance(entry.get("checksum"), str) and bool(CHECKSUM_RE.fullmatch(entry["checksum"])))


def snapshot_on(peer: Qdrant, name: str) -> dict:
    """A verified snapshot of `name` on this peer: reuse the newest listed one
    with a size and a sha256 checksum, else create one and require that the
    listing shows it with both. Snapshots are node-local and secondary to the
    kept copy."""
    listed = [s for s in peer.snapshots(name) if _valid_snapshot(s)]
    if listed:
        return max(listed, key=lambda s: (s.get("creation_time") or "", s.get("name") or ""))
    created = peer.create_snapshot(name)
    match = [s for s in peer.snapshots(name) if s.get("name") == created.get("name")]
    if not created.get("name") or not match or not _valid_snapshot(match[0]):
        raise Refusal(f"E_RETIRE_SNAPSHOT_UNVERIFIED: snapshot of {name} is not listed with a size and checksum; nothing deleted")
    if created.get("checksum") and created["checksum"] != match[0]["checksum"]:
        raise Refusal(f"E_RETIRE_SNAPSHOT_UNVERIFIED: snapshot of {name} changed between create and list; nothing deleted")
    return match[0]


def _refuse_alias_conflicts(aliases: dict[str, str], names: tuple[str, ...]) -> None:
    for name in names:
        if name in aliases:
            raise Refusal(f"E_RETIRE_ALIAS_CONFLICT: {name} is an alias; never deleted or copied by name")
    targeted = sorted(alias for alias, target in aliases.items() if target in names[:2])
    if targeted:
        raise Refusal("E_RETIRE_ALIAS_CONFLICT: the legacy pair is the target of an alias "
                      f"({', '.join(targeted)}); it is a published generation, not the legacy layout")


def _kept(store: Qdrant, keep: str, keep_control: str) -> bool:
    return (store.exists(keep) and store.exists(keep_control)
            and store.retrieve(keep_control, manifest_point_id(keep_control)) is not None)


def retire(store: Qdrant, peers: list[Qdrant], logical: str, *, no_keep_ok: bool, out=print) -> None:
    control, keep = logical + COMPLETIONS_SUFFIX, logical + KEEP_SUFFIX
    keep_control = keep + COMPLETIONS_SUFFIX
    aliases = store.aliases()
    if aliases.get(logical) == keep:
        # The rolled-back state: unpoint L (the kept copy stays) so this
        # release can publish L again; then report the retired state.
        if store.call("POST", "/collections/aliases",
                      {"actions": [{"delete_alias": {"alias_name": logical}}]}) is not True:
            raise Unreadable("Qdrant alias update was not acknowledged")
        out(f"UNPOINTED alias {logical} (it pointed at the kept copy {keep}, which stays)")
        aliases = store.aliases()
    if logical in aliases:
        out(f"STATE published: alias {logical} -> {aliases[logical]}; nothing to retire (an alias is never deleted)")
        return
    _refuse_alias_conflicts(aliases, (logical, control, keep, keep_control))
    if not store.exists(logical):
        if not store.exists(keep):
            if no_keep_ok:
                out(f"STATE empty: no {logical} and no kept copy; nothing to retire (--no-keep-ok)")
                return
            raise Refusal(f"E_RETIRE_NOTHING_KEPT: {logical} does not exist and no kept copy {keep} exists, "
                          "so no rollback data exists; re-run with --no-keep-ok to accept that")
        if not _kept(store, keep, keep_control):
            raise Refusal(f"E_RETIRE_KEEP_INCOMPLETE: {logical} is gone but {keep}/{keep_control} is incomplete; inspect it")
        if store.exists(control):
            # Crash between the two deletes: finish the pair against the
            # still-present source, re-verified before the delete.
            verify_copy(store, control, keep_control, frozenset({manifest_point_id(keep_control)}))
            store.delete(control)
        _report(store, logical, keep, keep_control, out)
        return
    if not store.exists(control):
        raise Refusal(f"E_RETIRE_PAIR_INCOMPLETE: {logical} exists without {control}; not a d53595e layout, inspect it")
    extra = frozenset({manifest_point_id(keep_control)})
    count = preserve(store, logical, keep)
    out(f"KEPT {keep}: {count} points, ids {ids_digest(store.ids(keep))} (verified equal to {logical})")
    preserve(store, control, keep_control, extra)
    rekey_manifest(store, control, keep_control)
    out(f"KEPT {keep_control}: verified equal to {control}; manifest re-keyed")
    for index, peer in enumerate(peers):
        for name in (logical, control):
            snap = snapshot_on(peer, name)
            out(f"SNAPSHOT peer {index} {name}: {snap['name']} size {snap['size']} sha256 {snap['checksum']}")
    # Immediately before the deletes, re-observe: no alias took the name,
    # and the kept copies still equal the sources.
    _refuse_alias_conflicts(store.aliases(), (logical, control, keep, keep_control))
    verify_copy(store, logical, keep)
    verify_copy(store, control, keep_control, extra)
    if not _kept(store, keep, keep_control):
        raise Refusal(f"E_RETIRE_KEEP_INCOMPLETE: {keep_control} lost its re-keyed manifest; nothing deleted")
    store.delete(logical)
    store.delete(control)
    _report(store, logical, keep, keep_control, out)


def _report(store: Qdrant, logical: str, keep: str, keep_control: str, out) -> None:
    if logical in store.aliases() or store.exists(logical) or store.exists(logical + COMPLETIONS_SUFFIX):
        raise Refusal(f"E_RETIRE_NOT_CONVERGED: {logical} or its control still exists; re-run")
    ids = store.ids(keep)
    out(f"STATE retired: {logical} and {logical + COMPLETIONS_SUFFIX} deleted; "
        f"kept {keep} ({len(ids)} points, ids {ids_digest(ids)}) and {keep_control}")
    out(f"ROLLBACK fast: after redeploying d53595e, retire_legacy.sh --rollback --confirm {logical} "
        f"(one alias call: {logical} -> {keep})")
    out(f"ROLLBACK snapshot: PUT /collections/{logical}/snapshots/recover "
        f'{{"location": "file:///qdrant/snapshots/{logical}/<SNAPSHOT>", "priority": "snapshot"}} '
        "on the peer that holds it, after deleting the alias (node-local, unqualified for 6/3/2)")
    out(f"RUNBOOK {RUNBOOK}")


def rollback(store: Qdrant, logical: str) -> None:
    keep = logical + KEEP_SUFFIX
    keep_control = keep + COMPLETIONS_SUFFIX
    aliases = store.aliases()
    if aliases.get(logical) == keep:
        print(f"STATE rolled back: alias {logical} -> {keep} already")
        return
    if not _kept(store, keep, keep_control):
        raise Refusal(f"E_ROLLBACK_NO_KEEP: {keep}/{keep_control} with the re-keyed manifest is missing; no fast rollback")
    if logical not in aliases and store.exists(logical):
        raise Refusal(f"E_ROLLBACK_NAME_TAKEN: {logical} is a collection, not an alias; nothing changed")
    actions: list[dict] = []
    if logical in aliases:
        actions.append({"delete_alias": {"alias_name": logical}})
    actions.append({"create_alias": {"collection_name": keep, "alias_name": logical}})
    if store.call("POST", "/collections/aliases", {"actions": actions}) is not True:
        raise Unreadable("Qdrant alias update was not acknowledged")
    if store.aliases().get(logical) != keep:
        raise Refusal(f"E_ROLLBACK_NOT_CONVERGED: alias {logical} does not point at {keep}")
    previous = f" (was {aliases[logical]}, kept)" if logical in aliases else ""
    print(f"STATE rolled back: alias {logical} -> {keep}{previous}")


# --- entry ------------------------------------------------------------------


def main(argv: list[str], stdin_key: str) -> int:
    parser = argparse.ArgumentParser(prog="legacy_layout.py")
    parser.add_argument("command", choices=("check", "retire", "rollback"))
    parser.add_argument("--collection", required=True)
    parser.add_argument("--peer-url", action="append", default=[])
    parser.add_argument("--no-keep-ok", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}", args.collection):
        print("FAIL: invalid collection name", file=sys.stderr)
        return EXIT_REFUSED
    url = os.environ.get("QDRANT_URL", "")
    if not url:
        print("FAIL: QDRANT_URL is not set in this pod", file=sys.stderr)
        return EXIT_UNREADABLE
    if args.command == "check":
        key = stdin_key or os.environ.get("QDRANT_API_KEY", "")
    else:
        key = stdin_key
        if not key:
            print("FAIL: the writer key did not arrive on stdin", file=sys.stderr)
            return EXIT_REFUSED
    store = Qdrant(url, key)
    try:
        if args.command == "check":
            print(f"LAYOUT {classify(store, args.collection)}")
        elif args.command == "retire":
            peers = [Qdrant(u, key) for u in args.peer_url] or [store]
            retire(store, peers, args.collection, no_keep_ok=args.no_keep_ok)
        else:
            rollback(store, args.collection)
    except Refusal as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Unreadable as exc:
        if args.command == "check":
            print("LAYOUT unreadable")
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    return 0


if __name__ == "__main__":
    _key = globals().get("QDRANT_KEY_FROM_STDIN")
    if _key is None:
        _key = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else ""
    sys.exit(main(sys.argv[1:], _key))

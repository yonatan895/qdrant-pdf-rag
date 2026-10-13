"""Lifecycle through executable CLI boundaries; never invoke an installed Colab."""
from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import colab as controller

from tests import test_colab_production, test_colab_storage, test_local_gateway_sh

retained = test_colab_storage.retained
original_inputs = test_colab_production.original_inputs
gateway_transport = test_local_gateway_sh.gateway_transport

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "example.invalid/gateway@sha256:" + "a" * 64

# Output and auto-allocation semantics follow google-colab-cli v0.7.4
# commands/session.py, commands/usage.py and commands/ssh.py. The fake SSH
# really binds the four requested local sockets; Docker's host-gateway fake
# probes those sockets rather than declaring them reachable by fiat.
FAKE = r'''
import http.server, json, os, pathlib, shlex, signal, socketserver, subprocess, sys, threading, time
root=pathlib.Path(os.environ['FAKE_COLAB_ROOT'])
with (root/'fake-pids').open('a') as f: f.write(str(os.getpid())+'\n')
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
store=root/'default-cli-store.json'
if name=='colab' and '--config' in args:
    position=args.index('--config')
    store=pathlib.Path(args[position+1])
    args=args[:position]+args[position+2:]
def event(kind, data):
    with (root/'events.jsonl').open('a') as f: f.write(json.dumps([kind,data])+'\n')
event(name,args)
if name=='colab': event('cli-store',[str(store)])
scenario=os.environ.get('FAKE_COLAB_SCENARIO','success')
remote=root/'remote'; remote.mkdir(exist_ok=True)
def path(p): return remote/pathlib.Path(p).name
def diagnostic(value):
    target=path('/content/colab-diagnostics.json')
    temporary=target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary,target)
def mutate_origin():
    source=pathlib.Path(os.environ['FAKE_COLAB_SOURCE_ROOT'])
    names=['scripts/colab_runtime.py','scripts/colab_preflight.py','scripts/run_local_gateway.sh',
           'src/mainframe_rag/serve/colab_profiles.py','src/mainframe_rag/snapshot_probe.py','.tools/bin/task']
    originals={name:(source/name).read_bytes().hex() for name in names}
    (root/'original-source-bytes.json').write_text(json.dumps(originals))
    for name in names:
        (source/name).write_text("raise AssertionError('mutable origin must not execute')\n")
    (source/'src/mainframe_rag/snapshot_probe.py').write_text("VALUE = 'unattributed mutable origin'\n")
    (root/'origin-mutated').touch()
def restore_origin():
    saved=root/'original-source-bytes.json'
    if saved.exists():
        source=pathlib.Path(os.environ['FAKE_COLAB_SOURCE_ROOT'])
        for name,raw in json.loads(saved.read_text()).items(): (source/name).write_bytes(bytes.fromhex(raw))
        saved.unlink()
if name=='colab':
    assert args[:2]==['--auth','oauth2']
    cmd=args[2]; rest=args[3:]
    sessions=root/'sessions.json'
    state=json.loads(sessions.read_text()) if sessions.exists() else {}
    local=json.loads(store.read_text()) if store.exists() else {}
    session=rest[rest.index('-s')+1] if '-s' in rest else None
    if cmd=='version': print('0.7.4')
    elif cmd=='usage':
        reads=root/'usage-reads'
        count=int(reads.read_text()) if reads.exists() else 0
        reads.write_text(str(count+1))
        override=root/'usage-response.json'
        if override.exists():
            response=json.loads(override.read_text())
            if response['mode']=='error': sys.exit(17)
            if response['mode']=='timeout': time.sleep(30)
            if response['mode']=='malformed':
                print('original fake account usage is unavailable'); sys.exit(0)
            print('Current balance: '+str(response['balance'])+' compute units\nUsage rate: 0.00/hr\nActive assignments: '+str(len(state)))
            sys.exit(0)
        balance='175.00' if scenario=='spent_cap_zero_rate' and count else '200.00'
        rate='0.00' if scenario=='spent_cap_zero_rate' else '8.00'
        print('Current balance: '+balance+' compute units\nUsage rate: '+rate+'/hr\nActive assignments: '+str(len(state)))
    elif cmd=='sessions':
        if scenario=='slow_export' and not (root/'first-sessions').exists():
            (root/'first-sessions').touch()
            time.sleep(10)
        if not state: print('[colab] No active sessions found on server.')
        for n,e in state.items():
            shown='?' if os.environ.get('FAKE_UNNAMED_SESSIONS') else next((n for n,v in local.items() if v==e),'?')
            print(f'[{shown}] {e} | Hardware: G4 | Shape: HIGH_RAM | Variant: GPU')
    elif cmd=='new':
        assert session not in state
        for previous in remote.iterdir(): previous.unlink()
        state[session]='endpoint-owned'; sessions.write_text(json.dumps(state))
        local[session]='endpoint-owned'; store.write_text(json.dumps(local))
        if scenario=='origin_mutation': mutate_origin()
        print('[colab] Session READY.')
    elif cmd=='stop':
        if scenario=='stop_fail': sys.exit(1)
        state.pop(session,None); sessions.write_text(json.dumps(state))
        local.pop(session,None); store.write_text(json.dumps(local))
        restore_origin()
        if scenario=='noisy_stop': print('x'*262144)
        print('[colab] Session terminated.')
    elif cmd=='ssh':
        assert '--proxy-mode' in rest
        if session and session not in local:
            # Actual0.7.4 named proxy selection allocates if local name vanished.
            state[session]='endpoint-replacement'; sessions.write_text(json.dumps(state))
            local[session]='endpoint-replacement'; store.write_text(json.dumps(local))
            event('proxy-allocation',[session])
        elif not session:
            live=[n for n,e in local.items() if e in state.values()]
            if len(live)!=1: sys.exit(2)
            session=live[0]
            print("[colab] Using unique session '"+session+"'.")
        if local[session] not in state.values(): sys.exit(2)
    elif cmd=='upload':
        local, target=rest[-2:]; path(target).write_bytes(pathlib.Path(local).read_bytes())
    elif cmd=='download':
        source,target=rest[-2:]
        if scenario=='download_fail' and source.endswith('.tgz'): sys.exit(1)
        pathlib.Path(target).write_bytes(path(source).read_bytes())
    elif cmd=='exec':
        file=rest[rest.index('-f')+1]
        assert pathlib.Path(file).is_file(), 'fake must not hide a missing runtime source'
        if file.endswith('colab_preflight.py'):
            report={'gpu_name':'NVIDIA RTX PRO 6000 Blackwell Server Edition','vram_mib':98304,
                    'available_ram_mib':131072,'free_disk_mib':200000,'driver_version':'580.100','admitted':True}
            if scenario=='bad_gpu': report.update(gpu_name='NVIDIA A100-SXM4-40GB',vram_mib=40960)
            path('/content/colab-preflight.json').write_text(json.dumps(report))
        else:
            diagnostic({'stage':'transport_ready'})
            lifetime=min(30,json.loads(path('/content/model-profile.json').read_text())['deadline_seconds'])
            assert type(lifetime) is int and lifetime>0
            until=time.monotonic()+lifetime
            while not path('/content/colab-stop').exists() and time.monotonic()<until:
                if path('/content/colab-transport-ok').exists():
                    report=root/'original-runtime-observed.json'
                    observed=json.loads(report.read_text()) if report.exists() else {}
                    diagnostic({**observed,'stage':'ready'})
                time.sleep(.02)
            path('/content/colab-artifacts.tgz').write_bytes(b'private-fake-diagnostics')
    else: raise AssertionError('unsupported real CLI command: '+cmd)
elif name=='ssh':
    assert '-N' in args and args.count('-L')==4
    configuration=pathlib.Path(args[args.index('-F')+1]).read_text()
    proxy=shlex.split(next(line.strip().removeprefix('ProxyCommand ') for line in configuration.splitlines()
                           if line.strip().startswith('ProxyCommand ')))
    if scenario=='proxy_session_pruned':
        (root/'sessions.json').write_text('{}')
        proxy_store=pathlib.Path(proxy[proxy.index('--config')+1]) if '--config' in proxy else root/'default-cli-store.json'
        proxy_store.unlink(missing_ok=True)
    result=subprocess.run(proxy,check=False)
    if result.returncode: sys.exit(result.returncode)
    lifetime=min(30,json.loads(path('/content/model-profile.json').read_text())['deadline_seconds'])
    assert type(lifetime) is int and lifetime>0
    until=time.monotonic()+lifetime
    startup=float(os.environ.get('FAKE_SSH_START_SECONDS','0'))
    assert 0<=startup<=13, 'fake SSH startup must remain bounded'
    time.sleep(startup)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
        def log_message(self,*args): pass
    class Server(http.server.ThreadingHTTPServer): allow_reuse_address=True
    servers=[]
    for i,arg in enumerate(args):
        if arg=='-L':
            host,port,_,_=args[i+1].split(':')
            server=Server((host,int(port)),Handler); servers.append(server)
    # SSH admits every requested forward before serving traffic. Starting a
    # listener early let the fake expose health before its final bridge bind.
    for server in servers:
        threading.Thread(target=server.serve_forever,daemon=True).start()
    if scenario=='tunnel_loss': time.sleep(.5); sys.exit(7)
    while time.monotonic()<until: time.sleep(.1)
elif name=='docker':
    networks_file=root/'gateway-networks.json'
    networks=json.loads(networks_file.read_text()) if networks_file.exists() else {}
    if args[:2]==['network','inspect']:
        target=args[2]
        if target=='bridge' and 'IPAM' in args[-1]: print('127.0.0.2')
        else:
            if target not in networks: sys.exit(1)
            print(networks[target]+' bridge')
    elif args[:2]==['network','create']:
        target=args[2]
        if target in networks: sys.exit(1)
        sequence=root/'gateway-network-sequence'
        number=int(sequence.read_text())+1 if sequence.exists() else 1
        sequence.write_text(str(number))
        networks[target]=f'{number:064x}'; networks_file.write_text(json.dumps(networks))
        print(networks[target])
    elif args[:2]==['network','ls']:
        expected=args[args.index('--filter')+1].removeprefix('id=')
        for identity in networks.values():
            if identity==expected: print(identity)
    elif args[:2]==['network','rm']:
        if scenario=='gateway_network_release_fail': sys.exit(23)
        if scenario=='gateway_network_release_hang': time.sleep(30)
        names=[key for key,identity in networks.items() if identity==args[2]]
        if not names: sys.exit(1)
        for key in names: del networks[key]
        networks_file.write_text(json.dumps(networks))
    elif args[0]=='image': print('sha256:'+'a'*64)
    elif args[0]=='ps':
        assert args[1:3]==['-aq','--no-trunc']
        assert args[3]=='--filter' and args[4].startswith('label=io.mainframe-rag.gateway-run=')
    elif args[0]=='run':
        network=args[args.index('--network')+1]
        if network!='bridge':
            assert network in networks.values()
            if scenario=='gateway_network_fail': sys.exit(19)
            if scenario=='gateway_network_probe_hang': time.sleep(30)
        code_at=args.index('-c')+1
        code=args[code_at].replace('host.docker.internal','127.0.0.2')
        subprocess.run([sys.executable,'-c',code,*args[code_at+1:]],check=True)
    else: raise AssertionError('unsupported Docker command')
elif name=='kubectl':
    assert args[:1]==['--kubeconfig'] and args[2]=='--context' and args[4]=='--namespace'
    rest=args[6:]
    fixture=json.loads((root/'storage-fixture.json').read_text())
    if rest[0]=='get':
        kind=rest[1]
        if kind=='pods':
            assert rest[2:]==['-l','app=qdrant','-o','json']
            output={'items':[fixture['pod']]}
        else:
            key={'service':'service','pod':'pod','pvc':'pvc','pv':'pv',
                 'storageclass':'class','node':'node'}[kind]
            output=fixture[key]
            assert rest[2]==output['metadata']['name'] and rest[3:]==['-o','json']
        if kind=='pod':
            changed=os.environ.get('FAKE_QDRANT_CHANGE')
            status=output['status']['containerStatuses'][0]
            if changed=='uid': output['metadata']['uid']='pod-replacement'
            elif changed=='container': status['containerID']='containerd://replacement'
            elif changed=='image': status['imageID']='sha256:replacement'
            elif changed=='ready': status['ready']=False
            if (root/'fresh-producer-replaced').exists(): output['metadata']['uid']='fresh-replacement'
        print(json.dumps(output))
    elif rest[0]=='exec':
        assert rest[1:6]==['qdrant-0','-c','qdrant','--','cat']
        assert rest[6] in fixture['files'] and len(rest)==7
        print(fixture['files'][rest[6]],end='')
        print('ORIGINAL-FAKE-CONFIG-STDERR',file=sys.stderr)
    elif rest[0]=='port-forward':
        assert rest[:4]==['port-forward','--address','127.0.0.1','pod/qdrant-0']
        local,remote_port=rest[4].split(':'); assert remote_port=='6333'
        if os.environ.get('FAKE_FRESH_API')=='resist-cleanup':
            signal.signal(signal.SIGTERM,lambda *_: (root/'fresh-forward-term').touch())
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/readyz':
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ok'); return
                event('qdrant-read',['GET',self.path])
                assert self.path in ('/collections','/aliases')
                slow=os.environ.get('FAKE_FRESH_API')
                if self.path=='/collections' and slow in ('slow-headers','slow-chunk'):
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ' if slow=='slow-headers' else
                                     b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
                    (root/'fresh-slow-started').touch()
                    while True:
                        self.wfile.write(b'a'); self.wfile.flush(); time.sleep(.05)
                inventory=root/'fresh-qdrant.json'
                values=json.loads(inventory.read_text()) if inventory.exists() else {
                    '/collections': {'collections':[{'name':'legacy1024'}]}, '/aliases': {'aliases':[]}}
                payload=json.dumps({'status':'ok','result':values[self.path]}).encode()
                if os.environ.get('FAKE_FRESH_API')=='oversized': payload=b'x'*65537
                self.send_response(200); self.end_headers(); self.wfile.write(payload)
                if self.path=='/aliases' and os.environ.get('FAKE_FRESH_API')=='replace-producer':
                    (root/'fresh-producer-replaced').touch()
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                event('qdrant-read',['POST',self.path,body])
                assert self.path.endswith('__completions/points')
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok","result":[]}')
            def log_message(self,*args): pass
        class Server(http.server.HTTPServer): allow_reuse_address=True
        Server(('127.0.0.1',int(local)),Handler).serve_forever()
    else: raise AssertionError('unsupported kubectl command')
else: raise AssertionError(name)
'''


def _reference_lock():
    # Original fake wheel hashes: syntax/identity fixtures, never install inputs.
    return "\n".join(f"{name}=={version} --hash=sha256:" + "0" * 64
                     for name, version in (("vllm", "0.28.0"), ("transformers", "5.15.0"),
                                           ("torch", "2.11.0"), ("huggingface-hub", "1.0.0"))) + "\n"


def _controller_call(argv, *, source_root=None):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    previous_umask = os.umask(0o077)
    try:
        return controller.main(argv, bootstrap=False, source_root=source_root)
    finally:
        os.umask(previous_umask)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _prepare_arguments(source_state, state, *, profile="COLAB_CPU_TRANSPORT", extra=()):
    original = json.loads((source_state / "manifest.json").read_text())
    config, tools = original["config"], original["tools"]
    argv = ["prepare", "--state", str(state), "--dataset", "synthetic", "--profile", profile,
            "--gateway-image", config["gateway_image"], "--deadline-seconds", "30",
            "--reasoning-port", str(config["reasoning_port"]), "--embedding-port", str(config["embedding_port"])]
    for name in ("colab", "ssh", "ssh_keygen", "docker"):
        argv += ["--" + name.replace("_", "-"), tools[name]]
    return argv + list(extra), Path(original["origin"]["source_root"])


def _prepare_new(source_state, state, *, profile="COLAB_CPU_TRANSPORT", extra=()):
    argv, source = _prepare_arguments(source_state, state, profile=profile, extra=extra)
    assert _controller_call(argv, source_root=source) == 0
    return state


def _run_fake(state, workload=("/bin/true",)):
    return _controller_call(["run", "--state", str(state), "--approve-cu", "20", "--", *workload])


def _events(root):
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _seed_owned_assignment(run, root, *, unrelated=None):
    owned = {run.manifest["session"]: "endpoint-owned"}
    (root / "sessions.json").write_text(json.dumps({**owned, **(unrelated or {})}))
    (run.state / "colab-sessions.json").write_text(json.dumps(owned))


def _allocated_cleanup_run(lab):
    """Actual pinned CLI creation/ownership, without model or transport setup."""
    root, state, _ = lab
    run = controller.Run(state)
    run.expiry = time.time() + 30
    run.work_expiry = run.expiry - 5
    run.cleanup_reserve = 5
    with controller.state_lock(state, recovery=False):
        run.budget(initial=True)
        run.manifest.update(allocation_attempted=True, phase="allocating")
        run.save("allocation_attempted")
        run.colab("new", "-s", run.manifest["session"])
        assert run.owned()
        run.budget()
        assert run.manifest["cu_summary"]["last"]["balance"] == 200.0
        run.manifest["phase"] = "running"
        run.save("original_fake_owned_runtime")
    (root / "remote" / "colab-artifacts.tgz").write_bytes(b"original-fake-diagnostics")
    return run


def test_cleanup_observes_usage_after_last_work_poll_before_exact_release_then_next_run_works(lab):
    root, state, _ = lab
    run = _allocated_cleanup_run(lab)
    (root / "usage-response.json").write_text(json.dumps({"mode": "available", "balance": 197.0}))
    assert run.cleanup() is True
    manifest = json.loads((state / "manifest.json").read_text())
    final = manifest["final_cu_observation"]
    assert final["status"] == "available"
    assert final["balance"] == 197.0 and final["rate_per_hour"] == 0.0 and final["assignments"] == 1
    assert final["spent_since_start"] == 3.0
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    calls = [(kind, argv) for kind, argv in _events(root) if kind == "colab"]
    usage = [index for index, (_, argv) in enumerate(calls) if argv[2] == "usage"]
    stop = next(index for index, (_, argv) in enumerate(calls) if argv[2] == "stop")
    assert len(usage) == 3 and usage[-1] < stop
    assert calls[stop][1][-1] == run.manifest["session"]
    (root / "usage-response.json").unlink()
    ordinary = _prepare_new(state, root / "ordinary-after-final-usage")
    assert _run_fake(ordinary) == 0


@pytest.mark.parametrize("endpoint_bound", [True, False])
def test_orphan_assignment_cannot_establish_release_then_named_recovery_allows_next_run(lab, monkeypatch,
                                                                                       endpoint_bound):
    root, state, _ = lab
    run = _allocated_cleanup_run(lab)
    if not endpoint_bound:
        # Backend allocation can finish before CLI persistence/parent binding.
        run.manifest["endpoint"] = None
        run.save("allocation_result_unknown")
    monkeypatch.setenv("FAKE_UNNAMED_SESSIONS", "1")
    assert run.cleanup() is False
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is False
    assert manifest["phase"] == "cleanup_required"
    assert not manifest.get("release_evidence", {}).get("absent")
    assert json.loads((root / "sessions.json").read_text()) == {run.manifest["session"]: "endpoint-owned"}
    assert not any(kind == "colab" and argv[2] == "stop" for kind, argv in _events(root))

    # Independent CLI naming restoration makes exact recovery possible. Never
    # infer a writable name from an orphan's endpoint or stop a foreign VM.
    monkeypatch.delenv("FAKE_UNNAMED_SESSIONS")
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert json.loads((root / "sessions.json").read_text()) == {}
    ordinary = _prepare_new(state, root / "ordinary-after-orphan-recovery")
    assert _run_fake(ordinary) == 0


def test_unbound_allocation_with_empty_listing_remains_unverified(lab):
    root, state, _ = lab
    run = controller.Run(state)
    run.expiry = time.time() + 10
    run.manifest["allocation_attempted"] = True
    run.save("allocation_result_unknown")
    assert run.cleanup() is False
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is False
    assert manifest["phase"] == "cleanup_required"
    assert not manifest.get("release_evidence", {}).get("absent")
    assert not any(kind == "colab" and argv[2] == "stop" for kind, argv in _events(root))


def test_generated_ssh_proxy_never_reallocates_pruned_session_then_next_run_works(lab, monkeypatch):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "proxy_session_pruned")
    assert _run_fake(state) != 0
    assert not any(kind == "proxy-allocation" for kind, _ in _events(root))
    assert json.loads((root / "sessions.json").read_text()) == {}
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert not (state / "transport-receipt.json").exists()
    monkeypatch.delenv("FAKE_COLAB_SCENARIO")
    ordinary = _prepare_new(state, root / "ordinary-after-pruned-proxy")
    assert _run_fake(ordinary) == 0
    stores = {argv[0] for kind, argv in _events(root) if kind == "cli-store"}
    assert stores == {str(state / "colab-sessions.json"), str(ordinary / "colab-sessions.json")}


def test_fresh_run_refuses_preexisting_private_cli_store_without_any_cloud_command(lab):
    root, state, _ = lab
    (state / "colab-sessions.json").write_text(json.dumps({"foreign": "endpoint-foreign"}))
    assert _run_fake(state) == 2
    assert _events(root) == []
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False


@pytest.mark.parametrize("failure", ["error", "malformed", "timeout"])
def test_unavailable_final_usage_is_explicit_and_cannot_block_exact_release_then_next_run(lab, failure):
    root, state, _ = lab
    run = _allocated_cleanup_run(lab)
    (root / "usage-response.json").write_text(json.dumps({"mode": failure}))
    started = time.monotonic()
    assert run.cleanup() is False
    assert time.monotonic() - started < 9
    manifest = json.loads((state / "manifest.json").read_text())
    final = manifest["final_cu_observation"]
    assert final["status"] == "unavailable"
    assert final["reason"] == "usage_unavailable"
    assert "balance" not in final and "spent_since_start" not in final
    assert "original fake" not in json.dumps(final)
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    calls = [(kind, argv) for kind, argv in _events(root) if kind == "colab"]
    stops = [argv[-1] for _, argv in calls if argv[2] == "stop"]
    assert stops == [run.manifest["session"]]
    assert sum(argv[2] == "usage" for _, argv in calls) == 3
    (root / "usage-response.json").unlink()
    ordinary = _prepare_new(state, root / "ordinary-after-unavailable-final-usage")
    assert _run_fake(ordinary) == 0


@pytest.mark.parametrize("available", [True, False])
def test_manual_recovery_records_final_usage_or_explicit_unavailable_and_still_releases(lab, available):
    root, state, _ = lab
    run = _allocated_cleanup_run(lab)
    response = {"mode": "available", "balance": 196.0} if available else {"mode": "error"}
    (root / "usage-response.json").write_text(json.dumps(response))
    assert _controller_call(["stop", "--state", str(state)]) == (0 if available else 1)
    manifest = json.loads((state / "manifest.json").read_text())
    final = manifest["final_cu_observation"]
    assert final["status"] == ("available" if available else "unavailable")
    if available:
        assert final["balance"] == 196.0 and final["spent_since_start"] == 4.0
    else:
        assert final["reason"] == "usage_unavailable" and "balance" not in final
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    stops = [argv[-1] for kind, argv in _events(root) if kind == "colab" and argv[2] == "stop"]
    assert stops == [run.manifest["session"]]
    (root / "usage-response.json").unlink()
    ordinary = _prepare_new(state, root / "ordinary-after-final-usage-recovery")
    assert _run_fake(ordinary) == 0


def test_successful_workload_with_unavailable_final_usage_has_no_cpu_receipt_then_next_run_works(lab, monkeypatch):
    root, state, _ = lab
    cleanup = controller.Run.cleanup

    def fail_only_final_account_observation(self):
        response = root / "usage-response.json"
        response.write_text(json.dumps({"mode": "error"}))
        try:
            return cleanup(self)
        finally:
            response.unlink()

    monkeypatch.setattr(controller.Run, "cleanup", fail_only_final_account_observation)
    assert _run_fake(state) == 1
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["workload_succeeded"] is True
    assert manifest["final_cu_observation"]["status"] == "unavailable"
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert not (state / "transport-receipt.json").exists()
    assert json.loads((root / "sessions.json").read_text()) == {}
    stops = [argv[-1] for kind, argv in _events(root) if kind == "colab" and argv[2] == "stop"]
    assert stops == [manifest["session"]]
    monkeypatch.setattr(controller.Run, "cleanup", cleanup)
    ordinary = _prepare_new(state, root / "ordinary-after-unavailable-final-accounting")
    assert _run_fake(ordinary) == 0
    assert (ordinary / "transport-receipt.json").is_file()


@pytest.mark.parametrize("interruption,created", [("terminate", True), ("terminate", False), ("recovery", True)])
def test_native_gateway_pg_readiness_interruption_and_recovery_preserve_foreign_objects_then_next_serve(
    lab, gateway_transport, monkeypatch, interruption, created,
):
    root, original, _ = lab
    gateway_root, gateway_env, urls, _ = gateway_transport
    for key, value in gateway_env.items():
        monkeypatch.setenv(key, value)
    # The exact probed endpoints remain tiny loopback handlers. The prepared
    # controller uses the same explicit Docker owner and configuration.
    ports = [url.rsplit(":", 1)[1].split("/", 1)[0] for url in urls]
    state = _prepare_new(original, root / "native-owned-cleanup", extra=(
        "--docker", gateway_env["GATEWAY_DOCKER_BIN"], "--reasoning-port", ports[0],
        "--embedding-port", ports[1],
    ))
    run = _allocated_cleanup_run((root, state, None))
    foreign = {"f" * 64: {"id": "f" * 64, "name": "unrelated-existing-postgres",
                            "network": test_local_gateway_sh.BRIDGE_ID,
                            "labels": {"io.mainframe-rag.gateway-run": "rag-" + "b" * 16}}}
    container_file = gateway_root / "containers.json"
    container_file.write_text(json.dumps(foreign))
    network_file = gateway_root / "networks.json"
    if not created:
        networks = json.loads(network_file.read_text())
        networks[run.config["gateway_network"]] = {
            "name": run.config["gateway_network"], "id": "e" * 64, "driver": "bridge"}
        network_file.write_text(json.dumps(networks))
    run.probe_gateway_network("127.0.0.2")
    ownership = run.manifest["gateway_network"]
    assert ownership["created"] is created
    env = run.gateway_environment("127.0.0.2")
    env["FAKE_GATEWAY_SCENARIO"] = "postgres-notready"
    child = run.spawn(["sh", str(run.execution_root / "scripts/run_local_gateway.sh")], "native-gateway", env=env)
    run.workload = child
    try:
        entered = gateway_root / "pg-readiness-entered"
        until = time.monotonic() + 5
        while not entered.exists() and child.poll() is None and time.monotonic() < until:
            time.sleep(.01)
        assert entered.exists(), "native shell never reached daemon PG readiness"
        owned_id = entered.read_text()
        containers = json.loads(container_file.read_text())
        assert containers[owned_id]["name"] == run.config["gateway_name"] + "-pg"
        assert child.poll() is None
        if interruption == "terminate":
            # Exercise the real controller's TERM then finite KILL policy,
            # rather than manually invoking a new cleanup helper.
            run.terminate(child)
            assert json.loads(container_file.read_text()) == foreign, "owned daemon PG survived controller TERM"
            assert run.cleanup() is True
        else:
            # A dead controller cannot run a trap. Recovery has only retained
            # run ownership, not this child's in-memory PID or Docker ID.
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)
            run.terminate(child)
            assert owned_id in json.loads(container_file.read_text())
            assert _controller_call(["stop", "--state", str(state)]) == 0
    finally:
        run.terminate(child)
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert manifest["gateway_network_cleanup"]["completed"] is True
    assert json.loads(container_file.read_text()) == foreign
    networks = json.loads(network_file.read_text())
    if created:
        assert set(networks) == {"bridge"}
    else:
        assert len(networks) == 2
        assert networks[test_local_gateway_sh.TRANSPORT_NETWORK]["id"] == ownership["network_id"]
    calls = test_local_gateway_sh._transport_calls(gateway_transport)
    deleted = [argv[-1] for argv in calls if argv[0] in ("stop", "rm")]
    assert owned_id in deleted and "f" * 64 not in deleted
    assert "unrelated-existing-postgres" not in deleted
    assert not any(argv[0] == "volume" for argv in calls)
    # The next normal native operation must pass its real existing-name check.
    next_probe = test_local_gateway_sh._transport_operation(gateway_transport)
    assert next_probe.returncode == 0, next_probe.stderr
    next_identity = json.loads(next_probe.stdout)
    next_serve = test_local_gateway_sh._transport_operation(
        gateway_transport, "serve", GATEWAY_RUN_OWNER="rag-" + "c" * 16,
        GATEWAY_TRANSPORT_NETWORK_ID=next_identity["network_id"],
        GATEWAY_TRANSPORT_NETWORK_CREATED="1" if next_identity["created"] else "0",
    )
    assert next_serve.returncode == 0, next_serve.stderr
    assert json.loads(container_file.read_text()) == foreign
    assert (gateway_root / "rendered-gateway.yaml").is_file()


def _production_fixture_paths():
    # Only the native admission/import/recipe closure, with explicit finite
    # fixture bounds. No checkout, environment, corpus or model-tree copy.
    names = {
        "mainframe_rag.serve.colab_production", "mainframe_rag.serve.colab_corpus",
        "mainframe_rag.ingest.run_ingest", "mainframe_rag.ingest.ibm_pdf",
        "mainframe_rag.ingest.chrome", "mainframe_rag.ingest.classify",
        "mainframe_rag.ingest.identity", "mainframe_rag.ingest.payload",
    }
    pending = list(names)
    files = set()
    while pending:
        name = pending.pop()
        base = ROOT / "src" / name.replace(".", "/")
        path = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
        if not path.is_file() or path in files:
            continue
        files.add(path)
        assert len(files) <= 128 and sum(item.stat().st_size for item in files) < 4 * 1024 * 1024
        for directory in path.parents:
            if directory == ROOT / "src":
                break
            initializer = directory / "__init__.py"
            if initializer.is_file():
                module = initializer.parent.relative_to(ROOT / "src").as_posix().replace("/", ".")
                if module not in names:
                    names.add(module)
                    pending.append(module)
        for node in ast.walk(ast.parse(path.read_bytes())):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imports = [node.module, *(node.module + "." + alias.name for alias in node.names)]
            for module in imports:
                if module.startswith("mainframe_rag.") and module not in names:
                    names.add(module)
                    pending.append(module)
    return {path.relative_to(ROOT).as_posix() for path in files} | {
        "src/mainframe_rag/retrieve/acronyms_v1.json",
    }


def _execution_fixture(root, *, production=False):
    # Copy actual bounded owner code only, with an original tiny pinned Task;
    # never duplicate the checkout, site-packages or the 52MiB real Task binary.
    source = root / "original-execution-source"
    paths = set(controller.SOURCE_REQUIRED) | {
        "scripts/tools/task-pin.txt", "src/mainframe_rag/__init__.py",
        "src/mainframe_rag/serve/__init__.py", "src/mainframe_rag/serve/colab_storage.py",
        "src/mainframe_rag/serve/budget.py", "src/mainframe_rag/serve/profiles.py",
        "scripts/gateway/strict_finish.py", "scripts/gateway/scoped_passthrough.py",
        "scripts/gateway/trace_privacy.py", "scripts/gateway/log_capture.py",
    }
    paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "taskfiles").iterdir() if path.is_file())
    if production:
        paths.update(_production_fixture_paths())
    assert sum((ROOT / name).stat().st_size for name in paths) < 4 * 1024 * 1024
    for relative in paths:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    task = source / ".tools/bin/task"
    task.parent.mkdir(parents=True)
    task.write_text(f"#!{sys.executable}\n" + '''
import json,os,pathlib,subprocess,sys
assert sys.argv[1:3]==['--offline','--dir'] and sys.argv[4]=='--taskfile'
assert pathlib.Path(sys.argv[3])==pathlib.Path.cwd()
assert pathlib.Path(sys.argv[5]).parent==pathlib.Path.cwd().parents[1]
if '--summary' in sys.argv: sys.exit(0)
if 'local:stack' in sys.argv:
    os.chdir(pathlib.Path(sys.argv[5]).parent)
    code="""
import json,os,time
from pathlib import Path
import mainframe_rag.snapshot_probe as p
observed={'value':p.VALUE,'module':p.__file__,'cwd':str(Path.cwd()),'python':os.environ['PY'],
          'origin_changed':Path(os.environ['FAKE_COLAB_ROOT'],'origin-mutated').exists()}
if 'PRODUCTION_CORPUS_DIR' in os.environ:
    from mainframe_rag.config import Settings
    from mainframe_rag.ingest.completion import representation_fingerprint
    from mainframe_rag.ingest.rules_version import extraction_rules_version
    settings=Settings(_env_file=None)
    names=('CORPUS_DIR','PRODUCTION_CORPUS_DIR','PRODUCTION_GOLDEN_PATH','LOCAL_INGEST_PROGRESS',
           'LOCAL_STACK_DRYRUN','QDRANT_URL','PYTHONPATH','GATEWAY_EMBED_MODEL','GATEWAY_EMBED_MODEL_REVISION')
    observed['production']={name:os.environ[name] for name in names}
    observed['production'].update(collection=settings.qdrant_collection,prefix=settings.dense_query_prefix,
        context_model=settings.context_llm_model,bm25_cache=settings.bm25_cache_dir,
        representation=representation_fingerprint(settings,extraction_rules_version()))
Path(os.environ['FAKE_COLAB_ROOT'],'snapshot-workload.json').write_text(json.dumps(observed))
time.sleep(float(os.environ.get('FAKE_NATIVE_WORKLOAD_SECONDS','0')))
"""
    subprocess.run([os.environ['PY'],'-c',code],check=True)
''')
    task.chmod(0o755)
    pin = source / "scripts/tools/task-pin.txt"
    pin.write_text("binary-sha256: " + hashlib.sha256(task.read_bytes()).hexdigest() + "\n")
    (source / "src/mainframe_rag/snapshot_probe.py").write_text("VALUE = 'original frozen fixture'\n")
    return source


@pytest.fixture
def lab(tmp_path, monkeypatch, request):
    source = _execution_fixture(tmp_path, production="storage_lab" in request.fixturenames)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("colab", "ssh", "docker"):
        executable = binaries / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE)
        executable.chmod(0o755)
    monkeypatch.setenv("FAKE_COLAB_ROOT", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(ROOT / "src"), str(ROOT))))
    monkeypatch.delenv("GATEWAY_NAME", raising=False)
    monkeypatch.delenv("PG_NET", raising=False)
    monkeypatch.setenv("GATEWAY_DRYRUN", "0")
    monkeypatch.setenv("FAKE_COLAB_SOURCE_ROOT", str(source))
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        reasoning_port = str(first.getsockname()[1])
        embedding_port = str(second.getsockname()[1])
    state = tmp_path / "private-run"
    command = [sys.executable, str(ROOT / "scripts/colab.py")]
    profile = getattr(request, "param", "COLAB_CPU_TRANSPORT")
    common = ["--dataset", "synthetic", "--gateway-image", IMAGE,
              "--colab", str(binaries / "colab"), "--ssh", str(binaries / "ssh"),
              "--docker", str(binaries / "docker"), "--reasoning-port", reasoning_port,
              "--embedding-port", embedding_port, "--deadline-seconds", "30"]
    prerequisites = []
    if profile != "COLAB_CPU_TRANSPORT":
        cpu = tmp_path / "cpu-proof"
        assert _controller_call(["prepare", "--state", str(cpu), "--profile", "COLAB_CPU_TRANSPORT"]
                                + common, source_root=source) == 0
        proof = subprocess.run(command + ["run", "--state", str(cpu), "--approve-cu", "20", "--", "/bin/true"],
                               capture_output=True, text=True, timeout=15, check=False)
        assert proof.returncode == 0, proof.stdout + proof.stderr
        inference_lock = tmp_path / "inference.lock"
        inference_lock.write_text(_reference_lock())
        prerequisites = ["--inference-lock", str(inference_lock),
                         "--transport-receipt", str(cpu / "transport-receipt.json")]
        (tmp_path / "events.jsonl").write_text("")
    assert _controller_call(["prepare", "--state", str(state), "--profile", profile] + common + prerequisites,
                            source_root=source) == 0
    try:
        yield tmp_path, state, command
    finally:
        ledger = tmp_path / "fake-pids"
        if ledger.exists():
            for pid in {int(line) for line in ledger.read_text().splitlines()}:
                try:
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if str(binaries).encode() in argv:
                        os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except FileNotFoundError:
                    pass
        # Restore only test-owned snapshot directories for bounded fixture
        # cleanup after their children are gone. Source files remain read-only.
        for execution in tmp_path.glob("*/execution"):
            for directory, children, _ in os.walk(execution):
                Path(directory).chmod(0o700)
                for name in children:
                    (Path(directory) / name).chmod(0o700)


@pytest.fixture
def storage_lab(lab, retained, original_inputs, monkeypatch):
    root, base, command = lab
    original = json.loads((base / "manifest.json").read_text())
    source = Path(original["origin"]["source_root"])
    kubectl = root / "bin/kubectl"
    kubectl.write_text(f"#!{sys.executable}\n" + FAKE)
    kubectl.chmod(0o755)
    (root / "storage-fixture.json").write_text(json.dumps(retained))
    config, _, arguments, evidence = original_inputs
    config["namespace"] = "retained"
    profile = controller.build_profile("COLAB_G4_BF16", deadline_seconds=30)
    arguments["recipe_root"] = source
    pins = {"vllm": "0.28.0", "transformers": "5.15.0", "torch": "2.11.0",
            "tokenizers": "0.0.0+originalfixture", "huggingface-hub": "1.0.0"}
    arguments["runtime_pins"] = pins
    evidence["runtime"] = {name: pins[name] for name in ("vllm", "transformers", "tokenizers", "torch")}
    evidence["provenance"]["runtime_sha256"] = test_colab_production._digest(evidence["runtime"])
    test_colab_production._json(Path(config["embedding_evidence"]) / "manifest.json", evidence)
    fields = ("embed_mode", "embed_model", "embed_model_revision", "dense_dim", "qdrant_collection",
              "bm25_model", "bm25_weights_revision", "bm25_cache_dir", "dense_query_prefix",
              "acronym_expansion_enabled", "comparative_split_enabled", "diagnostic_dualpath_enabled",
              "contextual_embed_enabled", "context_llm_model", "context_max_chars", "embed_max_input_chars",
              "ingest_max_pdf_bytes", "ingest_max_doc_pages", "ingest_max_doc_chunks", "ingest_alias_publish",
              "query_max_chars")
    for name in tuple(os.environ):
        if name.casefold() in fields:
            monkeypatch.delenv(name)
    for name in fields:
        value = getattr(arguments["settings"], name)
        if value is not None:
            monkeypatch.setenv(name.upper(), str(value).lower() if type(value) is bool else str(value))
    # The existing complete factory checks actual original PDF/artifact bytes
    # and native query producers; it never substitutes admission success.
    expected, _, _ = test_colab_production._complete((config, profile, arguments, evidence))
    target = {"namespace": "retained", "claim_uid": "claim-original", "pv_uid": "pv-original-uid",
              "storage_path": "/qdrant/storage", "logical_collection": "original_manuals"}
    ledger = Path(config["data_state"]) / "retained-index-binding.json"
    stored = json.loads(ledger.read_text())
    stored["target"] = target
    test_colab_production._json(ledger, stored)
    approval = root / "original-production-approval.json"
    test_colab_production._json(approval, {
        "schema": 1, "pooling_producer": arguments["pooling_producer"],
        "census_producer": {"development_sha": "6" * 40, "source_sha256": "7" * 64},
        "source_scan": {"max_entries": 100, "max_bytes": 32 * 1024, "deadline_seconds": 20},
    })
    inference = root / "original-production-inference.lock"
    inference.write_text("\n".join(f"{name}=={version} --hash=sha256:" + "0" * 64
                                   for name, version in pins.items()) + "\n")
    runtime = {"software": evidence["runtime"], "models": [{
        "model_id": profile["servers"][1]["model_id"], "resolved_revision": profile["servers"][1]["revision"],
        "server_argv_sha256": test_colab_production._digest(profile["servers"][1]["argv"]),
        "metadata_sha256": {name: row["sha256"] for name, row in evidence["files"].items()},
    }]}
    test_colab_production._json(root / "original-runtime-observed.json", runtime)
    proof = subprocess.run(command + ["run", "--state", str(base), "--approve-cu", "20", "--", "/bin/true"],
                           capture_output=True, text=True, timeout=15, check=False)
    assert proof.returncode == 0, proof.stdout + proof.stderr
    (root / "events.jsonl").write_text("")
    kubeconfig = root / "original-kubeconfig"
    kubeconfig.write_text("original fake Kubernetes client input\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    state = _prepare_new(base, root / "retained-qdrant-run", profile="COLAB_G4_BF16", extra=[
        "--dataset", "production", "--corpus", config["corpus"], "--golden", config["golden"],
        "--inventory", config["inventory"], "--data-state", config["data_state"],
        "--source-inventory", config["source_inventory"], "--embedding-evidence", config["embedding_evidence"],
        "--production-approval", str(approval), "--inference-lock", str(inference),
        "--transport-receipt", str(base / "transport-receipt.json"),
        "--kubeconfig", str(kubeconfig), "--kube-context", "original-kind", "--namespace", "retained",
        "--qdrant-service", "qdrant", "--kubectl", str(kubectl), "--qdrant-port", str(port),
    ])
    run = controller.Run(state)
    assert run.manifest["production"]["summary"]["source_count"] == 2
    assert run.production_admission().environment["QDRANT_COLLECTION"] == "original_manuals"
    assert expected.query_input_counts == (1, 2)
    run.cleanup_reserve = 10
    try:
        yield root, run, retained
    finally:
        for child in reversed(run.children):
            run.terminate(child)


def _prepare_production_again(run, name):
    extra = []
    for field in ("corpus", "golden", "inventory", "source_inventory", "embedding_evidence", "production_approval",
                  "data_state", "kubeconfig", "kube_context", "namespace", "qdrant_service",
                  "inference_lock", "transport_receipt", "qdrant_port"):
        extra += ["--" + field.replace("_", "-"), str(run.config[field])]
    extra += ["--dataset", "production", "--kubectl", run.tools["kubectl"]]
    return _prepare_new(run.state, run.state.parent / name, profile="COLAB_G4_BF16", extra=extra)


def _fresh_cli(run, name="fresh-initialization", *, extra=()):
    argv = ["initialize-fresh", "--state", str(run.state.parent / name), "--collection", "original_manuals",
            "--profile", "COLAB_G4_BF16", "--deadline-seconds", "30", "--kubectl", run.tools["kubectl"],
            "--qdrant-port", str(run.config["qdrant_port"])]
    for field in ("corpus", "golden", "inventory", "source_inventory", "embedding_evidence", "production_approval",
                  "data_state", "kubeconfig", "kube_context", "namespace", "qdrant_service", "inference_lock"):
        argv += ["--" + field.replace("_", "-"), run.config[field]]
    return _controller_call([*argv, *extra], source_root=run.origin_source_root)


def test_fresh_cli_proves_storage_releases_forward_and_next_ordinary_prepare_works(storage_lab):
    root, run, _ = storage_lab
    ledger = Path(run.config["data_state"]) / "retained-index-binding.json"
    ledger.unlink()
    assert _fresh_cli(run) == 0
    state = run.state.parent / "fresh-initialization"
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["phase"] == "initialized" and manifest["local_forward_released"] is True
    assert manifest["allocation_attempted"] is False and manifest["endpoint"] is None
    assert not (state / "execution").exists()
    assert manifest["initialization"]["inventory"] == {"collections": 1, "aliases": 0, "controls_checked": 0}
    assert json.loads(ledger.read_text())["target"] == {
        "namespace": "retained", "claim_uid": "claim-original", "pv_uid": "pv-original-uid",
        "storage_path": "/qdrant/storage", "logical_collection": "original_manuals"}
    assert [(kind, argv) for kind, argv in _events(root) if kind == "qdrant-read"] == [
        ("qdrant-read", ["GET", "/collections"]), ("qdrant-read", ["GET", "/aliases"])]
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", run.config["qdrant_port"]))
    for command in (["stop", "--state", str(state)], ["run", "--state", str(state), "--approve-cu", "20"]):
        assert _controller_call(command) == 2
    assert not any(kind in ("colab", "docker", "ssh", "ssh-keygen") for kind, _ in _events(root))
    next_state = _prepare_production_again(run, "ordinary-after-fresh-binding")
    ordinary = controller.Run(next_state)
    assert ordinary.production_admission().manifest["sources_verified"] is True
    original = ledger.read_bytes()
    assert _fresh_cli(run, "duplicate-initialization") == 2
    assert ledger.read_bytes() == original


@pytest.mark.parametrize("failure", ["oversized", "replace-producer"])
def test_fresh_cli_refusal_releases_forward_then_retry_reproves_target(storage_lab, monkeypatch, failure):
    root, run, _ = storage_lab
    ledger = Path(run.config["data_state"]) / "retained-index-binding.json"
    ledger.unlink()
    monkeypatch.setenv("FAKE_FRESH_API", failure)
    assert _fresh_cli(run) == 2
    assert not ledger.exists()
    manifest = json.loads((run.state.parent / "fresh-initialization/manifest.json").read_text())
    assert manifest["local_forward_released"] is True
    monkeypatch.delenv("FAKE_FRESH_API")
    (root / "fresh-producer-replaced").unlink(missing_ok=True)
    assert _fresh_cli(run, "fresh-after-refusal") == 0
    assert ledger.exists()
    assert not any(kind in ("colab", "docker", "ssh", "ssh-keygen") for kind, _ in _events(root))


@pytest.mark.parametrize("mode", ["slow-headers", "slow-chunk"])
def test_fresh_http_progress_cannot_extend_absolute_deadline(storage_lab, monkeypatch, mode):
    root, run, _ = storage_lab
    ledger = Path(run.config["data_state"]) / "retained-index-binding.json"
    ledger.unlink()
    approval = Path(run.config["production_approval"])
    value = json.loads(approval.read_text())
    value["source_scan"]["deadline_seconds"] = 2
    test_colab_production._json(approval, value)
    monkeypatch.setenv("FAKE_FRESH_API", mode)
    started = time.monotonic()
    assert _fresh_cli(run, extra=("--deadline-seconds", "3")) == 2
    assert time.monotonic() - started < 8
    assert (root / "fresh-slow-started").exists(), "the actual framing read must have started"
    assert not ledger.exists()
    manifest = json.loads((run.state.parent / "fresh-initialization/manifest.json").read_text())
    assert manifest["local_forward_released"] is True
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", run.config["qdrant_port"]))


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_fresh_cleanup_finishes_before_honoring_another_interrupt(storage_lab, monkeypatch, interrupt):
    root, run, _ = storage_lab
    (Path(run.config["data_state"]) / "retained-index-binding.json").unlink()
    monkeypatch.setenv("FAKE_FRESH_API", "resist-cleanup")
    delivered = []

    def interrupt_term_wait():
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if (root / "fresh-forward-term").exists():
                os.kill(os.getpid(), interrupt)
                delivered.append(True)
                return
            time.sleep(.01)

    sender = threading.Thread(target=interrupt_term_wait, daemon=True)
    sender.start()
    try:
        assert _fresh_cli(run) == 0
    finally:
        sender.join(timeout=8)
    assert delivered == [True]
    manifest = json.loads((run.state.parent / "fresh-initialization/manifest.json").read_text())
    assert manifest["local_forward_released"] is True
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", run.config["qdrant_port"]))


def _production_cli(state, *, stop=False, workload=()):
    argv = [sys.executable, str(ROOT / "scripts/colab.py"), "stop" if stop else "run", "--state", str(state)]
    if not stop:
        argv += ["--approve-cu", "20"]
        if workload:
            argv += ["--", *workload]
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)


def test_production_prepare_refuses_maxima_only_census_without_independent_inputs(lab):
    root, base, _ = lab
    original = json.loads((base / "manifest.json").read_text())
    corpus = root / "unmeasured-original-corpus"
    corpus.mkdir()
    golden = root / "unmeasured-original-golden.jsonl"
    golden.write_text('{"query":"Original question","expected_doc_ids":["original"]}\n')
    inventory = root / "maxima-only.json"
    inventory.write_text('{"max_document_tokens":8,"max_query_tokens":3}\n')
    kubeconfig = root / "original-kubeconfig"
    kubeconfig.write_text("original fake Kubernetes client input\n")
    state = root / "must-refuse-incomplete-production"
    argv = ["prepare", "--state", str(state), "--profile", "COLAB_G4_BF16",
            "--dataset", "production", "--gateway-image", IMAGE,
            "--corpus", str(corpus), "--golden", str(golden), "--inventory", str(inventory),
            "--data-state", str(root / "unknown-retained-data"), "--kubeconfig", str(kubeconfig),
            "--kube-context", "original-kind", "--namespace", "retained", "--qdrant-service", "qdrant"]
    for name in ("colab", "ssh", "ssh_keygen", "docker"):
        argv += ["--" + name.replace("_", "-"), original["tools"][name]]
    argv += ["--kubectl", original["tools"]["docker"]]
    assert _controller_call(argv, source_root=Path(original["origin"]["source_root"])) == 2
    assert _events(root) == [], "incomplete evidence reached an external tool"
    assert not state.exists(), "incomplete evidence was published as prepared production"


@pytest.mark.parametrize("lab", ["COLAB_G4_BF16"], indirect=True)
def test_bad_assignment_releases_only_owned_session_before_model_runtime(lab, monkeypatch):
    root, state, command = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "bad_gpu")
    result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20",
                            "--", sys.executable, "-c", "raise AssertionError('must not run')"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert not any(kind == "ssh" for kind, _ in events)
    assert not any("colab_runtime.py" in " ".join(argv) for _, argv in events)
    stopped = [argv[argv.index("-s") + 1] for kind, argv in events if kind == "colab" and "stop" in argv]
    assert stopped == [json.loads((state / "manifest.json").read_text())["session"]]
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_spent_cu_cap_refuses_even_when_current_rate_is_zero(lab, monkeypatch):
    root, state, command = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "spent_cap_zero_rate")
    result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20",
                            "--", sys.executable, "-c", "raise AssertionError('must not run')"],
                            capture_output=True, text=True, timeout=15, check=False)
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert not any("colab_runtime.py" in " ".join(argv) for _, argv in events)
    assert result.returncode != 0
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((state / "manifest.json").read_text())["released"] is True


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_interrupt_quiesces_workload_before_remote_export_and_release(lab):
    root, state, command = lab
    program = (
        "import json,signal,time\n"
        f"from pathlib import Path\np=Path({str(root / 'events.jsonl')!r})\n"
        f"ready=Path({str(root / 'workload-started')!r})\n"
        "def stop(*args):\n"
        " with p.open('a') as f: f.write(json.dumps(['workload-stopped',[]])+'\\n')\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "ready.touch()\n"
        "until=time.monotonic()+10\n"
        "while time.monotonic()<until: time.sleep(.02)\n"
    )
    process = subprocess.Popen(command + ["run", "--state", str(state), "--approve-cu", "20",
                                         "--", sys.executable, "-c", program],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while not (root / "workload-started").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / "workload-started").exists(), "controller never reached the synthetic workload"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    assert process.returncode == 130, stdout + stderr
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    stopped = next(i for i, (kind, _) in enumerate(events) if kind == "workload-stopped")
    remote_teardown = next(i for i, (kind, argv) in enumerate(events)
                           if kind == "colab" and "upload" in argv and argv[-1] == "/content/colab-stop")
    assert stopped < remote_teardown
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_workload_executes_the_prepared_checkout_from_a_decoy_cwd(lab, tmp_path):
    root, state, command = lab
    decoy = tmp_path / "decoy-checkout"
    decoy.mkdir()
    observed = root / "workload-cwd"
    program = f"from pathlib import Path; Path({str(observed)!r}).write_text(str(Path.cwd()))"
    result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20",
                            "--", sys.executable, "-c", program],
                            cwd=decoy, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed.read_text() == str(state / "execution")
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_G4_BF16"], indirect=True)
def test_paid_execution_uses_frozen_uploads_gateway_task_and_fresh_imports_during_origin_mutation(lab, monkeypatch):
    root, state, command = lab
    manifest = json.loads((state / "manifest.json").read_text())
    execution = state / "execution"
    origin = Path(manifest["origin"]["source_root"])
    expected_profile = (execution / "src/mainframe_rag/serve/colab_profiles.py").read_bytes()
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "origin_mutation")
    result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20"],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((root / "snapshot-workload.json").read_text())
    assert observed == {
        "value": "original frozen fixture",
        "module": str(execution / "src/mainframe_rag/snapshot_probe.py"),
        "cwd": str(execution), "python": sys.executable, "origin_changed": True,
    }
    assert (root / "remote/colab_profiles.py").read_bytes() == expected_profile
    executions = [argv[argv.index("-f") + 1] for kind, argv in _events(root)
                  if kind == "colab" and argv[2] == "exec"]
    assert executions == [str(execution / "scripts/colab_preflight.py"),
                          str(execution / "scripts/colab_runtime.py")]
    runtime_call = next(argv for kind, argv in _events(root)
                        if kind == "colab" and argv[2] == "exec" and argv[-1].endswith("colab_runtime.py"))
    assert "RAG_COLAB_DEVELOPMENT_SHA=" + manifest["development_sha"] in runtime_call
    # The fake modifies the original gateway and Task to fail after allocation;
    # successful probing and the imported marker therefore require the copies.
    restored = controller.source_identity(origin)
    assert restored == manifest["execution"]["source"]
    assert hashlib.sha256((origin / ".tools/bin/task").read_bytes()).hexdigest() == \
        manifest["execution"]["task_sha256"]
    assert manifest["execution"]["source"]["source_bytes"] < 2 * 1024 * 1024
    assert manifest["execution"]["task_bytes"] < 4096
    assert not any((execution / name).exists() for name in (".git", ".venv", ".env", "corpus"))
    finished = json.loads((state / "manifest.json").read_text())
    assert finished["released"] is True and finished["release_evidence"]["absent"] is True
    assert not (state / "transport-receipt.json").exists(), "GPU success cannot certify CPU transport"
    assert json.loads((root / "sessions.json").read_text()) == {}
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    ordinary = _prepare_new(state, root / "ordinary-after-source-restoration")
    next_run = subprocess.run(command + ["run", "--state", str(ordinary), "--approve-cu", "20", "--", "/bin/true"],
                              capture_output=True, text=True, timeout=15, check=False)
    assert next_run.returncode == 0, next_run.stdout + next_run.stderr
    assert (ordinary / "transport-receipt.json").is_file()


@pytest.mark.parametrize("changed", ["source", "task", "dotenv"])
def test_tampered_execution_snapshot_refuses_before_tools_then_restored_snapshot_runs(lab, changed):
    root, state, command = lab
    execution = state / "execution"
    relative = {"source": "scripts/colab_preflight.py", "task": ".tools/bin/task", "dotenv": ".env"}[changed]
    target = execution / relative
    original = target.read_bytes() if changed != "dotenv" else None
    if changed == "dotenv":
        execution.chmod(0o700)
        target.write_text("ORIGINAL_SYNTHETIC_SETTING=unsupported\n")
        execution.chmod(0o500)
    else:
        target.chmod(0o600)
        target.write_bytes(original + b"\n# original synthetic tampering\n")
        target.chmod(0o500 if changed == "task" else 0o400)
    rejected = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20", "--", "/bin/true"],
                              capture_output=True, text=True, timeout=5, check=False)
    assert rejected.returncode == 2
    assert _events(root) == []
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False
    if changed == "dotenv":
        execution.chmod(0o700)
        target.unlink()
        execution.chmod(0o500)
    else:
        target.chmod(0o600)
        target.write_bytes(original)
        target.chmod(0o500 if changed == "task" else 0o400)
    result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20", "--", "/bin/true"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state / "transport-receipt.json").is_file()


@pytest.mark.parametrize("changed", ["task", "dotenv", "copy_race", "gateway_capture", "gateway_capture_race"])
def test_prepare_publishes_only_exact_bounded_source_and_pinned_tool_then_next_candidate_runs(lab, monkeypatch, changed):
    root, base, command = lab
    state = root / "rejected-source-candidate"
    argv, source = _prepare_arguments(base, state)
    task = source / ".tools/bin/task"
    original_task = task.read_bytes()
    capture = source / "scripts/gateway/log_capture.py"
    original_capture = capture.read_bytes()
    read_bounded = controller.read_bounded_input
    source_check = controller.check_source_configuration
    if changed == "task":
        task.write_bytes(original_task + b"\n# mismatched original tool\n")
    elif changed == "dotenv":
        (source / ".env").write_text("ORIGINAL_SYNTHETIC_SETTING=unsupported\n")
    elif changed == "gateway_capture":
        capture.unlink()
    elif changed == "gateway_capture_race":
        def removed_after_precheck(root):
            source_check(root)
            if root == source:
                capture.unlink()

        monkeypatch.setattr(controller, "check_source_configuration", removed_after_precheck)
    else:
        def changed_during_copy(path, limit):
            raw = read_bounded(path, limit)
            return raw + b"\n# original concurrent edit\n" if path == source / "scripts/colab_preflight.py" else raw

        monkeypatch.setattr(controller, "read_bounded_input", changed_during_copy)
    assert _controller_call(argv, source_root=source) == 2
    assert not (state / "manifest.json").exists(), "a partial or mismatched copy became a prepared candidate"
    assert _events(root) == []
    assert not (state / "execution/.env").exists(), "private configuration was copied"
    if changed == "task":
        task.write_bytes(original_task)
    elif changed == "dotenv":
        (source / ".env").unlink()
    elif changed in {"gateway_capture", "gateway_capture_race"}:
        capture.write_bytes(original_capture)
        monkeypatch.setattr(controller, "check_source_configuration", source_check)
    else:
        monkeypatch.setattr(controller, "read_bounded_input", read_bounded)
    ordinary = _prepare_new(base, root / "ordinary-after-invalid-source")
    result = subprocess.run(command + ["run", "--state", str(ordinary), "--approve-cu", "20", "--", "/bin/true"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (ordinary / "transport-receipt.json").is_file()


@pytest.mark.parametrize("bounded", ["source_bytes", "source_entries", "task_bytes"])
def test_prepare_copy_enforces_separate_source_and_tool_caps_without_allocating(lab, monkeypatch, bounded):
    root, base, _ = lab
    state = root / "bounded-source-refusal"
    argv, source = _prepare_arguments(base, state)
    if bounded == "source_bytes":
        monkeypatch.setattr(controller, "SOURCE_LIMIT_BYTES", 1)
    elif bounded == "source_entries":
        monkeypatch.setattr(controller, "SOURCE_ENTRY_LIMIT", 1)
    else:
        monkeypatch.setattr(controller, "TASK_BINARY_LIMIT_BYTES", 1)
    assert _controller_call(argv, source_root=source) == 2
    assert not (state / "manifest.json").exists()
    assert _events(root) == []
    copied = state / "execution/.tools/bin/task"
    assert not copied.exists(), "a tool larger than its independent envelope was copied"


def test_snapshot_recovery_releases_owned_session_despite_changed_origin_then_next_run_works(lab, monkeypatch):
    root, state, command = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "stop_fail")
    failed = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20", "--", "/bin/true"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert failed.returncode == 1
    manifest = json.loads((state / "manifest.json").read_text())
    assert json.loads((root / "sessions.json").read_text()) == {manifest["session"]: "endpoint-owned"}
    source = Path(manifest["origin"]["source_root"])
    changed = source / "scripts/run_local_gateway.sh"
    original = changed.read_bytes()
    changed.write_text("exit 97\n")
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    recovered = subprocess.run(command + ["stop", "--state", str(state)],
                               capture_output=True, text=True, timeout=15, check=False)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    assert not (state / "transport-receipt.json").exists()
    changed.write_bytes(original)
    ordinary = _prepare_new(state, root / "ordinary-after-frozen-recovery")
    result = subprocess.run(command + ["run", "--state", str(ordinary), "--approve-cu", "20", "--", "/bin/true"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (ordinary / "transport-receipt.json").is_file()


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_separate_run_states_share_assignment_ownership_and_allow_next_run(lab, monkeypatch):
    root, state, _ = lab
    second = _prepare_new(state, root / "second-private-run")
    entered = []
    monkeypatch.setattr(controller.Run, "run", lambda self, approval, workload: entered.append(self.state) or 0)
    with controller.assignment_lock():
        assert _controller_call(["run", "--state", str(second), "--approve-cu", "20"]) == 2
        assert entered == [], "a distinct state bypassed assignment ownership"
    assert _controller_call(["run", "--state", str(second), "--approve-cu", "20"]) == 0
    assert entered == [second]


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_slow_export_cannot_consume_owned_vm_release_budget(lab, monkeypatch):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "slow_export")
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned")
    _seed_owned_assignment(run, root)
    run.expiry = time.time() + 6
    assert run.cleanup() is False, "an unavailable export cannot be reported as a complete cleanup"
    assert json.loads((root / "sessions.json").read_text()) == {}
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True
    assert any(event["event"] == "artifact_export_unavailable" for event in manifest["events"])
    run.expiry = time.time() + 6
    assert run.cleanup() is True, "the next ordinary recovery must recognize the released session"


def test_cli_capture_keeps_stdout_separate_from_bounded_noisy_stderr_and_allows_next_command(lab):
    _, state, _ = lab
    run = controller.Run(state)
    result = run.command([sys.executable, "-c",
                          "import os; os.write(1,b'{\"identity\":\"owned\"}'); os.write(2,b'E'*262144)"],
                         seconds=3)
    assert result.stdout == '{"identity":"owned"}'
    assert result.stderr == "E" * (64 * 1024)
    assert run.manifest["controller_capture"]["stderr_truncated"] is True
    assert run.manifest["controller_capture"]["stdout_truncated"] is False
    assert sorted(path.stat().st_size for path in state.glob("command-work-*.log")) == [20, 64 * 1024]
    assert run.command([sys.executable, "-c", "print('next ordinary command')"], seconds=3).stdout == \
        "next ordinary command\n"


def test_oversize_cli_stdout_cannot_establish_partial_structured_identity(lab):
    _, state, _ = lab
    run = controller.Run(state)
    program = "import os; os.write(1,b'[colab] No active sessions found on server.\\n'+b' '*262144)"
    with pytest.raises(controller.LabError, match="partial identity"):
        run.command([sys.executable, "-c", program], seconds=3, allow_failure=True)
    assert run.manifest["controller_capture"]["stdout_truncated"] is True
    assert all(path.stat().st_size <= 64 * 1024 for path in state.glob("command-work-*.log"))
    assert run.command([sys.executable, "-c", "print('ordinary')"], seconds=3).stdout == "ordinary\n"


def test_spawned_noisy_streams_are_capped_drained_and_followed_by_an_ordinary_child(lab):
    _, state, _ = lab
    run = controller.Run(state)
    child = run.spawn([sys.executable, "-c",
                       "import os; os.write(1,b'O'*262144); os.write(2,b'E'*262144)"], "noisy")
    capture = run.captures[child]
    try:
        assert child.wait(timeout=3) == 0
    finally:
        run.terminate(child)
    assert not capture.thread.is_alive()
    assert (state / "noisy.stdout.log").read_bytes() == b"O" * (64 * 1024)
    assert (state / "noisy.stderr.log").read_bytes() == b"E" * (64 * 1024)
    assert capture.output == {"stdout": bytearray(), "stderr": bytearray()}
    normal = run.spawn([sys.executable, "-c", "print('ordinary child')"], "ordinary")
    try:
        assert normal.wait(timeout=3) == 0
    finally:
        run.terminate(normal)
    assert (state / "ordinary.stdout.log").read_text() == "ordinary child\n"


def _assert_process_not_live(pid: int) -> None:
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        except FileNotFoundError:
            return
        if status == "Z":
            return  # An orphan may await init reaping; it no longer runs/holds pipes.
        time.sleep(.01)
    pytest.fail("owned process remained live after bounded group termination")


@pytest.mark.parametrize("overflow", [False, True])
def test_closed_child_pipes_survive_scheduler_pause_before_final_eof_then_next_command_works(lab, monkeypatch, overflow):
    _, state, _ = lab
    run = controller.Run(state)
    stdout = "complete | ordinary α\n".encode()
    child = subprocess.Popen([sys.executable, "-c", f"import os; os.write(1, {stdout!r})"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    assert child.wait(timeout=3) == 0  # No descendants or live writer remain.
    selector_factory = controller.selectors.DefaultSelector
    clock = controller.time
    offset = 0.0

    class PauseAfterReady:
        def __init__(self):
            self.selector = selector_factory()
            self.paused = False

        def __enter__(self):
            self.selector.__enter__()
            return self

        def __exit__(self, *args):
            return self.selector.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.selector, name)

        def select(self, timeout):
            nonlocal offset
            events = self.selector.select(timeout)
            if events and not self.paused:
                self.paused = True
                offset += controller.PIPE_DRAIN_GRACE_SECONDS + .01
            return events

    if overflow:
        monkeypatch.setattr(controller, "CAPTURE_LIMIT_BYTES", 16)
    read = os.read
    stdout_fd = child.stdout.fileno()
    bytes_read = 0

    def partial_first_read(fd, limit):
        nonlocal bytes_read
        if fd == stdout_fd and bytes_read == 0:
            limit = min(limit, 8)
        data = read(fd, limit)
        if fd == stdout_fd:
            bytes_read += len(data)
        return data

    reserved = run.output_budget.reserve("work")
    try:
        with monkeypatch.context() as patch:
            if overflow:
                patch.setattr(controller.os, "read", partial_first_read)
            patch.setattr(controller, "time", SimpleNamespace(monotonic=lambda: clock.monotonic() + offset))
            patch.setattr(controller.selectors, "DefaultSelector", PauseAfterReady)
            capture = run.capture(child, {name: state / f"paused.{name}.log" for name in ("stdout", "stderr")},
                                  "work", reserved, retain=True)
            capture.finish()
    finally:
        run.terminate(child)
    assert offset > controller.PIPE_DRAIN_GRACE_SECONDS
    expected = stdout[:16] if overflow else stdout
    assert bytes(capture.output["stdout"]) == expected
    assert capture.truncated == {"stdout": overflow, "stderr": False}
    assert capture.held_pipes is overflow and capture.log_failed is False
    assert (state / "paused.stdout.log").read_bytes() == expected
    if overflow:
        assert bytes_read == 17, "final probing must read only the cap plus one overflow byte"
    assert run.command([sys.executable, "-c", "print('ordinary')"], seconds=3).stdout == "ordinary\n"


def test_root_exit_with_descendant_held_pipes_is_bounded_and_next_command_works(lab):
    root, state, _ = lab
    run = controller.Run(state)
    descendant_pid = root / "held-pipe-pid"
    worker = ("import os,signal,time; from pathlib import Path; "
              "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              f"Path({str(descendant_pid)!r}).write_text(str(os.getpid())); time.sleep(30)")
    program = (
        "import subprocess,sys,time; from pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{worker!r}])\n"
        "until=time.monotonic()+1\n"
        f"while not Path({str(descendant_pid)!r}).exists() and time.monotonic()<until: time.sleep(.01)\n"
        "print('root finished',flush=True)\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(controller.LabError, match="partial identity"):
            run.command([sys.executable, "-c", program], seconds=3)
        assert any(path.read_text() == "root finished\n" for path in state.glob("command-work-*.stdout.log"))
        assert run.manifest["controller_capture"]["descendant_pipes_incomplete"] is True
        assert time.monotonic() - started < 2
        assert descendant_pid.exists()
        _assert_process_not_live(int(descendant_pid.read_text()))
        assert run.command([sys.executable, "-c", "print('ordinary')"], seconds=3).stdout == "ordinary\n"
    finally:
        if descendant_pid.exists():
            try:
                os.kill(int(descendant_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cli_timeout_reaps_an_uncooperative_root_and_allows_next_command(lab):
    root, state, _ = lab
    run = controller.Run(state)
    pid_file = root / "timeout-pid"
    program = ("import os,signal,time; from pathlib import Path; "
               "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
               f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run.command([sys.executable, "-c", program], seconds=.5)
    assert time.monotonic() - started < 4
    assert pid_file.exists()
    _assert_process_not_live(int(pid_file.read_text()))
    assert run.command([sys.executable, "-c", "print('ordinary')"], seconds=3).stdout == "ordinary\n"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 0, True, "1"])
def test_invalid_cli_timeout_is_refused_before_launch(lab, invalid):
    root, state, _ = lab
    marker = root / "invalid-timeout-executed"
    run = controller.Run(state)
    with pytest.raises(controller.LabError, match="timeout must be finite and positive"):
        run.command([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                    seconds=invalid)
    assert not marker.exists()
    assert not list(state.glob("command-*.log"))


def test_capture_write_allowance_persists_across_controller_recovery(lab, monkeypatch):
    _, state, _ = lab
    monkeypatch.setitem(controller.CAPTURE_POOLS, "work", 256 * 1024)
    run = controller.Run(state)
    program = "import os; os.write(1,b'O'*65536); os.write(2,b'E'*65536)"
    for _ in range(2):
        result = run.command([sys.executable, "-c", program], seconds=3)
        assert result.stdout == "O" * 65536
        assert result.stderr == "E" * 65536
    assert sum(path.stat().st_size for path in state.glob("command-work-*.log")) == 256 * 1024
    recovered = controller.Run(state)
    with pytest.raises(controller.LabError, match="work output allowance exhausted"):
        recovered.command([sys.executable, "-c", "raise AssertionError('must not execute')"], seconds=3)
    assert json.loads((state / "capture-budget.json").read_text())["used"]["work"] == 256 * 1024
    assert recovered.command([sys.executable, "-c", "print('cleanup still works')"],
                             cleanup=True, seconds=3).stdout == "cleanup still works\n"


def test_command_capture_uses_a_fixed_number_of_log_slots(lab, monkeypatch):
    _, state, _ = lab
    monkeypatch.setattr(controller, "COMMAND_LOG_SLOTS", 2)
    run = controller.Run(state)
    for _ in range(5):
        assert run.command([sys.executable, "-c", "print('ordinary')"], seconds=3).stdout == "ordinary\n"
    assert len(list(state.glob("command-work-*.log"))) == 4


@pytest.mark.parametrize("budget", ["exhausted", "corrupt"])
@pytest.mark.parametrize("scenario", ["success", "noisy_stop"])
def test_output_limits_never_prevent_owned_release_and_a_fresh_ordinary_operation(lab, monkeypatch,
                                                                                 budget, scenario):
    root, state, _ = lab
    ledger = {"schema": 1, "used": {"work": 4 * 1024 * 1024,
                                    "export": 256 * 1024, "release": 512 * 1024}}
    (state / "capture-budget.json").write_text(json.dumps(ledger) if budget == "exhausted" else "[]")
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", scenario)
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned")
    _seed_owned_assignment(run, root)
    remote = root / "remote"
    remote.mkdir()
    (remote / "colab-artifacts.tgz").write_bytes(b"private-fake-diagnostics")
    with pytest.raises(controller.LabError, match="work output allowance exhausted"):
        run.command([sys.executable, "-c", "raise AssertionError('must not execute')"], seconds=3)
    run.expiry = time.time() + 6
    assert run.cleanup() is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True
    assert manifest["controller_capture"]["stdout_truncated"] is (scenario == "noisy_stop")
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    stops = [args for kind, args in events if kind == "colab" and args[2] == "stop"]
    assert len(stops) == 1 and stops[0][-1] == run.manifest["session"]
    assert not list(state.glob("command-*.log")), "exhausted capture must not create unbounded cleanup logs"
    fresh = root / "next-private-run"
    _prepare_new(state, fresh)
    ordinary = controller.Run(fresh)
    assert ordinary.command([sys.executable, "-c", "print('ordinary after release')"],
                            seconds=3).stdout == "ordinary after release\n"


def test_prepared_dirty_source_change_refuses_before_allocation_then_ordinary_cpu_run(lab, monkeypatch):
    root, base, _ = lab
    sources = Path(json.loads((base / "manifest.json").read_text())["origin"]["source_root"])
    state = _prepare_new(base, root / "bound-source-run")
    changed = sources / "src/mainframe_rag/serve/colab_profiles.py"
    original = changed.read_bytes()
    changed.write_text("changed source at the same HEAD\n")
    assert _run_fake(state) == 2
    assert _events(root) == [], "source drift reached a tool or allocated a runtime"
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False
    changed.write_bytes(original)
    assert _run_fake(state) == 0
    assert (state / "transport-receipt.json").is_file()
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("changed", ["head", "reported_head", "profile", "serialized_profile", "config", "tool"])
def test_prepared_candidate_and_execution_identity_drift_refuses_before_allocation(lab, changed):
    root, state, _ = lab
    manifest = json.loads((state / "manifest.json").read_text())
    if changed == "head":
        manifest["prepared_identity"]["candidate"]["development_sha"] = "0" * 40
    elif changed == "reported_head":
        manifest["development_sha"] = "0" * 40
    elif changed == "profile":
        manifest["profile"]["dense_dim"] = 4096
    elif changed == "serialized_profile":
        with (state / "model-profile.json").open("a") as stream:
            stream.write("\n")
    elif changed == "config":
        manifest["config"]["gateway_image"] = "example.invalid/other@sha256:" + "b" * 64
    else:
        with Path(manifest["tools"]["colab"]).open("a") as stream:
            stream.write("\n# changed executable at the same path\n")
    (state / "manifest.json").write_text(json.dumps(manifest))
    assert _run_fake(state) == 2
    assert _events(root) == []
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False


def test_successful_cpu_receipt_is_created_only_after_exact_release_absence(lab):
    root, state, _ = lab
    assert _run_fake(state) == 0
    receipt = json.loads((state / "transport-receipt.json").read_text())
    manifest = json.loads((state / "manifest.json").read_text())
    assert receipt["kind"] == "colab-cpu-transport"
    assert receipt["released"] is True and receipt["workload_succeeded"] is True
    assert receipt["cpu_profile"]["name"] == "COLAB_CPU_TRANSPORT"
    assert receipt["cpu_profile"]["accelerator"] is None and receipt["cpu_profile"]["servers"] == []
    assert receipt["candidate"] == manifest["prepared_identity"]["candidate"]
    assert receipt["transport"]["gateway_image"] == IMAGE
    assert receipt["transport"]["cli_version"] == "0.7.4"
    assert receipt["transport"]["network_name"] == "local-litellm-gateway-net"
    assert receipt["transport"]["gateway_name"] == "local-litellm-gateway"
    assert receipt["transport"]["reasoning_url"] == f"http://127.0.0.2:{manifest['config']['reasoning_port']}/v1"
    assert receipt["transport"]["embedding_url"] == f"http://127.0.0.2:{manifest['config']['embedding_port']}/v1"
    assert "network_id" not in receipt["transport"]
    assert receipt["transport"]["bridge"] == "127.0.0.2"
    assert receipt["transport"]["forwards"] == [
        {"bind": address, "local_port": manifest["config"][local],
         "remote_host": "127.0.0.1", "remote_port": remote}
        for local, remote in (("reasoning_port", 8000), ("embedding_port", 8001))
        for address in ("127.0.0.1", "127.0.0.2")
    ]
    proof = receipt["release_evidence"]
    assert proof["absent"] is True and proof["session"] == manifest["session"]
    assert proof["endpoint"] == receipt["endpoint"] == "endpoint-owned"
    assert 0 < proof["observed_at"] <= receipt["completed_at"] <= time.time()
    calls = [argv for kind, argv in _events(root) if kind == "colab"]
    stopped = next(index for index, argv in enumerate(calls) if argv[2] == "stop")
    assert calls[stopped][-1] == receipt["session"]
    assert any(argv[2] == "sessions" for argv in calls[stopped + 1:])
    assert "--gpu" not in next(argv for argv in calls if argv[2] == "new")
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("address", ["10.23.4.5", "172.20.5.6", "192.168.42.3"])
def test_explicit_private_docker_gateway_is_frozen_and_checked_before_allocation(lab, monkeypatch, address):
    root, base, _ = lab
    state = _prepare_new(base, root / "explicit-route", extra=("--docker-gateway", address))
    prepared = json.loads((state / "manifest.json").read_text())
    assert prepared["config"]["docker_gateway"] == address
    monkeypatch.setenv("COLAB_DOCKER_GATEWAY", "10.99.99.99")
    checked = []

    def unavailable(self, host, port):
        checked.append((host, port))
        if host == address:
            raise controller.LabError("original fixture private listener is unavailable")

    monkeypatch.setattr(controller.Run, "free_port", unavailable)
    assert _run_fake(state) != 0
    assert checked == [("127.0.0.1", prepared["config"]["reasoning_port"]),
                       (address, prepared["config"]["reasoning_port"])]
    assert not any(kind == "docker" and argv[:3] == ["network", "inspect", "bridge"]
                   for kind, argv in _events(root))
    assert not any(kind == "colab" and argv[2] == "new" for kind, argv in _events(root))
    failed = json.loads((state / "manifest.json").read_text())
    assert failed["allocation_attempted"] is False and failed["endpoint"] is None
    assert not (state / "transport-receipt.json").exists()


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "127.0.0.2", "169.254.1.2",
                                     "192.0.2.1", "100.64.0.1", "8.8.8.8", "224.0.0.1",
                                     "::1", "fd00::1", "host.docker.internal", "http://10.0.0.1",
                                     "10.0.0.1:8000", "10.0.0.1\n", "010.0.0.1"])
def test_non_private_or_non_numeric_docker_gateway_refuses_preparation(lab, address):
    root, base, _ = lab
    state = root / "rejected-route"
    argv, source = _prepare_arguments(base, state, extra=("--docker-gateway", address))
    assert _controller_call(argv, source_root=source) == 2
    assert not state.exists()
    assert _events(root) == []


def test_cpu_transport_survives_delayed_listener_inside_run_deadline_then_next_run_works(lab, monkeypatch):
    root, state, command = lab
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["profile"]["deadline_seconds"] == 30
    # The public controller has a 20-second work window. Its boundary doubles
    # must not disappear at 12 seconds while that valid work is still ongoing.
    with monkeypatch.context() as delayed:
        delayed.setenv("FAKE_SSH_START_SECONDS", "12.5")
        result = subprocess.run(command + ["run", "--state", str(state), "--approve-cu", "20",
                                           "--", "/bin/true"],
                                capture_output=True, text=True, timeout=24, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((state / "transport-receipt.json").read_text())
    assert receipt["released"] is True and receipt["workload_succeeded"] is True
    assert receipt["release_evidence"]["session"] == manifest["session"]
    assert receipt["release_evidence"]["endpoint"] == "endpoint-owned"
    assert receipt["release_evidence"]["absent"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((root / "gateway-networks.json").read_text()) == {}

    following = _prepare_new(state, root / "ordinary-after-delayed-listener")
    assert _run_fake(following) == 0
    next_receipt = json.loads((following / "transport-receipt.json").read_text())
    assert next_receipt["released"] is True and next_receipt["workload_succeeded"] is True
    assert next_receipt["release_evidence"]["absent"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((root / "gateway-networks.json").read_text()) == {}


@pytest.mark.parametrize("failure", ["workload", "download_fail", "stop_fail"])
def test_failed_cpu_run_has_no_receipt_and_recovery_allows_a_fresh_normal_run(lab, monkeypatch, failure):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success" if failure == "workload" else failure)
    assert _run_fake(state, ("/bin/false",) if failure == "workload" else ("/bin/true",)) != 0
    assert not (state / "transport-receipt.json").exists()
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert not (state / "transport-receipt.json").exists(), "recovery cannot certify a failed rehearsal"
    ordinary = _prepare_new(state, root / "ordinary-cpu-after-recovery")
    assert _run_fake(ordinary) == 0
    assert (ordinary / "transport-receipt.json").is_file()
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("lock", [None, "vllm>=0.28.0\n", "vllm==0.28.0\n",
                                  _reference_lock().replace("vllm==0.28.0", "vllm==0.27.0")])
def test_gpu_lock_must_validate_before_allocation_and_valid_prerequisites_allow_next_run(lab, lock):
    root, cpu, _ = lab
    assert _run_fake(cpu) == 0
    (root / "events.jsonl").write_text("")
    inference = root / "gpu-inference.lock"
    extra = ["--transport-receipt", str(cpu / "transport-receipt.json")]
    if lock is not None:
        inference.write_text(lock)
        extra += ["--inference-lock", str(inference)]
    invalid = _prepare_new(cpu, root / "invalid-gpu-lock", profile="COLAB_G4_BF16", extra=extra)
    assert _run_fake(invalid) != 0
    assert not any(kind == "colab" and argv[2] == "new" for kind, argv in _events(root))
    assert json.loads((invalid / "manifest.json").read_text())["allocation_attempted"] is False
    if lock is None:
        inference.write_text(_reference_lock())
        valid = _prepare_new(cpu, root / "ordinary-gpu-lock", profile="COLAB_G4_BF16",
                             extra=extra + ["--inference-lock", str(inference)])
        assert _run_fake(valid) == 0
        news = [argv for kind, argv in _events(root) if kind == "colab" and argv[2] == "new"]
        assert len(news) == 1 and news[0][-2:] == ["--gpu", "G4"]
        assert json.loads((root / "sessions.json").read_text()) == {}
        assert not (valid / "transport-receipt.json").exists()
        cpu_manifest = json.loads((cpu / "manifest.json").read_text())
        gpu_manifest = json.loads((valid / "manifest.json").read_text())
        assert cpu_manifest["gateway_network"]["network_id"] != gpu_manifest["gateway_network"]["network_id"]
        assert cpu_manifest["transport"] == gpu_manifest["transport"]


@pytest.mark.parametrize("changed", ["released", "verified", "candidate", "cli", "gateway", "bridge",
                                     "network", "profile", "absence", "session", "future"])
def test_gpu_rejects_incompatible_or_unsuccessful_cpu_receipt_before_allocation(lab, changed):
    root, cpu, _ = lab
    assert _run_fake(cpu) == 0
    (root / "events.jsonl").write_text("")
    receipt = json.loads((cpu / "transport-receipt.json").read_text())
    if changed == "released":
        receipt["released"] = False
    elif changed == "verified":
        receipt["transport"]["verified"] = False
    elif changed == "candidate":
        receipt["candidate"]["source_sha256"] = "0" * 64
    elif changed in ("cli", "gateway", "bridge", "network"):
        field = {"cli": "cli_version", "gateway": "gateway_image", "bridge": "bridge", "network": "network_name"}[changed]
        receipt["transport"][field] = "different identity"
    elif changed == "profile":
        receipt["cpu_profile"]["name"] = "COLAB_G4_BF16"
    elif changed == "absence":
        del receipt["release_evidence"]
    elif changed == "session":
        receipt["release_evidence"]["session"] = "rag-" + "a" * 16
    else:
        receipt["release_evidence"]["observed_at"] = time.time() + 3600
    supplied = root / "incompatible-receipt.json"
    supplied.write_text(json.dumps(receipt))
    inference = root / "receipt-inference.lock"
    inference.write_text(_reference_lock())
    invalid = _prepare_new(cpu, root / "invalid-receipt-gpu", profile="COLAB_G4_BF16",
                           extra=["--transport-receipt", str(supplied), "--inference-lock", str(inference)])
    assert _run_fake(invalid) != 0
    assert not any(kind == "colab" and argv[2] == "new" for kind, argv in _events(root))
    assert json.loads((invalid / "manifest.json").read_text())["allocation_attempted"] is False


@pytest.mark.parametrize("changed", ["lock_input", "lock_snapshot", "receipt_input", "receipt_snapshot"])
@pytest.mark.parametrize("lab", ["COLAB_G4_BF16"], indirect=True)
def test_gpu_prerequisite_byte_drift_after_prepare_refuses_before_allocation(lab, changed):
    root, state, _ = lab
    manifest = json.loads((state / "manifest.json").read_text())
    target = {"lock_input": Path(manifest["config"]["inference_lock"]),
              "lock_snapshot": state / "inference.lock",
              "receipt_input": Path(manifest["config"]["transport_receipt"]),
              "receipt_snapshot": state / "cpu-transport-receipt.input.json"}[changed]
    with target.open("a") as stream:
        stream.write("\n")
    assert _run_fake(state) == 2
    assert _events(root) == []
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False


@pytest.mark.parametrize("poll", ["nan", "inf", "0", "0.5", "5.1", "-1"])
def test_invalid_poll_interval_refuses_preparation_without_allocation(lab, poll):
    root, base, _ = lab
    original = json.loads((base / "manifest.json").read_text())
    tools = original["tools"]
    argv = ["prepare", "--state", str(root / "invalid-poll"), "--dataset", "synthetic",
            "--profile", "COLAB_CPU_TRANSPORT", "--gateway-image", IMAGE, "--poll-seconds", poll]
    for name in ("colab", "ssh", "ssh_keygen", "docker"):
        argv += ["--" + name.replace("_", "-"), tools[name]]
    assert _controller_call(argv) == 2
    assert not (root / "invalid-poll").exists()
    assert _events(root) == []


def test_sensitive_capture_keeps_bounded_memory_without_disk_then_ordinary_capture_works(lab):
    _, state, _ = lab
    run = controller.Run(state)
    program = ("import os; os.write(1,b'ORIGINAL-FAKE-SECRET-STDOUT'); "
               "os.write(2,b'ORIGINAL-FAKE-SECRET-STDERR'+b'X'*262144)")
    result = run.command([sys.executable, "-c", program], seconds=3, sensitive=True)
    assert result.stdout == "ORIGINAL-FAKE-SECRET-STDOUT"
    assert result.stderr.startswith("ORIGINAL-FAKE-SECRET-STDERR")
    assert len(result.stderr) == 64 * 1024
    assert not list(state.glob("command-*.log"))
    assert json.loads((state / "capture-budget.json").read_text())["used"]["work"] == 0
    assert run.command([sys.executable, "-c", "print('ordinary after secret read')"],
                       seconds=3).stdout == "ordinary after secret read\n"
    for path in state.iterdir():
        if path.is_file():
            assert b"ORIGINAL-FAKE-SECRET" not in path.read_bytes()
    assert any(path.read_text() == "ordinary after secret read\n" for path in state.glob("*.stdout.log"))


def test_native_settings_use_proven_qdrant_and_gateway_generated_legs_despite_case_aliases(storage_lab, monkeypatch):
    _, run, _ = storage_lab
    run.persistent_qdrant()
    proven_url = f"http://127.0.0.1:{run.config['qdrant_port']}"
    assert run.manifest["persistent_qdrant"]["url"] == proven_url
    with monkeypatch.context() as ambient:
        ambient.setenv("QDRANT_URL", "http://unapproved-original-target.invalid:6333")
        # These aliases are introduced after preparation. Setting an uppercase
        # URL alone cannot constrain native case-insensitive Settings.
        ambient.setenv("qdrant_url", "http://unapproved-later-target.invalid:6333")
        ambient.setenv("embed_base_url", "http://unapproved-embedding.invalid/v1")
        ambient.setenv("llm_base_url", "http://unapproved-reasoning.invalid/v1")
        ambient.setenv("llm_model_reasoning", "unapproved-reasoning-model")
        ambient.setenv("embed_api_key", "sk-original-unapproved-embedding")
        ambient.setenv("llm_api_key", "sk-original-unapproved-reasoning")
        ambient.setenv("rerank_base_url", "http://unapproved-reranking.invalid/v1")
        ambient.setenv("rerank_model", "unapproved-reranking-model")
        ambient.setenv("rerank_api_key", "sk-original-unapproved-reranking")
        environment = run.environment("127.0.0.2")
    assert environment["QDRANT_URL"] == proven_url
    direct = subprocess.run(
        [run.manifest["execution"]["interpreter"], "-c",
         "from mainframe_rag.config import Settings; print(Settings(_env_file=None).qdrant_url)"],
        env=environment, cwd=run.execution_root, capture_output=True, text=True, timeout=5, check=False)
    assert direct.returncode == 0, direct.stdout + direct.stderr
    assert direct.stdout.strip() == proven_url
    program = """
import json
from mainframe_rag.config import Settings
s=Settings(_env_file=None)
print(json.dumps({'qdrant':s.qdrant_url,'embedding':s.embed_base_url,'reasoning':s.llm_base_url,
                  'reasoning_model':s.llm_model_reasoning,
                  'embedding_key_owned':s.embed_api_key=='sk-original-embedding',
                  'reasoning_key_owned':s.llm_api_key=='sk-original-reasoning',
                  'reranking':s.rerank_base_url,'reranking_model':s.rerank_model,
                  'reranking_key_owned':s.rerank_api_key=='sk-original-reranking'}))
"""
    # The owner's generated gateway file exports these canonical names before
    # the native consumer starts. Original fake leg values preserve that shell
    # boundary without rendering or starting a gateway and without real keys.
    leg_exports = """
export LLM_BASE_URL=http://localhost:40127/v1 LLM_MODEL_REASONING="$3" LLM_API_KEY=sk-original-reasoning
export EMBED_BASE_URL=http://localhost:40127/v1 EMBED_API_KEY=sk-original-embedding
export RERANK_BASE_URL=http://localhost:40127/v1 RERANK_MODEL=original-reranking-model RERANK_API_KEY=sk-original-reranking
exec "$1" -c "$2"
"""
    child = subprocess.run(["sh", "-c", leg_exports, "original-routing-child",
                            run.manifest["execution"]["interpreter"], program, run.profile["servers"][0]["model_id"]],
                           env=environment, cwd=run.execution_root,
                           capture_output=True, text=True, timeout=5, check=False)
    assert child.returncode == 0, child.stdout + child.stderr
    actual = json.loads(child.stdout)
    assert actual == {
        "qdrant": proven_url, "embedding": "http://localhost:40127/v1", "reasoning": "http://localhost:40127/v1",
        "reasoning_model": run.profile["servers"][0]["model_id"],
        "embedding_key_owned": True, "reasoning_key_owned": True,
        "reranking": "http://localhost:40127/v1", "reranking_model": "original-reranking-model",
        "reranking_key_owned": True,
    }


def test_handoff_canonicalizes_new_aliases_in_a_different_shell_and_preserves_native_representation(
        storage_lab, monkeypatch):
    _, run, _ = storage_lab
    run.persistent_qdrant()
    monkeypatch.delenv("context_llm_model", raising=False)
    monkeypatch.delenv("dense_query_prefix", raising=False)
    run.environment("127.0.0.2")
    handoff = run.state / "handoff.env"
    assert handoff.is_file()
    expected = json.loads((run.state / "production-expected.json").read_text())["expected"]
    second_shell = dict(os.environ)
    second_shell.update(context_llm_model="unapproved-later-context-model",
                        dense_query_prefix="Unapproved later prefix | α\n",
                        ORIGINAL_UNRELATED_KEEP="Original | α\n$(printf literal-value)")
    program = """
import json,os
from mainframe_rag.config import Settings
from mainframe_rag.ingest.completion import representation_fingerprint
from mainframe_rag.ingest.rules_version import extraction_rules_version
from mainframe_rag.serve.colab_production import native_query_inputs
s=Settings(_env_file=None)
print(json.dumps({'prefix':s.dense_query_prefix,'context_model':s.context_llm_model,
                  'representation':representation_fingerprint(s,extraction_rules_version()),
                  'queries':list(native_query_inputs(s,'Compare CSA versus ECSA')),
                  'python':os.environ['PY'],'unrelated':os.environ['ORIGINAL_UNRELATED_KEEP']}))
"""
    child = subprocess.run(["sh", "-c", '. "$1"; exec "$PY" -c "$2"', "original-independent-shell",
                            str(handoff), program], env=second_shell, cwd=run.execution_root,
                           capture_output=True, text=True, timeout=5, check=False)
    assert child.returncode == 0, child.stdout + child.stderr
    actual = json.loads(child.stdout)
    assert actual == {
        "prefix": "Original: ", "context_model": None,
        "representation": expected["contract"]["representation_fingerprint"],
        "queries": ["Original: CSA", "Original: ECSA"], "python": run.manifest["execution"]["interpreter"],
        "unrelated": "Original | α\n$(printf literal-value)",
    }
    assert actual["python"] == sys.executable


def test_complete_production_admission_hands_exact_target_progress_and_native_settings_to_fresh_child(
        storage_lab, monkeypatch):
    root, run, _ = storage_lab
    sidecar = json.loads((run.state / "production-expected.json").read_text())
    target = {"namespace": "retained", "claim_uid": "claim-original", "pv_uid": "pv-original-uid",
              "storage_path": "/qdrant/storage", "logical_collection": "original_manuals"}
    assert sidecar["target"] == target
    progress_key = hashlib.sha256(json.dumps(
        {"target": target, "progress_binding": sidecar["progress_binding"]},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    progress = str(Path(run.config["data_state"]) / f"ingest-{progress_key}.jsonl")
    # Settings is case-insensitive. Later aliases must not replace the prepared
    # prefix or restore optional values that were deliberately absent.
    with monkeypatch.context() as ambient:
        ambient.setenv("dense_query_prefix", "Unapproved changed prefix | α\n")
        ambient.setenv("context_llm_model", "unapproved-context-model")
        ambient.setenv("bm25_cache_dir", str(root / "unapproved-cache"))
        result = _production_cli(run.state)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((root / "snapshot-workload.json").read_text())
    production = observed["production"]
    assert observed["cwd"] == str(run.execution_root) and observed["python"] == sys.executable
    assert production == {
        "CORPUS_DIR": run.config["corpus"], "PRODUCTION_CORPUS_DIR": run.config["corpus"],
        "PRODUCTION_GOLDEN_PATH": run.config["golden"], "LOCAL_INGEST_PROGRESS": progress,
        "LOCAL_STACK_DRYRUN": "0", "QDRANT_URL": f"http://127.0.0.1:{run.config['qdrant_port']}",
        "PYTHONPATH": str(run.execution_root / "src"),
        "GATEWAY_EMBED_MODEL": run.profile["servers"][1]["model_id"],
        "GATEWAY_EMBED_MODEL_REVISION": run.profile["servers"][1]["revision"],
        "collection": "original_manuals", "prefix": "Original: ", "context_model": None, "bm25_cache": None,
        "representation": sidecar["expected"]["contract"]["representation_fingerprint"],
    }
    manifest = json.loads((run.state / "manifest.json").read_text())
    assert manifest["production_progress"] == progress
    assert manifest["persistent_qdrant"]["claim_uid"] == "claim-original"
    assert manifest["persistent_qdrant"]["pv_uid"] == "pv-original-uid"
    assert manifest["workload_succeeded"] is True and manifest["released"] is True
    assert manifest["production"]["acceptance_certified"] is False
    assert not (run.state / "transport-receipt.json").exists()
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    next_state = _prepare_production_again(run, "ordinary-native-production-after-success")
    next_run = _production_cli(next_state)
    assert next_run.returncode == 0, next_run.stdout + next_run.stderr
    next_observed = json.loads((root / "snapshot-workload.json").read_text())
    assert next_observed["production"]["LOCAL_INGEST_PROGRESS"] == progress


@pytest.mark.parametrize("changed", ["source", "ledger", "wrapper", "runtime_software", "runtime_metadata"])
def test_production_drift_and_wrappers_refuse_before_native_workload_then_next_native_run_succeeds(
        storage_lab, changed):
    root, run, _ = storage_lab
    target = original = None
    if changed == "source":
        target = Path(run.config["corpus"]) / "original books/manual a.pdf"
        original = target.read_bytes()
        target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    elif changed == "ledger":
        target = Path(run.config["data_state"]) / "retained-index-binding.json"
        original = target.read_bytes()
        with target.open("a") as stream:
            stream.write("\n")
    elif changed.startswith("runtime_"):
        target = root / "original-runtime-observed.json"
        original = target.read_bytes()
        report = json.loads(original)
        if changed == "runtime_software":
            report["software"]["tokenizers"] = "unapproved-fixture-version"
        else:
            report["models"][0]["metadata_sha256"]["tokenizer.json"] = "f" * 64
        test_colab_production._json(target, report)
    rejected = _production_cli(run.state, workload=("/bin/true",) if changed == "wrapper" else ())
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert not (root / "snapshot-workload.json").exists(), "unadmitted native workload executed"
    manifest = json.loads((run.state / "manifest.json").read_text())
    assert manifest.get("workload_succeeded") is not True
    assert not (run.state / "transport-receipt.json").exists()
    allocated = [argv for kind, argv in _events(root) if kind == "colab" and argv[2] == "new"]
    if changed.startswith("runtime_"):
        assert len(allocated) == 1
        assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
        assert json.loads((root / "sessions.json").read_text()) == {}
        stopped = [argv[-1] for kind, argv in _events(root) if kind == "colab" and argv[2] == "stop"]
        assert stopped == [manifest["session"]]
    else:
        assert allocated == [], "source/ledger/wrapper refusal allocated a paid runtime"
        assert manifest["allocation_attempted"] is False
    if target is not None:
        target.write_bytes(original)
    next_state = _prepare_production_again(run, "ordinary-native-production-after-refusal")
    ordinary = _production_cli(next_state)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert json.loads((root / "snapshot-workload.json").read_text())["production"]["collection"] == "original_manuals"
    assert json.loads((root / "sessions.json").read_text()) == {}


def test_malformed_production_sidecar_cannot_block_exact_recovery_then_next_native_run_works(storage_lab, monkeypatch):
    root, run, _ = storage_lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "stop_fail")
    failed = _production_cli(run.state)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    manifest = json.loads((run.state / "manifest.json").read_text())
    assert manifest["workload_succeeded"] is True
    assert json.loads((root / "sessions.json").read_text()) == {manifest["session"]: "endpoint-owned"}
    (run.state / "production-expected.json").write_text("{original broken synthetic sidecar\n")
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    recovered = _production_cli(run.state, stop=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads((root / "sessions.json").read_text()) == {}
    stopped = [argv[-1] for kind, argv in _events(root) if kind == "colab" and argv[2] == "stop"]
    assert stopped == [manifest["session"], manifest["session"]]
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    assert not (run.state / "transport-receipt.json").exists()
    next_state = _prepare_production_again(run, "ordinary-native-production-after-sidecar-recovery")
    ordinary = _production_cli(next_state)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert json.loads((root / "snapshot-workload.json").read_text())["production"]["collection"] == "original_manuals"


def test_retained_storage_forwards_exact_proven_pod_without_logging_live_config(storage_lab):
    root, run, _ = storage_lab
    run.persistent_qdrant()
    proof = run.manifest["persistent_qdrant"]
    assert proof["pod_name"] == "qdrant-0" and proof["pod_uid"] == "pod-original"
    assert proof["container_name"] == "qdrant" and proof["container_id"] == "containerd://original"
    assert proof["claim_uid"] == "claim-original" and proof["pv_uid"] == "pv-original-uid"
    assert proof["storage_path"] == "/qdrant/storage" and proof["node_hostname"] == "actual-node-hostname"
    assert proof["url"] == f"http://127.0.0.1:{run.config['qdrant_port']}"
    calls = [argv[6:] for kind, argv in _events(root) if kind == "kubectl"]
    forwards = [argv for argv in calls if argv[0] == "port-forward"]
    assert forwards == [["port-forward", "--address", "127.0.0.1", "pod/qdrant-0",
                         f"{run.config['qdrant_port']}:6333"]]
    assert {tuple(argv) for argv in calls if argv[0] == "exec"} == {
        ("exec", "qdrant-0", "-c", "qdrant", "--", "cat", path)
        for path in ("/qdrant/config/initialize.sh", "/qdrant/config/config.yaml",
                     "/qdrant/config/production.yaml", "/qdrant/config/local.yaml")
    }
    assert calls[-1] == ["get", "pod", "qdrant-0", "-o", "json"]
    for path in run.state.iterdir():
        if path.is_file():
            assert b"ORIGINAL-FAKE-WRITER" not in path.read_bytes()
            assert b"ORIGINAL-FAKE-READER" not in path.read_bytes()
            assert b"ORIGINAL-FAKE-CONFIG-STDERR" not in path.read_bytes()
    assert run.command([sys.executable, "-c", "print('ordinary after storage proof')"],
                       seconds=3).stdout == "ordinary after storage proof\n"


@pytest.mark.parametrize("field", ["name", "namespace"])
def test_retained_service_must_match_explicit_selection_before_forwarding(storage_lab, monkeypatch, field):
    root, run, retained = storage_lab
    service = json.loads(json.dumps(retained["service"]))
    service["metadata"][field] = "different-selection"
    monkeypatch.setattr(run, "qdrant_object", lambda *_: service)
    with pytest.raises(controller.LabError, match="selected Qdrant service identity changed"):
        run.persistent_qdrant()
    assert run.qdrant_forward is None and "persistent_qdrant" not in run.manifest
    assert _events(root) == []


def test_sidecar_pvc_cannot_admit_ephemeral_qdrant_then_valid_storage_works(storage_lab):
    root, run, retained = storage_lab
    fixture = root / "storage-fixture.json"
    original = fixture.read_text()
    retained["pod"]["spec"]["containers"].append({"name": "sidecar", "volumeMounts": [
        {"name": "data", "mountPath": "/qdrant/storage"}]})
    retained["pod"]["spec"]["containers"][0]["volumeMounts"][0]["name"] = "ephemeral"
    retained["pod"]["spec"]["volumes"].append({"name": "ephemeral", "emptyDir": {}})
    fixture.write_text(json.dumps(retained))
    with pytest.raises(ValueError, match="writable PVC"):
        run.persistent_qdrant()
    assert run.qdrant_forward is None
    assert not any(kind == "kubectl" and "port-forward" in argv for kind, argv in _events(root))
    fixture.write_text(original)
    run.persistent_qdrant()
    assert run.manifest["persistent_qdrant"]["pod_uid"] == "pod-original"


@pytest.mark.parametrize("changed", ["uid", "container", "image", "ready", "forward"])
def test_retained_producer_rechecks_exact_identity_and_forward_after_readiness(storage_lab, monkeypatch, changed):
    _, run, _ = storage_lab
    if changed == "forward":
        wait_http = run.wait_http

        def lose_after_ready(url, child):
            wait_http(url, child)
            run.terminate(child)

        monkeypatch.setattr(run, "wait_http", lose_after_ready)
    else:
        monkeypatch.setenv("FAKE_QDRANT_CHANGE", changed)
    with pytest.raises(controller.LabError, match="retained Qdrant (producer identity or readiness changed|forward is unavailable)"):
        run.persistent_qdrant()
    assert "persistent_qdrant" not in run.manifest
    assert run.qdrant_forward is not None, "failed proof lost ownership of the spawned forward"


@pytest.mark.parametrize("changed", ["uid", "container", "image", "ready", "forward"])
def test_budget_poll_refuses_retained_forward_loss_or_producer_drift(storage_lab, monkeypatch, changed):
    root, run, _ = storage_lab
    run.persistent_qdrant()
    if changed == "forward":
        run.terminate(run.qdrant_forward)
    else:
        monkeypatch.setenv("FAKE_QDRANT_CHANGE", changed)
    with pytest.raises(controller.LabError, match="retained Qdrant"):
        run.budget(initial=True)
    assert not any(kind == "colab" and argv[2] == "usage" for kind, argv in _events(root))


def test_workload_cannot_succeed_after_retained_container_restart(storage_lab, monkeypatch):
    root, run, _ = storage_lab
    spawn = controller.Run.spawn

    def restart_after_workload(self, argv, name, **kwargs):
        child = spawn(self, argv, name, **kwargs)
        if name == "workload":
            child.wait(timeout=2)
            monkeypatch.setenv("FAKE_QDRANT_CHANGE", "container")
        return child

    monkeypatch.setattr(controller.Run, "spawn", restart_after_workload)
    assert _run_fake(run.state, ()) != 0
    manifest = json.loads((run.state / "manifest.json").read_text())
    assert manifest.get("workload_succeeded") is not True
    assert manifest["released"] is True and not (run.state / "transport-receipt.json").exists()
    assert json.loads((root / "sessions.json").read_text()) == {}


def test_running_budget_poll_lost_forward_releases_owned_vm(storage_lab, monkeypatch):
    root, run, _ = storage_lab
    budget = controller.Run.budget

    def lose_running_forward(self, *, initial=False):
        if self.manifest["phase"] == "running":
            self.terminate(self.qdrant_forward)
        return budget(self, initial=initial)

    monkeypatch.setattr(controller.Run, "budget", lose_running_forward)
    monkeypatch.setenv("FAKE_NATIVE_WORKLOAD_SECONDS", "3")
    assert _run_fake(run.state, ()) != 0
    manifest = json.loads((run.state / "manifest.json").read_text())
    assert manifest.get("workload_succeeded") is not True and manifest["released"] is True
    assert json.loads((root / "sessions.json").read_text()) == {}


def test_host_health_cannot_certify_failed_actual_gateway_network_then_next_run_succeeds(lab, monkeypatch):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "gateway_network_fail")
    probe = controller.Run.probe_gateway_network
    healthy = []

    def check_host_before_owner(self, bridge):
        for address in ("127.0.0.1", bridge):
            for key in ("reasoning_port", "embedding_port"):
                with controller.urllib.request.urlopen(f"http://{address}:{self.config[key]}/health", timeout=1) as response:
                    healthy.append(response.status)
        return probe(self, bridge)

    monkeypatch.setattr(controller.Run, "probe_gateway_network", check_host_before_owner)
    assert _run_fake(state) != 0
    assert healthy == [200, 200, 200, 200]
    manifest = json.loads((state / "manifest.json").read_text())
    owned = manifest["gateway_network"]
    assert owned["created"] is True and manifest["released"] is True
    assert "transport" not in manifest and not (state / "transport-receipt.json").exists()
    assert not any(kind == "colab" and argv[-1] == "/content/colab-transport-ok" for kind, argv in _events(root))
    runs = [argv for kind, argv in _events(root) if kind == "docker" and argv[0] == "run"]
    assert len(runs) == 1 and runs[0][runs[0].index("--network") + 1] == owned["network_id"]
    assert runs[0][-2:] == [owned["reasoning_url"], owned["embedding_url"]]
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    ordinary = _prepare_new(state, root / "ordinary-after-network-failure")
    assert _run_fake(ordinary) == 0
    following = json.loads((ordinary / "manifest.json").read_text())
    assert following["gateway_network"]["network_id"] != owned["network_id"]
    assert json.loads((root / "gateway-networks.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_G4_BF16"], indirect=True)
def test_normal_workload_gets_frozen_gateway_network_handoff_and_debug_disabled(lab, monkeypatch):
    root, state, _ = lab
    before = json.loads((state / "manifest.json").read_text())
    monkeypatch.setenv("GATEWAY_NAME", "ambient-decoy-gateway")
    monkeypatch.setenv("PG_NET", "ambient-decoy-network")
    monkeypatch.setenv("GATEWAY_DEBUG", "true")
    observed = root / "original-handoff-observation.json"
    keys = ("GATEWAY_NAME", "PG_NET", "LITELLM_IMAGE", "GATEWAY_DOCKER_BIN", "GATEWAY_DEBUG",
            "GATEWAY_REASONING_URL", "GATEWAY_EMBED_URL", "GATEWAY_TRANSPORT_NETWORK_ID",
            "GATEWAY_TRANSPORT_NETWORK_CREATED")
    program = ("import json,os; from pathlib import Path; "
               f"Path({str(observed)!r}).write_text(json.dumps({{key:os.environ[key] for key in {keys!r}}}))")
    assert _run_fake(state, (sys.executable, "-c", program)) == 0
    manifest = json.loads((state / "manifest.json").read_text())
    assert json.loads(observed.read_text()) == {
        "GATEWAY_NAME": before["config"]["gateway_name"], "PG_NET": before["config"]["gateway_network"],
        "LITELLM_IMAGE": IMAGE, "GATEWAY_DOCKER_BIN": before["tools"]["docker"], "GATEWAY_DEBUG": "0",
        "GATEWAY_REASONING_URL": f"http://127.0.0.2:{before['config']['reasoning_port']}/v1",
        "GATEWAY_EMBED_URL": f"http://127.0.0.2:{before['config']['embedding_port']}/v1",
        "GATEWAY_TRANSPORT_NETWORK_ID": manifest["gateway_network"]["network_id"],
        "GATEWAY_TRANSPORT_NETWORK_CREATED": "1",
    }
    assert manifest["gateway_network_cleanup"]["owned_removed_or_absent"] is True
    assert json.loads((root / "gateway-networks.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_G4_BF16"], indirect=True)
def test_colab_workload_uses_prepared_gateway_caps_despite_ambient_removal(lab, monkeypatch):
    root, state, _ = lab
    expected = {
        "GATEWAY_MEMORY_MIB": "2048", "PG_MEMORY_MIB": "512",
        "GATEWAY_LOG_MAX_BYTES": "8388608", "GATEWAY_DOCKER_LOG_MAX_BYTES": "8388608",
        "GATEWAY_DOCKER_LOG_FILES": "2",
    }
    prepared = json.loads((state / "manifest.json").read_text())
    assert prepared["config"]["gateway_limits"] == expected
    for name in expected:
        monkeypatch.setenv(name, "0")
    observed = root / "gateway-resource-observation.json"
    program = ("import json,os; from pathlib import Path; "
               f"Path({str(observed)!r}).write_text(json.dumps({{key:os.environ[key] for key in {tuple(expected)!r}}}))")
    assert _run_fake(state, (sys.executable, "-c", program)) == 0
    assert json.loads(observed.read_text()) == expected


def test_preexisting_gateway_network_is_pinned_and_preserved(lab):
    root, state, _ = lab
    manifest = json.loads((state / "manifest.json").read_text())
    existing = {manifest["config"]["gateway_network"]: "c" * 64}
    (root / "gateway-networks.json").write_text(json.dumps(existing))
    assert _run_fake(state) == 0
    completed = json.loads((state / "manifest.json").read_text())
    assert completed["gateway_network"]["network_id"] == "c" * 64
    assert completed["gateway_network"]["created"] is False
    assert completed["gateway_network_cleanup"] == {
        "completed": True, "owned_removed_or_absent": False, "preexisting_preserved": True}
    assert json.loads((root / "gateway-networks.json").read_text()) == existing
    assert not any(kind == "docker" and argv[:2] in (["network", "create"], ["network", "rm"])
                   for kind, argv in _events(root))


@pytest.mark.parametrize("failure", ["timeout", "interrupt"])
def test_probe_ownership_survives_timeout_or_interrupt_before_transport_ok(lab, monkeypatch, failure):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "gateway_network_probe_hang")
    if failure == "timeout":
        command = controller.Run.command

        def shorten_probe(self, argv, **kwargs):
            if argv[-1:] == ["transport-probe"]:
                kwargs["seconds"] = .8
            return command(self, argv, **kwargs)

        monkeypatch.setattr(controller.Run, "command", shorten_probe)
    else:
        observe = controller.Run.observe_gateway_network

        def interrupt_after_saved_ownership(self, raw, bridge):
            accepted = observe(self, raw, bridge)
            if accepted:
                persisted = json.loads((self.state / "manifest.json").read_text())
                assert persisted["gateway_network"] == self.manifest["gateway_network"]
                raise KeyboardInterrupt
            return accepted

        monkeypatch.setattr(controller.Run, "observe_gateway_network", interrupt_after_saved_ownership)
    result = _run_fake(state)
    assert result == (130 if failure == "interrupt" else 1)
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["gateway_network"]["created"] is True and manifest["released"] is True
    assert manifest["gateway_network_cleanup"]["completed"] is True
    events = [entry["event"] for entry in manifest["events"]]
    assert events.index("gateway_network_ownership_observed") < events.index(
        "run_interrupted" if failure == "interrupt" else "run_failed")
    assert "transport" not in manifest and not (state / "transport-receipt.json").exists()
    assert json.loads((root / "gateway-networks.json").read_text()) == {}


@pytest.mark.parametrize("failure", ["gateway_network_release_fail", "gateway_network_release_hang"])
def test_network_release_has_independent_budget_after_vm_release_then_recovery_works(lab, monkeypatch, failure):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", failure)
    command = controller.Run.command
    release_windows = []

    def record_release_window(self, argv, **kwargs):
        if argv[-1:] == ["transport-release"]:
            assert 0 < kwargs["cleanup_deadline"] - time.time() <= 5
            assert kwargs["seconds"] == 5 and kwargs["cleanup"] is True
            started = time.monotonic()
            try:
                return command(self, argv, **kwargs)
            finally:
                release_windows.append(time.monotonic() - started)
        return command(self, argv, **kwargs)

    monkeypatch.setattr(controller.Run, "command", record_release_window)
    assert _run_fake(state) != 0
    assert release_windows and max(release_windows) < 8
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert manifest["gateway_network_cleanup"] == {"completed": False}
    assert not (state / "transport-receipt.json").exists()
    calls = _events(root)
    stopped = next(index for index, (kind, argv) in enumerate(calls) if kind == "colab" and argv[2] == "stop")
    removed = next(index for index, (kind, argv) in enumerate(calls) if kind == "docker" and argv[:2] == ["network", "rm"])
    assert stopped < removed
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "success")
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    ordinary = _prepare_new(state, root / "ordinary-after-network-release-recovery")
    assert _run_fake(ordinary) == 0


def test_exact_network_cleanup_remains_available_after_absolute_vm_deadline(lab):
    root, state, _ = lab
    run = controller.Run(state)
    network = run.config["gateway_network"]
    (root / "gateway-networks.json").write_text(json.dumps({network: "e" * 64}))
    ownership = {"network_name": network, "network_id": "e" * 64, "created": True,
                 "gateway_image": IMAGE,
                 "reasoning_url": f"http://127.0.0.2:{run.config['reasoning_port']}/v1",
                 "embedding_url": f"http://127.0.0.2:{run.config['embedding_port']}/v1"}
    assert run.observe_gateway_network(json.dumps(ownership), "127.0.0.2") is True
    run.expiry = run.work_expiry = time.time() - 10
    run.release_gateway_network()
    assert run.manifest["gateway_network_cleanup"]["completed"] is True
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    assert not any(kind == "colab" for kind, _ in _events(root))


@pytest.mark.parametrize("changed", ["created", "id", "network", "image", "url", "extra", "replacement"])
def test_gateway_ownership_observer_refuses_invalid_or_replaced_objects(lab, changed):
    _, state, _ = lab
    run = controller.Run(state)
    ownership = {"network_name": run.config["gateway_network"], "network_id": "e" * 64,
                 "created": False, "gateway_image": IMAGE,
                 "reasoning_url": f"http://127.0.0.2:{run.config['reasoning_port']}/v1",
                 "embedding_url": f"http://127.0.0.2:{run.config['embedding_port']}/v1"}
    if changed == "created":
        ownership["created"] = 0
    elif changed == "id":
        ownership["network_id"] = "e" * 63
    elif changed == "network":
        ownership["network_name"] = "different-name"
    elif changed == "image":
        ownership["gateway_image"] = "different-image"
    elif changed == "url":
        ownership["reasoning_url"] = "http://127.0.0.1:8000/v1"
    elif changed == "extra":
        ownership["unrecognized"] = True
    else:
        assert run.observe_gateway_network(json.dumps(ownership), "127.0.0.2") is True
        ownership["network_id"] = "f" * 64
        ownership["created"] = True
    assert run.observe_gateway_network(json.dumps(ownership), "127.0.0.2") is False
    if changed == "replacement":
        assert run.manifest["gateway_network"]["network_id"] == "e" * 64
        assert run.manifest["gateway_network"]["created"] is False
    else:
        assert "gateway_network" not in run.manifest


def test_explicit_gateway_names_are_frozen_at_prepare_and_reused_by_owner_probe(lab, monkeypatch):
    root, base, _ = lab
    monkeypatch.setenv("GATEWAY_NAME", "original-custom-gateway")
    monkeypatch.setenv("PG_NET", "original-custom-model-network")
    state = _prepare_new(base, root / "custom-gateway-network-run")
    monkeypatch.setenv("GATEWAY_NAME", "later-ambient-gateway")
    monkeypatch.setenv("PG_NET", "later-ambient-network")
    assert _run_fake(state) == 0
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["config"]["gateway_name"] == "original-custom-gateway"
    assert manifest["gateway_network"]["network_name"] == "original-custom-model-network"
    assert manifest["transport"]["gateway_name"] == "original-custom-gateway"
    assert manifest["transport"]["network_name"] == "original-custom-model-network"
    assert json.loads((root / "gateway-networks.json").read_text()) == {}


def test_same_name_replacement_is_never_removed_and_next_preexisting_run_works(lab):
    root, state, _ = lab
    networks = root / "gateway-networks.json"
    program = ("import json,os; from pathlib import Path; "
               f"p=Path({str(networks)!r}); "
               "p.write_text(json.dumps({'retained-old-name':os.environ['GATEWAY_TRANSPORT_NETWORK_ID'],"
               "os.environ['PG_NET']:'d'*64}))")
    assert _run_fake(state, (sys.executable, "-c", program)) != 0
    manifest = json.loads((state / "manifest.json").read_text())
    old_id = manifest["gateway_network"]["network_id"]
    network_name = manifest["config"]["gateway_network"]
    assert json.loads(networks.read_text()) == {"retained-old-name": old_id, network_name: "d" * 64}
    assert manifest["released"] is True and manifest["gateway_network_cleanup"]["completed"] is False
    assert not any(kind == "docker" and argv[:2] == ["network", "rm"] for kind, argv in _events(root))
    # Model the operator independently removing the detached original object.
    # Recovery must observe its absence while preserving the replacement.
    networks.write_text(json.dumps({network_name: "d" * 64}))
    assert _controller_call(["stop", "--state", str(state)]) == 0
    ordinary = _prepare_new(state, root / "ordinary-with-preserved-replacement")
    assert _run_fake(ordinary) == 0
    following = json.loads((ordinary / "manifest.json").read_text())
    assert following["gateway_network"]["network_id"] == "d" * 64
    assert following["gateway_network"]["created"] is False
    assert json.loads(networks.read_text()) == {network_name: "d" * 64}


def test_two_hour_poll_metadata_is_bounded_linear_and_critical_state_is_immediate(monkeypatch):
    # Virtual state only: no fixture filesystem, command, account or corpus.
    clock = [1_000_000.0]
    monkeypatch.setattr(controller.time, "time", lambda: clock[0])
    run = controller.Run.__new__(controller.Run)
    run.state = Path("/original-virtual-controller-state")
    run.config = {"dataset": "synthetic", "cu_cap": 20, "cu_floor": 10,
                  "gateway_image": IMAGE, "gateway_name": "local-litellm-gateway",
                  "gateway_network": "local-litellm-gateway-net", "gateway_debug": "0"}
    run.manifest = {"schema": 1, "session": "rag-" + "1" * 16, "endpoint": "endpoint-owned",
                    "phase": "running", "allocation_attempted": True, "events": [],
                    "starting_balance": 200.0, "config": run.config,
                    "profile": controller.build_profile("COLAB_G4_BF16")}
    run.qdrant_forward = None
    run.work_expiry = run.expiry = clock[0] + 7200
    run.cleanup_reserve = 90
    tick = [0]

    def usage(*args, **kwargs):
        assert args == ("usage",) and kwargs == {"cleanup": False, "seconds": 30}
        output = (f"Current balance: {200 - tick[0] / 10000:.4f} compute units\n"
                  "Usage rate: 8.00/hr\nActive assignments: 1\n")
        return subprocess.CompletedProcess(["original-fake-colab"], 0, output, "")

    monkeypatch.setattr(run, "colab", usage)
    writes = 0
    serialized_bytes = 0
    last_snapshot = {}

    def private_sink(path, value):
        nonlocal writes, serialized_bytes, last_snapshot
        assert path == run.state / "manifest.json"
        writes += 1
        # Once red has exceeded the byte allowance, keep simulating all polls
        # without needlessly serializing the old growing list for another GB.
        if serialized_bytes <= 32 * 1024 * 1024:
            serialized_bytes += len(json.dumps(value, indent=2).encode()) + 1
        last_snapshot = {"phase": value["phase"], "endpoint": value["endpoint"],
                         "released": value.get("released"), "release_evidence": value.get("release_evidence")}

    monkeypatch.setattr(controller, "private_json", private_sink)
    for index in range(7200):
        tick[0] = index
        clock[0] = 1_000_000.0 + index
        run.budget()
    before_critical = writes
    run.manifest["endpoint"] = "endpoint-exact-rebound"
    run.save("session_bound")
    assert writes == before_critical + 1 and last_snapshot["endpoint"] == "endpoint-exact-rebound"
    proof = {"session": run.manifest["session"], "endpoint": run.manifest["endpoint"],
             "absent": True, "observed_at": clock[0]}
    run.manifest.update(phase="released", released=True, release_evidence=proof)
    run.save("cleanup_complete")
    assert writes == before_critical + 2
    assert last_snapshot == {"phase": "released", "endpoint": "endpoint-exact-rebound",
                             "released": True, "release_evidence": proof}
    assert writes <= 1443
    assert serialized_bytes < 32 * 1024 * 1024
    assert len(run.manifest["events"]) <= 64
    assert len(run.manifest["cu_observations"]) <= 32
    assert run.manifest["event_history"] == {"total": 7202, "dropped": 7202 - len(run.manifest["events"])}
    summary = run.manifest["cu_summary"]
    assert summary["total"] == 7200 and summary["dropped"] == 7200 - len(run.manifest["cu_observations"])
    assert summary["min_balance"] == 199.2801 and summary["max_rate_per_hour"] == 8.0
    assert summary["first"] == {"time": 1_000_000.0, "balance": 200.0, "rate_per_hour": 8.0, "assignments": 1}
    assert summary["last"] == {"time": 1_007_199.0, "balance": 199.2801, "rate_per_hour": 8.0, "assignments": 1}


def test_manifest_disk_full_during_cleanup_cannot_prevent_exact_vm_release(lab, monkeypatch):
    root, state, _ = lab
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned", phase="running")
    unrelated = {"rag-" + "b" * 16: "endpoint-unrelated"}
    _seed_owned_assignment(run, root, unrelated=unrelated)
    remote = root / "remote"
    remote.mkdir()
    (remote / "colab-artifacts.tgz").write_bytes(b"original-fake-diagnostics")
    child = run.spawn([sys.executable, "-c", "import time; time.sleep(30)"], "workload")
    run.workload = child
    run.expiry = time.time() + 10
    private_json = controller.private_json

    def disk_full(path, value):
        if path == state / "manifest.json":
            raise OSError(28, "original fake metadata disk full")
        return private_json(path, value)

    monkeypatch.setattr(controller, "private_json", disk_full)
    try:
        assert run.cleanup() is False, "undurable cleanup metadata cannot be certified"
    finally:
        run.terminate(child)
    assert json.loads((root / "sessions.json").read_text()) == unrelated
    assert run.manifest["released"] is True and run.manifest["release_evidence"]["absent"] is True
    assert run.manifest["metadata_persistence_failed"] is True
    assert run.cleanup_in_progress is False
    assert not (state / "transport-receipt.json").exists()
    stops = [argv[-1] for kind, argv in _events(root) if kind == "colab" and argv[2] == "stop"]
    assert stops == [run.manifest["session"]]
    with pytest.raises(OSError):
        run.save("ordinary_critical_state_after_cleanup")
    monkeypatch.setattr(controller, "private_json", private_json)
    assert run.command([sys.executable, "-c", "print('ordinary after metadata failure')"],
                       seconds=3).stdout == "ordinary after metadata failure\n"


def test_successful_cpu_workload_with_undurable_cleanup_cannot_issue_receipt(lab, monkeypatch, capsys):
    root, state, _ = lab
    private_json, cleanup = controller.private_json, controller.Run.cleanup
    cleaning = False
    observed = []

    def disk_full_only_during_cleanup(path, value):
        if cleaning and path == state / "manifest.json":
            raise OSError(28, "original fake cleanup metadata disk full")
        return private_json(path, value)

    def cleanup_with_disk_full(self):
        nonlocal cleaning
        observed.append(self)
        cleaning = True
        try:
            return cleanup(self)
        finally:
            cleaning = False

    monkeypatch.setattr(controller, "private_json", disk_full_only_during_cleanup)
    monkeypatch.setattr(controller.Run, "cleanup", cleanup_with_disk_full)
    assert _run_fake(state) == 1
    run = observed[0]
    assert run.manifest["workload_succeeded"] is True
    assert run.manifest["released"] is True and run.manifest["release_evidence"]["absent"] is True
    assert run.manifest["metadata_persistence_failed"] is True
    assert "Cleanup metadata could not be persisted" in capsys.readouterr().err
    assert not (state / "transport-receipt.json").exists()
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert json.loads((root / "gateway-networks.json").read_text()) == {}
    # Recovery uses the last durable exact ownership state, then the next fresh
    # ordinary run must work without adopting the failed run's acceptance.
    monkeypatch.setattr(controller.Run, "cleanup", cleanup)
    monkeypatch.setattr(controller, "private_json", private_json)
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert not (state / "transport-receipt.json").exists()
    ordinary = _prepare_new(state, root / "ordinary-after-undurable-cleanup")
    assert _run_fake(ordinary) == 0
    assert (ordinary / "transport-receipt.json").exists()


def test_normal_critical_metadata_write_failure_refuses_before_allocation_then_normal_run_works(lab, monkeypatch):
    root, state, _ = lab
    private_json = controller.private_json

    def disk_full(path, value):
        if path == state / "manifest.json":
            raise OSError(28, "original fake approval metadata disk full")
        return private_json(path, value)

    monkeypatch.setattr(controller, "private_json", disk_full)
    assert _run_fake(state) == 2
    assert _events(root) == []
    assert not (root / "sessions.json").exists()
    assert not (state / "transport-receipt.json").exists()
    assert json.loads((state / "manifest.json").read_text())["phase"] == "prepared"
    monkeypatch.setattr(controller, "private_json", private_json)
    assert _run_fake(state) == 0
    assert (state / "transport-receipt.json").exists()


@pytest.mark.parametrize("address", ["127.0.0.1", "127.0.0.2"])
def test_port_admission_refuses_live_listener_then_allows_closed_transport_reuse(address):
    run = controller.Run.__new__(controller.Run)
    with socket.socket() as listener:
        listener.settimeout(1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        with pytest.raises(controller.LabError):
            run.free_port(address, port)
        with socket.create_connection((address, port), timeout=1) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.settimeout(1)
                # The original server actively closes first: its finite TCP
                # TIME_WAIT must not block the next reusable transport.
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert accepted.recv(1) == b""
    run.free_port(address, port)


def test_read_only_existing_recovery_lock_keeps_exclusive_ownership_then_next_run_works(lab, monkeypatch):
    root, state, _ = lab
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned", phase="running")
    run.save("original_owned_assignment")
    _seed_owned_assignment(run, root)
    remote = root / "remote"
    remote.mkdir()
    (remote / "colab-artifacts.tgz").write_bytes(b"original-fake-diagnostics")
    lock_path = state / "run.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o400)
    inode = lock_path.stat().st_ino
    flock = fcntl.flock
    observed = []

    def inspect_recovery_lock(descriptor, operation):
        number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
        if os.fstat(number).st_ino == inode:
            # This tests the real descriptor access mode, including on a root
            # test host that could bypass the file's mode0400 permission.
            assert fcntl.fcntl(number, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            observed.append(operation)
        return flock(descriptor, operation)

    with lock_path.open("rb") as owner:
        flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(controller.fcntl, "flock", inspect_recovery_lock)
        assert _controller_call(["stop", "--state", str(state)]) == 2
        assert _events(root) == [], "recovery bypassed the active exclusive owner"
        assert json.loads((root / "sessions.json").read_text()) == {run.manifest["session"]: "endpoint-owned"}
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert observed == [fcntl.LOCK_EX | fcntl.LOCK_NB] * 2
    assert lock_path.stat().st_ino == inode and lock_path.stat().st_mode & 0o777 == 0o400
    assert json.loads((root / "sessions.json").read_text()) == {}
    assert not (state / "transport-receipt.json").exists()
    ordinary = _prepare_new(state, root / "ordinary-after-read-only-recovery")
    assert _run_fake(ordinary) == 0


@pytest.mark.parametrize("unsafe", ["missing", "symlink", "fifo", "public", "unowned"])
def test_recovery_refuses_unsafe_existing_lock_without_tool_calls(lab, monkeypatch, unsafe):
    root, state, _ = lab
    lock = state / "run.lock"
    if unsafe == "symlink":
        target = root / "original-other-lock"
        target.write_bytes(b"")
        lock.symlink_to(target)
    elif unsafe == "fifo":
        os.mkfifo(lock, 0o600)
    elif unsafe != "missing":
        lock.write_bytes(b"")
        lock.chmod(0o644 if unsafe == "public" else 0o600)
        if unsafe == "unowned":
            inode = lock.stat().st_ino
            fstat = os.fstat

            def wrong_owner(descriptor):
                info = fstat(descriptor)
                if info.st_ino == inode:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1)
                return info

            monkeypatch.setattr(controller.os, "fstat", wrong_owner)
    assert _controller_call(["stop", "--state", str(state)]) == 2
    assert _events(root) == []


def test_normal_allocation_cannot_bypass_unwritable_lock_creation_then_next_run_works(lab, monkeypatch):
    root, state, _ = lab
    open_descriptor = os.open

    def read_only_state(path, flags, *args, **kwargs):
        if path == state / "run.lock":
            raise OSError(30, "original fake read-only allocation lock")
        return open_descriptor(path, flags, *args, **kwargs)

    monkeypatch.setattr(controller.os, "open", read_only_state)
    assert _run_fake(state) == 2
    assert _events(root) == []
    assert not (state / "run.lock").exists()
    monkeypatch.setattr(controller.os, "open", open_descriptor)
    assert _run_fake(state) == 0


def test_private_key_unlink_failure_is_incomplete_after_exact_release_then_recovery_works(lab, monkeypatch):
    root, state, _ = lab
    unlink = Path.unlink
    attempted = []

    def failed_private_key(self, *args, **kwargs):
        if self.parent == state and self.name in ("id_ed25519_colab_rag", "id_ed25519_colab_rag.pub"):
            attempted.append(self.name)
            if self.name == "id_ed25519_colab_rag":
                raise PermissionError(13, "original fake private key unlink refusal")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_private_key)
    assert _run_fake(state) == 1
    manifest = json.loads((state / "manifest.json").read_text())
    assert manifest["released"] is True and manifest["release_evidence"]["absent"] is True
    assert manifest["private_key_cleanup_failed"] is True
    assert manifest["events"][-1]["event"] == "cleanup_incomplete"
    assert attempted == ["id_ed25519_colab_rag", "id_ed25519_colab_rag.pub"]
    assert (state / "id_ed25519_colab_rag").exists() and not (state / "id_ed25519_colab_rag.pub").exists()
    assert not (state / "transport-receipt.json").exists()
    assert json.loads((root / "sessions.json").read_text()) == {}
    monkeypatch.setattr(Path, "unlink", unlink)
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert not (state / "id_ed25519_colab_rag").exists()
    assert not (state / "transport-receipt.json").exists()
    ordinary = _prepare_new(state, root / "ordinary-after-private-key-recovery")
    assert _run_fake(ordinary) == 0


def test_manifest_and_diagnostic_sink_disk_full_cannot_interrupt_release_then_recovery_works(lab, monkeypatch):
    root, state, _ = lab
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned", phase="running")
    run.save("original_owned_assignment")
    with controller.state_lock(state, recovery=False):
        pass
    unrelated = {"rag-" + "b" * 16: "endpoint-unrelated"}
    _seed_owned_assignment(run, root, unrelated=unrelated)
    remote = root / "remote"
    remote.mkdir()
    (remote / "colab-artifacts.tgz").write_bytes(b"original-fake-diagnostics")
    child = run.spawn([sys.executable, "-c", "import time; time.sleep(30)"], "workload")
    run.workload = child
    run.expiry = time.time() + 10
    private_json = controller.private_json

    def disk_full(path, value):
        if path == state / "manifest.json":
            raise OSError(28, "original fake metadata disk full")
        return private_json(path, value)

    class FullDiagnosticSink:
        def write(self, value):
            raise OSError(28, "original fake stderr disk full")

        def flush(self):
            raise OSError(28, "original fake stderr disk full")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(controller, "private_json", disk_full)
            fault.setattr(controller.sys, "stderr", FullDiagnosticSink())
            assert run.cleanup() is False
    finally:
        run.terminate(child)
    assert run.manifest["released"] is True and run.manifest["release_evidence"]["absent"] is True
    assert run.manifest["metadata_persistence_failed"] is True and run.metadata_warning_printed is True
    assert json.loads((root / "sessions.json").read_text()) == unrelated
    assert not (state / "transport-receipt.json").exists()
    assert run.command([sys.executable, "-c", "print('ordinary after both sinks failed')"],
                       seconds=3).stdout == "ordinary after both sinks failed\n"
    assert _controller_call(["stop", "--state", str(state)]) == 0
    assert not (state / "transport-receipt.json").exists()
    # Model the unrelated owner's independent release, never a controller stop.
    (root / "sessions.json").write_text("{}")
    ordinary = _prepare_new(state, root / "ordinary-after-diagnostic-sink-recovery")
    assert _run_fake(ordinary) == 0

"""Lifecycle through executable CLI boundaries; never invoke an installed Colab."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts import colab as controller
from tests.test_colab_storage import retained as retained

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "example.invalid/gateway@sha256:" + "a" * 64

# Output and auto-allocation semantics follow google-colab-cli v0.7.4
# commands/session.py, commands/usage.py and commands/ssh.py. The fake SSH
# really binds the four requested local sockets; Docker's host-gateway fake
# probes those sockets rather than declaring them reachable by fiat.
FAKE = r'''
import http.server, json, os, pathlib, signal, socketserver, subprocess, sys, threading, time
root=pathlib.Path(os.environ['FAKE_COLAB_ROOT'])
with (root/'fake-pids').open('a') as f: f.write(str(os.getpid())+'\n')
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
def event(kind, data):
    with (root/'events.jsonl').open('a') as f: f.write(json.dumps([kind,data])+'\n')
event(name,args)
scenario=os.environ.get('FAKE_COLAB_SCENARIO','success')
remote=root/'remote'; remote.mkdir(exist_ok=True)
def path(p): return remote/pathlib.Path(p).name
def diagnostic(value):
    target=path('/content/colab-diagnostics.json')
    temporary=target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary,target)
if name=='colab':
    assert args[:2]==['--auth','oauth2']
    cmd=args[2]; rest=args[3:]
    sessions=root/'sessions.json'
    state=json.loads(sessions.read_text()) if sessions.exists() else {}
    session=rest[rest.index('-s')+1] if '-s' in rest else None
    if cmd=='version': print('0.7.4')
    elif cmd=='usage':
        reads=root/'usage-reads'
        count=int(reads.read_text()) if reads.exists() else 0
        reads.write_text(str(count+1))
        balance='175.00' if scenario=='spent_cap_zero_rate' and count else '200.00'
        rate='0.00' if scenario=='spent_cap_zero_rate' else '8.00'
        print('Current balance: '+balance+' compute units\nUsage rate: '+rate+'/hr\nActive assignments: '+str(len(state)))
    elif cmd=='sessions':
        if scenario=='slow_export' and not (root/'first-sessions').exists():
            (root/'first-sessions').touch()
            time.sleep(10)
        if not state: print('[colab] No active sessions found on server.')
        for n,e in state.items(): print(f'[{n}] {e} | Hardware: G4 | Shape: HIGH_RAM | Variant: GPU')
    elif cmd=='new':
        assert session not in state
        for previous in remote.iterdir(): previous.unlink()
        state[session]='endpoint-owned'; sessions.write_text(json.dumps(state))
        print('[colab] Session READY.')
    elif cmd=='stop':
        if scenario=='stop_fail': sys.exit(1)
        state.pop(session,None); sessions.write_text(json.dumps(state))
        if scenario=='noisy_stop': print('x'*262144)
        print('[colab] Session terminated.')
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
            until=time.monotonic()+12
            while not path('/content/colab-stop').exists() and time.monotonic()<until:
                if path('/content/colab-transport-ok').exists():
                    diagnostic({'stage':'ready'})
                time.sleep(.02)
            path('/content/colab-artifacts.tgz').write_bytes(b'private-fake-diagnostics')
    else: raise AssertionError('unsupported real CLI command: '+cmd)
elif name=='ssh':
    assert '-N' in args and args.count('-L')==4
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
            threading.Thread(target=server.serve_forever,daemon=True).start()
    if scenario=='tunnel_loss': time.sleep(.5); sys.exit(7)
    until=time.monotonic()+12
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
        print(json.dumps(output))
    elif rest[0]=='exec':
        assert rest[1:6]==['qdrant-0','-c','qdrant','--','cat']
        assert rest[6] in fixture['files'] and len(rest)==7
        print(fixture['files'][rest[6]],end='')
        print('ORIGINAL-FAKE-CONFIG-STDERR',file=sys.stderr)
    elif rest[0]=='port-forward':
        assert rest[:4]==['port-forward','--address','127.0.0.1','pod/qdrant-0']
        local,remote_port=rest[4].split(':'); assert remote_port=='6333'
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path=='/readyz'
                self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
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


def _controller_call(argv):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    previous_umask = os.umask(0o077)
    try:
        return controller.main(argv)
    finally:
        os.umask(previous_umask)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _prepare_new(source_state, state, *, profile="COLAB_CPU_TRANSPORT", extra=()):
    original = json.loads((source_state / "manifest.json").read_text())
    config, tools = original["config"], original["tools"]
    argv = ["prepare", "--state", str(state), "--dataset", "synthetic", "--profile", profile,
            "--gateway-image", config["gateway_image"], "--deadline-seconds", "30",
            "--reasoning-port", str(config["reasoning_port"]), "--embedding-port", str(config["embedding_port"])]
    for name in ("colab", "ssh", "ssh_keygen", "docker"):
        argv += ["--" + name.replace("_", "-"), tools[name]]
    assert _controller_call(argv + list(extra)) == 0
    return state


def _run_fake(state, workload=("/bin/true",)):
    return _controller_call(["run", "--state", str(state), "--approve-cu", "20", "--", *workload])


def _events(root):
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def lab(tmp_path, monkeypatch, request):
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
        prepared = subprocess.run(command + ["prepare", "--state", str(cpu), "--profile", "COLAB_CPU_TRANSPORT"]
                                  + common, capture_output=True, text=True, timeout=15, check=False)
        assert prepared.returncode == 0, prepared.stderr
        proof = subprocess.run(command + ["run", "--state", str(cpu), "--approve-cu", "20", "--", "/bin/true"],
                               capture_output=True, text=True, timeout=15, check=False)
        assert proof.returncode == 0, proof.stdout + proof.stderr
        inference_lock = tmp_path / "inference.lock"
        inference_lock.write_text(_reference_lock())
        prerequisites = ["--inference-lock", str(inference_lock),
                         "--transport-receipt", str(cpu / "transport-receipt.json")]
        (tmp_path / "events.jsonl").write_text("")
    result = subprocess.run(command + ["prepare", "--state", str(state), "--profile", profile] + common + prerequisites,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
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


@pytest.fixture
def storage_lab(lab, retained):
    root, base, _ = lab
    kubectl = root / "bin/kubectl"
    kubectl.write_text(f"#!{sys.executable}\n" + FAKE)
    kubectl.chmod(0o755)
    (root / "storage-fixture.json").write_text(json.dumps(retained))
    corpus = root / "original-corpus-fixture"
    corpus.mkdir()
    golden = root / "original-golden.jsonl"
    golden.write_text('{"question":"original test question"}\n')
    inventory = root / "original-inventory.json"
    inventory.write_text('{"max_document_tokens":8,"max_query_tokens":3}\n')
    kubeconfig = root / "original-kubeconfig"
    kubeconfig.write_text("original fake Kubernetes client input\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    state = _prepare_new(base, root / "retained-qdrant-run", extra=[
        "--dataset", "production", "--corpus", str(corpus), "--golden", str(golden),
        "--inventory", str(inventory), "--data-state", str(root / "retained-data"),
        "--kubeconfig", str(kubeconfig), "--kube-context", "original-kind", "--namespace", "retained",
        "--qdrant-service", "qdrant", "--kubectl", str(kubectl), "--qdrant-port", str(port),
    ])
    run = controller.Run(state)
    run.cleanup_reserve = 10
    try:
        yield root, run, retained
    finally:
        for child in reversed(run.children):
            run.terminate(child)


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
    assert observed.read_text() == str(ROOT)
    assert json.loads((root / "sessions.json").read_text()) == {}


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_separate_run_states_share_assignment_ownership_and_allow_next_run(lab, monkeypatch):
    root, state, _ = lab
    second = root / "second-private-run"
    second.mkdir(mode=0o700)
    manifest = json.loads((state / "manifest.json").read_text())
    manifest["session"] = "rag-" + "b" * 16
    (second / "manifest.json").write_text(json.dumps(manifest))
    entered = []
    monkeypatch.setattr(controller.Run, "run", lambda self, approval, workload: entered.append(self.state) or 0)
    with controller.assignment_lock():
        assert controller.main(["run", "--state", str(second), "--approve-cu", "20"]) == 2
        assert entered == [], "a distinct state bypassed assignment ownership"
    assert controller.main(["run", "--state", str(second), "--approve-cu", "20"]) == 0
    assert entered == [second]


@pytest.mark.parametrize("lab", ["COLAB_CPU_TRANSPORT"], indirect=True)
def test_slow_export_cannot_consume_owned_vm_release_budget(lab, monkeypatch):
    root, state, _ = lab
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", "slow_export")
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned")
    (root / "sessions.json").write_text(json.dumps({run.manifest["session"]: "endpoint-owned"}))
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
    prepared = json.loads((state / "manifest.json").read_text())
    ledger = {"schema": 1, "used": {"work": 4 * 1024 * 1024,
                                    "export": 256 * 1024, "release": 512 * 1024}}
    (state / "capture-budget.json").write_text(json.dumps(ledger) if budget == "exhausted" else "[]")
    monkeypatch.setenv("FAKE_COLAB_SCENARIO", scenario)
    run = controller.Run(state)
    run.manifest.update(allocation_attempted=True, endpoint="endpoint-owned")
    (root / "sessions.json").write_text(json.dumps({run.manifest["session"]: "endpoint-owned"}))
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
    fresh.mkdir(mode=0o700)
    (fresh / "manifest.json").write_text(json.dumps(prepared))
    ordinary = controller.Run(fresh)
    assert ordinary.command([sys.executable, "-c", "print('ordinary after release')"],
                            seconds=3).stdout == "ordinary after release\n"


def test_prepared_dirty_source_change_refuses_before_allocation_then_ordinary_cpu_run(lab, monkeypatch):
    root, base, _ = lab
    sources = root / "original-source-fixture"
    for relative in controller.SOURCE_REQUIRED:
        path = sources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original source fixture\n")
    real_source_identity = controller.source_identity
    monkeypatch.setattr(controller, "source_identity", lambda _: real_source_identity(sources))
    state = _prepare_new(base, root / "bound-source-run")
    changed = sources / "src/mainframe_rag/serve/colab_profiles.py"
    changed.write_text("changed source at the same HEAD\n")
    assert _run_fake(state) == 2
    assert _events(root) == [], "source drift reached a tool or allocated a runtime"
    assert json.loads((state / "manifest.json").read_text())["allocation_attempted"] is False
    changed.write_text("original source fixture\n")
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
    assert set(tuple(argv) for argv in calls if argv[0] == "exec") == {
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
    assert _run_fake(run.state) != 0
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
    assert _run_fake(run.state, (sys.executable, "-c", "import time; time.sleep(3)")) != 0
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
        assert args == ("usage",) and kwargs == {}
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
    (root / "sessions.json").write_text(json.dumps({run.manifest["session"]: "endpoint-owned", **unrelated}))
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

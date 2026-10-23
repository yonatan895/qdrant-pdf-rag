"""Hermetic contract for the Kind rehearsal's network egress gate (issue #809).

The kernel packet filter cannot run in unit tests, so the shell script is run
against stub iptables/sudo/setpriv that keep real rule state; the probe is run
with genuine loopback sockets; and the workflow is parsed to prove that every
operator step after the boundary goes through the gate. The live proof that
the rules actually reject packets is the lane's own probe steps in e2e.yml.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'scripts/ci/egress_gate.sh'
PROBE = ROOT / 'scripts/ci/egress_probe.py'
WORKFLOW = ROOT / '.github/workflows/e2e.yml'
GID = '4242'

IPTABLES_STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
tool = os.path.basename(sys.argv[0])
state = Path(os.environ['STATE']) / (tool + '.rules')
counters = Path(os.environ['STATE']) / 'counters.json'
args = [a for a in sys.argv[1:] if a != '-w']
rules = [line.split(' ', 1) for line in state.read_text().splitlines()] if state.exists() else []
def save():
    state.write_text(''.join(chain + ' ' + spec + '\\n' for chain, spec in rules))
if args[0] == '-I':
    chain, pos, spec = args[1], int(args[2]), ' '.join(args[3:])
    same = [i for i, (c, _) in enumerate(rules) if c == chain]
    at = same[pos - 1] if pos - 1 < len(same) else (same[-1] + 1 if same else len(rules))
    rules.insert(at, [chain, spec])
    save()
elif args[0] in ('-C', '-D'):
    chain, spec = args[1], ' '.join(args[2:])
    if [chain, spec] not in rules:
        sys.exit(1)
    if args[0] == '-D':
        rules.remove([chain, spec])
        save()
elif args[0] == '-S':
    if args[1] == 'DOCKER-USER' and os.environ.get('NO_DOCKER_USER') == '1':
        sys.exit(1)
    print('-P ' + args[1] + ' ACCEPT')
    for chain, spec in rules:
        if chain == args[1]:
            print('-A ' + chain + ' ' + spec)
elif args[:4] == ['-n', '-v', '-x', '-L']:
    print('Chain ' + args[4] + ' (policy ACCEPT 0 packets, 0 bytes)')
    print(' pkts bytes target prot opt in out source destination')
    hits = json.loads(counters.read_text()) if counters.exists() else {}
    for chain, spec in rules:
        if chain == args[4]:
            comment = spec.split('--comment ')[1].split()[0]
            print('%6d 0 TARGET all -- * * 0.0.0.0/0 0.0.0.0/0 /* %s */' % (hits.get(comment, 0), comment))
else:
    sys.exit(2)
'''

SUDO_STUB = '''#!/bin/sh
echo "$@" >> "$STATE/sudo.log"
while [ "${1:-}" = -n ] || [ "${1:-}" = -E ]; do shift; done
exec "$@"
'''

SETPRIV_STUB = '''#!/bin/sh
printf '%s\\n' "$@" > "$STATE/setpriv.args"
while [ "$1" != /usr/bin/env ]; do shift; done
shift
exec env "$@"
'''

GETENT_STUB = '''#!/bin/sh
[ "$1" = group ] && [ -f "$STATE/group" ] && { echo "$2:x:@GID@:"; exit 0; }
exit 2
'''.replace('@GID@', str(GID))

GROUPADD_STUB = '''#!/bin/sh
echo "$@" >> "$STATE/groupadd.log"
: > "$STATE/group"
'''


@pytest.fixture
def stubs(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    for name, body in (('iptables', IPTABLES_STUB), ('ip6tables', IPTABLES_STUB), ('sudo', SUDO_STUB),
                       ('setpriv', SETPRIV_STUB), ('getent', GETENT_STUB), ('groupadd', GROUPADD_STUB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'STATE': str(state)}
    env.pop('EGRESS_GATE_GROUP', None)

    def gate(*args, **overrides):
        return subprocess.run(['sh', str(GATE), *args], env={**env, **overrides},
                              capture_output=True, text=True, check=False)

    def rules(tool='iptables'):
        path = state / f'{tool}.rules'
        return path.read_text().splitlines() if path.exists() else []

    gate.rules = rules
    gate.state = state
    gate.tmp = tmp_path
    return gate


OWNER = f'-m owner --gid-owner {GID}'
TAGGED = '-m comment --comment airgap-egress-gate'


def test_install_orders_deny_before_allow_and_ends_with_reject(stubs):
    result = stubs('install', '--allow', '172.18.0.0/16', '--block-source', '172.18.0.0/16')
    assert result.returncode == 0, result.stderr
    assert stubs.rules() == [
        f'OUTPUT {OWNER} -p udp --dport 53 {TAGGED}:dns -j REJECT',
        f'OUTPUT {OWNER} -p tcp --dport 53 {TAGGED}:dns -j REJECT',
        f'OUTPUT {OWNER} -o lo {TAGGED}:loopback -j ACCEPT',
        f'OUTPUT {OWNER} -d 172.18.0.0/16 {TAGGED}:allow -j ACCEPT',
        f'OUTPUT {OWNER} {TAGGED}:reject -j REJECT',
        f'DOCKER-USER -s 172.18.0.0/16 ! -d 172.18.0.0/16 {TAGGED}:nodes -j REJECT',
    ]
    assert stubs.rules('ip6tables')[-1] == f'OUTPUT {OWNER} {TAGGED}:reject -j REJECT'
    assert (stubs.state / 'groupadd.log').read_text().strip() == '--system airgap-egress-gate'


def test_install_is_idempotent_and_remove_leaves_foreign_rules(stubs):
    foreign = '-p tcp --dport 22 -j ACCEPT'
    (stubs.state / 'iptables.rules').write_text(f'OUTPUT {foreign}\n')
    assert stubs('install', '--block-source', '10.9.0.0/16').returncode == 0
    first = stubs.rules()
    assert stubs('install', '--block-source', '10.9.0.0/16').returncode == 0
    assert stubs.rules() == first
    assert f'OUTPUT {foreign}' in first
    assert stubs('remove').returncode == 0
    assert stubs.rules() == [f'OUTPUT {foreign}']
    assert stubs.rules('ip6tables') == []


@pytest.mark.parametrize('flag', ['--allow', '--block-source'])
def test_install_rejects_malformed_cidr_before_any_rule(stubs, flag):
    result = stubs('install', flag, '1.1.1.1; reboot')
    assert result.returncode == 1
    assert 'invalid CIDR' in result.stderr
    assert stubs.rules() == []


def test_install_fails_closed_without_docker_user_chain(stubs):
    result = stubs('install', '--block-source', '172.18.0.0/16', NO_DOCKER_USER='1')
    assert result.returncode == 1
    assert 'DOCKER-USER' in result.stderr


def test_run_refuses_to_start_ungated(stubs):
    marker = stubs.tmp / 'ran'
    result = stubs('run', 'touch', str(marker))
    assert result.returncode == 1
    assert 'fail closed' in result.stderr
    assert not marker.exists()
    # The group exists but the rules were removed: still refuse.
    assert stubs('install').returncode == 0
    assert stubs('remove').returncode == 0
    result = stubs('run', 'touch', str(marker))
    assert result.returncode == 1 and not marker.exists()


def test_run_drops_into_the_gated_group_preserving_arguments_and_status(stubs):
    assert stubs('install').returncode == 0
    out = stubs.tmp / 'out.json'
    script = 'import json,os,sys; json.dump([sys.argv[1:], os.environ["HOME"]], open(sys.argv[1], "w"))'
    result = stubs('run', sys.executable, '-c', script, str(out), 'a b', "it's", HOME='/home/rehearsal')
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == [[str(out), 'a b', "it's"], '/home/rehearsal']
    args = (stubs.state / 'setpriv.args').read_text().split('\n')
    assert args[args.index('--regid') + 1] == GID
    assert args[args.index('--reuid') + 1] == str(os.getuid())
    assert '--no-new-privs' in args and '--init-groups' in args
    assert stubs('run', 'sh', '-c', 'exit 37').returncode == 37


@pytest.mark.parametrize('klass,comment', [('host', 'reject'), ('nodes', 'nodes')])
def test_require_hits_demands_a_rejected_packet(stubs, klass, comment):
    assert stubs('install', '--block-source', '172.18.0.0/16').returncode == 0
    result = stubs('require-hits', klass)
    assert result.returncode == 1
    assert 'never exercised' in result.stderr
    (stubs.state / 'counters.json').write_text(json.dumps({f'airgap-egress-gate:{comment}': 3}))
    result = stubs('require-hits', klass)
    assert result.returncode == 0, result.stderr
    assert 'rejected 3 packet' in result.stdout
    # Allow-rule traffic is not evidence of a rejection.
    (stubs.state / 'counters.json').write_text(json.dumps({'airgap-egress-gate:loopback': 99}))
    assert stubs('require-hits', klass).returncode == 1


# ----------------------------------------------------------------- probe


def probe(*args):
    return subprocess.run([sys.executable, '-I', str(PROBE), *args], capture_output=True, text=True, check=False)


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(4)
    yield f'127.0.0.1:{server.getsockname()[1]}'
    server.close()


@pytest.fixture
def closed_port():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    server.close()
    return f'127.0.0.1:{port}'


def test_blocked_mode_fails_when_a_connection_succeeds(listener, closed_port):
    result = probe('blocked', closed_port, listener)
    assert result.returncode == 1
    assert f'REACHABLE {listener}' in result.stdout
    assert 'VIOLATED' in result.stdout


def test_blocked_mode_accepts_refusal_and_name_resolution_failure(closed_port):
    result = probe('blocked', closed_port, 'host.invalid:443')
    assert result.returncode == 0, result.stdout
    assert 'ECONNREFUSED' in result.stdout
    assert 'name-resolution-failed' in result.stdout


def test_reachable_modes(listener, closed_port):
    assert probe('reachable', listener).returncode == 0
    assert probe('reachable', listener, closed_port).returncode == 1
    assert probe('reachable-any', closed_port, listener).returncode == 0
    assert probe('reachable-any', closed_port).returncode == 1


@pytest.mark.parametrize('argv', [[], ['blocked'], ['maybe', '1.1.1.1:443'], ['blocked', '1.1.1.1'],
                                  ['blocked', '1.1.1.1:0'], ['blocked', ':443'], ['blocked', 'h:99999']])
def test_probe_usage_errors(argv):
    assert probe(*argv).returncode == 2


# -------------------------------------------------------------- workflow


def kind_steps():
    jobs = yaml.safe_load(WORKFLOW.read_text())['jobs']
    return jobs['kind-live-rehearsal']['steps']


GATED_SHELL = 'sh rehearsal-infra/scripts/ci/egress_gate.sh run bash -e {0}'
# Steps that may stay outside the boundary after it closes: post-mortem
# collection and teardown run no operator code and need no operator network.
UNGATED_AFTER_BOUNDARY = {
    'Write Kind-scale Qdrant overrides (runner quota only; git prod values untouched)',
    'Write Kind-scale ingest patch (same quota; prod Job asks 4cpu/8Gi)',
    'Assert the boundary rejected real traffic (host and pods)',
    'Record the egress boundary state',
    'Failure diagnostics (pods, events, logs)',
    'Retain sanitized cluster diagnostics',
    'Delete Kind cluster (ephemeral, always)',
}


def test_every_operator_step_after_the_boundary_runs_through_the_gate():
    steps = kind_steps()
    names = [step.get('name', '') for step in steps]
    boundary = names.index('Close the network boundary (block public egress, prove it)')
    # Public-network setup must all happen before the boundary.
    for before in ('Install pinned local-path provisioner (explicit StorageClass)',
                   'Start authenticated TLS registry + Kind',
                   'Rehearsal platform stand-ins (public images, before the boundary)'):
        assert names.index(before) < boundary, before
    ungated = []
    for step in steps[boundary + 1:]:
        if 'run' not in step:
            continue
        if step.get('shell') != GATED_SHELL:
            ungated.append(step.get('name'))
    assert set(ungated) == UNGATED_AFTER_BOUNDARY, sorted(set(ungated) ^ UNGATED_AFTER_BOUNDARY)


def test_the_bundle_is_bootstrapped_only_behind_the_boundary():
    steps = kind_steps()
    names = [step.get('name', '') for step in steps]
    boundary = names.index('Close the network boundary (block public egress, prove it)')
    handoff = names.index('Black-box handoff (verify, unpack, bootstrap) with public egress blocked')
    assert handoff > boundary
    assert steps[handoff]['shell'] == GATED_SHELL
    assert 'bootstrap.sh' in steps[handoff]['run']
    for step in steps[:boundary]:
        assert 'bootstrap.sh' not in step.get('run', ''), step.get('name')


def boundary_policy():
    step = next(s for s in kind_steps() if s.get('name') == 'Close the network boundary (block public egress, prove it)')
    run = step['run']
    assert run.index('$GATE install') < run.index('kind: NetworkPolicy')
    body = run.split('apply -f - <<EOF\n', 1)[1].split('\nEOF', 1)[0]
    return yaml.safe_load(body.replace('${KIND_V4}', '172.18.0.0/16'))


def test_the_boundary_step_denies_pod_egress_inside_the_cluster():
    # The host rules were seen not to see a leaked pod SYN (runs 39936150839,
    # 39942787786, 39945681590); kindnet enforces this policy in the node.
    policy = boundary_policy()
    assert policy['kind'] == 'NetworkPolicy'
    assert policy['spec']['podSelector'] == {}
    assert policy['spec']['policyTypes'] == ['Egress']


def test_the_egress_policy_allows_only_dns_namespace_pods_and_the_api_server():
    rules = boundary_policy()['spec']['egress']
    assert rules[0]['to'][0]['podSelector'] == {'matchLabels': {'k8s-app': 'kube-dns'}}
    assert {(p['port'], p['protocol']) for p in rules[0]['ports']} == {(53, 'UDP'), (53, 'TCP')}
    assert rules[1] == {'to': [{'podSelector': {}}]}
    assert rules[2] == {'to': [{'ipBlock': {'cidr': '172.18.0.0/16'}}], 'ports': [{'port': 6443, 'protocol': 'TCP'}]}
    assert len(rules) == 3


def test_the_pod_probes_still_run_after_the_policy():
    names = [s.get('name', '') for s in kind_steps()]
    boundary = names.index('Close the network boundary (block public egress, prove it)')
    assert names.index('Prove node and pod egress is blocked (mock vLLM pod, real sockets)') > boundary
    assert names.index('Application pod egress is blocked (agent pod, real sockets)') > boundary


def test_a_node_probe_exercises_the_host_rule_before_require_hits_nodes():
    # The NetworkPolicy keeps pod connects off the host rule, so the nodes
    # counter is only reachable through a probe in a node's own namespace.
    steps = kind_steps()
    names = [s.get('name', '') for s in steps]
    probe = names.index('Prove node egress is blocked (Kind node network namespace, real sockets)')
    assert names.index('Assert the boundary rejected real traffic (host and pods)') > probe
    step = steps[probe]
    assert step['shell'] == GATED_SHELL
    assert 'docker exec "$node" curl' in step['run']
    assert all(ip in step['run'] for ip in ('1.1.1.1', '8.8.8.8', '9.9.9.9'))
    assert 'exit 1' in step['run']


def node_probe(tmp_path, curl_exit, nodes='gapbox-control-plane'):
    """Run the workflow's node-probe script against stub kind/docker."""
    step = next(s for s in kind_steps()
                if s.get('name') == 'Prove node egress is blocked (Kind node network namespace, real sockets)')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (tmp_path / 'kind').write_text('#!/bin/sh\nprintf "%s" "$NODES"\n')
    (tmp_path / 'kind').chmod(0o755)
    (bindir / 'docker').write_text('#!/bin/sh\nexit "$CURL_EXIT"\n')
    (bindir / 'docker').chmod(0o755)
    script = step['run'].replace('/usr/local/bin/kind', str(tmp_path / 'kind'))
    return subprocess.run(['bash', '-ec', script], capture_output=True, text=True, check=False,
                          env={**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'NODES': nodes,
                               'CURL_EXIT': str(curl_exit)})


@pytest.mark.parametrize('code', [6, 7, 28, 35, 56])
def test_node_probe_accepts_network_failures_as_blocked(tmp_path, code):
    result = node_probe(tmp_path, code)
    assert result.returncode == 0, result.stdout
    assert 'node egress probe blocked: ok' in result.stdout


@pytest.mark.parametrize('code', [0, 1, 2, 22, 125, 126, 127])
def test_node_probe_fails_when_connected_or_not_run(tmp_path, code):
    result = node_probe(tmp_path, code)
    assert result.returncode == 1
    assert 'VIOLATED' in result.stdout
    assert 'blocked: ok' not in result.stdout


def test_node_probe_fails_without_any_node(tmp_path):
    result = node_probe(tmp_path, 7, nodes='')
    assert result.returncode == 1
    assert 'no Kind node found' in result.stdout

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

DOCKER_STUB = '''#!/bin/sh
# docker network inspect NET -f TEMPLATE, answered from $STATE/docker.<field>
[ "$1 $2" = "network inspect" ] || exit 2
[ "$3" = "${NETWORK:-kind}" ] || exit 1
case "$5" in
    *bridge.name*) [ -f "$STATE/docker.bridge" ] && cat "$STATE/docker.bridge" || echo '<no value>' ;;
    *.Id*) cat "$STATE/docker.id" ;;
    *) exit 2 ;;
esac
'''


@pytest.fixture
def stubs(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    for name, body in (('iptables', IPTABLES_STUB), ('ip6tables', IPTABLES_STUB), ('sudo', SUDO_STUB),
                       ('setpriv', SETPRIV_STUB), ('getent', GETENT_STUB), ('groupadd', GROUPADD_STUB),
                       ('docker', DOCKER_STUB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {**os.environ, 'PATH': f'{bindir}:/usr/bin:/bin', 'STATE': str(state)}
    env.pop('EGRESS_GATE_GROUP', None)
    (state / 'docker.id').write_text('0123456789abcdef0123456789abcdef\n')

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


def test_block_network_rejects_by_bridge_interface_whatever_the_source(stubs):
    result = stubs('install', '--block-network', 'kind', '--block-source', '172.18.0.0/16',
                   '--block-source', '10.244.0.0/16')
    assert result.returncode == 0, result.stderr
    assert stubs.rules()[-3:] == [
        f'DOCKER-USER -s 172.18.0.0/16 ! -d 172.18.0.0/16 {TAGGED}:nodes -j REJECT',
        f'DOCKER-USER -s 10.244.0.0/16 ! -d 10.244.0.0/16 {TAGGED}:nodes -j REJECT',
        f'DOCKER-USER -i br-0123456789ab ! -o br-0123456789ab {TAGGED}:nodes -j REJECT',
    ]
    assert 'node bridges: br-0123456789ab' in result.stdout


def test_block_network_alone_installs_the_interface_rule(stubs):
    assert stubs('install', '--block-network', 'kind').returncode == 0
    assert stubs.rules()[-1] == f'DOCKER-USER -i br-0123456789ab ! -o br-0123456789ab {TAGGED}:nodes -j REJECT'


def test_block_network_prefers_the_explicit_bridge_name_option(stubs):
    (stubs.state / 'docker.bridge').write_text('kindbr0\n')
    assert stubs('install', '--block-network', 'kind').returncode == 0
    assert stubs.rules()[-1] == f'DOCKER-USER -i kindbr0 ! -o kindbr0 {TAGGED}:nodes -j REJECT'


@pytest.mark.parametrize('bridge', ['bad name', 'br;reboot', 'a-bridge-name-over-fifteen'])
def test_block_network_rejects_unusable_bridge_names_before_any_rule(stubs, bridge):
    (stubs.state / 'docker.bridge').write_text(bridge + '\n')
    result = stubs('install', '--block-network', 'kind')
    assert result.returncode == 1
    assert 'invalid bridge interface' in result.stderr
    assert stubs.rules() == []


def test_block_network_fails_for_an_unknown_network_before_any_rule(stubs):
    result = stubs('install', '--block-network', 'nope')
    assert result.returncode == 1
    assert 'cannot inspect Docker network' in result.stderr
    assert stubs.rules() == []


def test_interface_rule_is_reported_counted_and_removed(stubs):
    assert stubs('install', '--block-network', 'kind').returncode == 0
    status = stubs('status')
    assert status.returncode == 0
    assert 'node/pod rejected packets: 0' in status.stdout
    # The interface rule alone satisfies the nodes hit requirement.
    (stubs.state / 'counters.json').write_text(json.dumps({'airgap-egress-gate:nodes': 5}))
    assert 'rejected 5 packet' in stubs('require-hits', 'nodes').stdout
    assert stubs('remove').returncode == 0
    assert stubs.rules() == []


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


def test_the_boundary_step_blocks_the_kind_bridge_and_the_pod_cidr():
    step = next(s for s in kind_steps() if s.get('name') == 'Close the network boundary (block public egress, prove it)')
    assert '--block-network kind' in step['run']
    assert '--block-source 10.244.0.0/16' in step['run']

# Agents on the gap orchestration VM

Contract / scope: the coding agents of the air gap (herdr-core plus OpenCode), the
offline settings they need, and the VM that runs them (#1172).
Status: implemented in the dev kit image and `scripts/devkit/agents_vm.py`, with unit
evidence. The site steps (preflight, load, fence, proof, start) are **required, not yet
run** on the VM. The trials ran on the connected host in Docker and in rootless Podman
5.7.0 ([Podman trial](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6548398452));
SELinux labels, linger, delegation on a fresh account and the nftables fence were not
exercised there. The nftables layer, `install-fence`, the host-side proof and `start`'s
fence check run against real nftables in a rootless user+net namespace with
`tests/agents_vm_netns_contracts.py` (#1181; not a unit test, since CI runners have no
such namespaces, so run it after a fence change; a skip is not a pass). The systemd
layer needs real privilege and was not run.
Source of authority: #1172 design and decisions of 2026-10-31
([VM design](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6546764341),
[rootless Podman](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6546800917),
[pins and trust](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6547202945),
[site inputs](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6547220239),
[trial verdict](https://github.com/yonatan895/qdrant-pdf-rag/issues/1172#issuecomment-6548130709)),
and the dev kit of [ADR-0009 §8](adr/0009-gap-fork.md#8-dev-workspace-image-and-dev-kit-d3).
Decision owners: `images/devkit/agents/` (pins, licences, config, build verifier),
`images/devkit/bin/mfr-opencode` and `mfr-agent-workers`, `scripts/devkit/agents_vm.py`
(fence, proof, container, viewer), and this file.

## What the kit image holds

| Item | In the image | Pin and check |
|---|---|---|
| herdr-core 0.1.15 | `/usr/local/bin/herdr-core` and the link `hc` (the remote-machine probe looks for `herdr-core`) | `images/devkit/agents/herdr-core-pin.txt`: archive, binary, LICENSE and THIRD-PARTY-NOTICES digests |
| OpenCode 2.2.5 | `/usr/local/bin/opencode` | `images/devkit/agents/opencode-pin.txt`: archive and binary digests; the licence is the v2.2.5 tag's `LICENSE` (the archive has none), kept as `opencode-LICENSE` |
| Licence texts | `/usr/share/licenses/herdr-core/` and `/usr/share/licenses/opencode/` | the kit records `kit-tool:herdr-core` and `kit-tool:opencode` ([licensing](licensing.md#dev-kit)) |
| Hardened config | `/etc/opencode/opencode.json`, OpenCode's managed config on Linux: root-owned, highest precedence | `images/devkit/agents/opencode.json` |
| Wrappers | `mfr-opencode`, `mfr-agent-workers` | `images/devkit/bin/` |

Trust is HTTPS plus the pinned sha256 (maintainer decision: neither project signs its
releases). The connected-side image build downloads each pinned origin in its
`agent-tools` stage; `install_agent_tools.py` refuses the archive, the binary, a
licence or the notices when a digest differs, and checks `--version`, before anything
is installed. Nothing is piped to a shell, and nothing is downloaded at run time or in
the gap. A version change is a dedicated pin-bump PR that changes the kit key.

<a id="required-settings"></a>
## Required settings

The trial verdict accepted OpenCode v2.2.5 with five settings. Each one has a single
owner:

| # | Setting | Why | Enforced by |
|---|---|---|---|
| 1 | `OPENCODE_DISABLE_MODELS_FETCH=1` | the hardened config alone still fetches the model catalogue | the image `ENV`, and `mfr-opencode` sets it again whatever the caller's environment says |
| 2 | workers run `opencode run --standalone` | a plain run starts a shared service on `127.0.0.1:49374` that outlives it; the next worker fails on the busy port | `mfr-opencode run` adds `--standalone`; the TUI gets `opencode --standalone`; `serve`, `service`, `web`, `upgrade` and `uninstall` are refused |
| 3 | provider `timeout`, `headerTimeout`, `chunkTimeout` of 30 s | a silently dropped LiteLLM path hung over 300 s without them; with them a run exits after about attempts × 30 s | the managed config |
| 4 | an explicit `permission.shell` | v2 renamed the `bash` tool to `shell`; the trial ran it with no prompt | the managed config: unlisted commands `ask` (a headless `opencode run` rejects them and the model continues), a short allow list for git reads and commits, reads and the test and lint tools, and a deny list (push, remote and config changes, curl, wget, ssh, scp, oc, kubectl, helm, podman, docker, skopeo, pip) after it; `bash` gets the same rules. `mfr-opencode` refuses `--auto`, `--yolo` and `--dangerously-skip-permissions` |
| 5 | the container runs with `--init` | under a `sleep` PID 1 the exited service became a zombie (Podman: 3 orphans gave 3 zombies without it, 0 with it) | `agents_vm.py start` and the proof's container probe |

One more, from the Podman trial: **a serial warm-up turn before parallel workers on a
fresh HOME.** Concurrent cold starts race to create OpenCode's `opencode.db` schema
(`table account_state already exists`); the trial lost 4 of 33 workers without a
warm-up and 0 of 18 with one, under both runtimes. `mfr-agent-workers start` runs one
`mfr-opencode run` warm-up whenever the state database does not exist yet, and starts
no worker if it fails.

While a `--standalone` run lasts, its private `serve --stdio --port 0` child holds an
ephemeral `127.0.0.1` listener inside the container's network namespace (a random port,
so workers never collide). It is gone when the run ends, but anything in that namespace
can reach it meanwhile.

Also set: `autoupdate: false`, `share: disabled`, `webfetch` and `websearch` denied,
`enabled_providers` only the site LiteLLM, and `OPENCODE_DISABLE_AUTOUPDATE=1` and
`OPENCODE_DISABLE_LSP_DOWNLOAD=1`. The provider is the site LiteLLM at
`MFR_LLM_BASE_URL` with the model `MFR_LLM_MODEL`, and its key is read from the Podman
secret `/run/secrets/mfr-dev-litellm`: the **separate dev LiteLLM key** of the CI
namespace decision, never a production key. The image holds no key. Model limits are a
conservative 32,000-token context and 12,000-token output: 4,096 and 8,192 were too
small for OpenCode's own compaction summary, which then ends the run, and OpenCode
sends prompts of up to about 18,000 tokens, so 12,000 still fits the context
([#1196](https://github.com/yonatan895/qdrant-pdf-rag/issues/1196)).

Probed on v2.2.5 in a container, not on the VM: with the managed file in place, a config
named by `OPENCODE_CONFIG` that holds only `instructions` and `tools` is loaded as well
(the worker repeated a phrase found only in its instructions file, and the tools set to
false were gone). Not verified on v2.2.5 and left to the site run: that
`/etc/opencode/opencode.json` outranks a project `opencode.json` (OpenCode's
documentation says so); permission keys that a project config adds next to the managed
ones. On the VM, check the delivery once: start one worker whose brief asks it to repeat
a phrase. The egress fence, not the
permission policy, is the network control.

<a id="accounts"></a>
## Accounts and credentials

The VM is inside the gap network but outside the OpenShift namespaces. It reaches
GitLab and LiteLLM, and its egress is set on the VM itself.

- `agents`: a dedicated unprivileged user with no sudo, no docker group, no kubeconfig
  and no `oc` login. Rootless Podman needs, from the admin: an `/etc/subuid` and
  `/etc/subgid` range of at least 65536 ids for `agents` (`usermod --add-subuids
  START-END --add-subgids START-END agents`); cgroups v2 with `cpu memory pids`
  delegated to `user@<uid>.service` (a `Delegate=cpu memory pids` drop-in for
  `user@.service`), or `--memory` and `--cpus` are silently unenforced; and
  `loginctl enable-linger agents`, so the container outlives the SSH session.
  `agents_vm.py preflight` checks all three and fails with the fix; `start` and the
  container proof run it first. It runs rootless Podman, herdr-core and OpenCode. `agents_vm.py
  start` refuses an account with `KUBECONFIG` or `~/.kube`, and the container gets
  `KUBECONFIG=/dev/null`. The image's `oc` binary finds no credentials.
- The admin account (separate) installs the fence and the site env file.
- Credentials of `agents`, as Podman secrets: `mfr-dev-litellm` (the dev LiteLLM key)
  and, optionally, `mfr-gitlab-credentials`, a git-credential-store line for the GitLab
  project token (Developer role: `read_repository`, `write_repository`, `read_api`).
  **No OpenShift credential of any kind.**
- Containers on OpenShift happen only through GitLab CI (`wip` runners, read with
  `scripts/gap/ci_status.py`). The VM never creates pods.
- What the agents may read, and what stays out of their reach, is the agent class
  of [ADR-0011](adr/0011-gateway-caller-classes.md). It adds no credential or fence
  rule by itself.

<a id="site-inputs"></a>
## Site inputs

The GitLab, LiteLLM and DNS addresses, and who administers the VM's nftables rules, are
site inputs. They are never recorded in git or in an issue. Copy
[`scripts/devkit/agents-vm.env.example`](../scripts/devkit/agents-vm.env.example) to
`/etc/mfr-agents/site.env` (root:agents, 0640) and fill it in. `agents_vm.py` reads it
as literal `KEY=value` lines and refuses: a file inside a git work tree, shell syntax,
host names in an address list, ranges broader than /16 (IPv4) or /48 (IPv6), a missing
negative control, and a negative control inside an allowed address (its proof could
never fail). Who owns the firewall rules is noted in the private install record.

<a id="load-the-kit-image"></a>
## Load the kit image (as `agents`)

The image comes from the verified dev kit (`mfr-devkit-<key>/image/`, an OCI layout),
not from a registry: the fence allows no registry. After the kit is verified
([runbook](runbooks/devkit-windows.md) step 1, or `install.sh`'s checks), copy its
`image/` directory to the VM and run:

```sh
podman pull oci:/path/to/mfr-devkit-<key>/image
podman tag <image id printed above> localhost/qdrant-pdf-rag-devkit:<key>
podman image inspect --format '{{.Digest}}' localhost/qdrant-pdf-rag-devkit:<key>
```

The digest must equal `scripts/tools/devkit-pin.txt` at the release commit; put both
in the site env file (`MFR_VM_KIT_IMAGE`, `MFR_VM_KIT_DIGEST`). `start` and `proof`
check the digest again and never pull (`--pull=never`).

## Egress fence (as the admin)

```sh
sudo python3 scripts/devkit/agents_vm.py render-fence --out /root/mfr-fence   # review it
sudo python3 scripts/devkit/agents_vm.py install-fence
```

- **Primary layer, nftables:** table `inet mfr_agents`. Its output chain (policy
  accept) jumps to the chain `agents_egress` only on `meta skuid <agents uid>`; there
  established traffic, DNS, GitLab and LiteLLM are accepted, and everything else is
  counted and dropped. A packet with no socket owner (IPv6 neighbour discovery, kernel
  sockets of NFS, iSCSI or WireGuard) cannot match `meta skuid`, so it stays under the
  base chain's accept: other users and the kernel are unaffected (#1181). Rootless
  Podman's network helper (pasta or slirp4netns) runs as `agents`, so container traffic
  is caught too. That includes the
  VM's own services: pasta lets a container reach the host's loopback and addresses as
  `agents` (`host.containers.internal`), so the fence has no blanket loopback accept. A
  local DNS stub such as `127.0.0.53` passes only when it is listed in `MFR_VM_DNS`.
- **Second layer, systemd:** a drop-in for `user-<uid>.slice` with `IPAddressDeny=any`,
  `IPAddressAllow=localhost` and `IPAddressAllow=` the same addresses. It covers every
  process of the user's slice, Podman's `libpod-*` scopes included. It filters both
  directions, so the viewers' SSH clients go in `MFR_VM_SSH_CLIENTS`.
- **Which loopback each layer governs.** The systemd filter is cgroup eBPF: it applies
  to the slice's sockets in every network namespace, the container's included. Without
  `localhost` it would cut the container's own `127.0.0.1`, over which every
  `opencode run --standalone` reaches its child server (#1181), so workers and the
  warm-up would fail. The nftables table lives in the host's namespace only, where
  pasta's host-side sockets and the `agents` shell are; it has no loopback accept. So
  the host's loopback is refused to `agents` by nftables alone, and the proof's
  `host-loopback` control checks exactly that layer.
- `install-fence` checks the table with `nft -c` before loading it and changes nothing
  when nft refuses it or is missing. After loading it reads the live table back
  (`nft -j list table`) and refuses unless the output chain only jumps the agents uid
  into `agents_egress` and that chain ends in a drop. It writes
  `/etc/nftables/mfr-agents.nft`; include that file from the distribution's nftables
  service configuration so it survives a reboot (a site admin decision). Re-render
  after any address change. Running it again converges on the same table and files.
- `install-fence --remove` deletes both layers: the proof's negative control only. It
  refuses while any Podman container of `agents` runs (a `conmon` of that uid), since
  that container would run unfenced; stop `mfr-agents` first. Removing again
  converges: no table and no files is the state it wants.

## Egress proof (as `agents`)

```sh
python3 scripts/devkit/agents_vm.py proof --output /path/to/private/proof-fenced.json
podman stop mfr-agents                                                  # if it runs
sudo python3 scripts/devkit/agents_vm.py install-fence --remove        # admin
python3 scripts/devkit/agents_vm.py proof --fence-off --output /path/to/private/proof-open.json
sudo python3 scripts/devkit/agents_vm.py install-fence                 # admin, at once
podman rm mfr-agents && python3 scripts/devkit/agents_vm.py start     # if it ran
```

Each run probes from the host shell and from inside a kit container (`podman run
--init --pull=never`), with the same probe code. Fenced, it passes only when GitLab,
LiteLLM and DNS answer and every negative control (production Qdrant, the agent, the
OpenShift API, the internet, a service on the VM's loopback and one on its own address)
fails. Inside the container the loopback control targets `host.containers.internal`,
the path pasta gives to the host's loopback, and `container:loopback` must connect in
both modes: a listener on the container's own `127.0.0.1`, one byte each way, the
path OpenCode's workers need. Fence off, it passes only when those same negative
controls connect, which proves the probes can see a working path; a control that
fails either way proves nothing and is reported as such. Stdout carries labels and
states only. The JSON records hold the addresses, so they go in the private install
record, like the isolation proof's.

## Run the agents (as `agents`)

```sh
podman secret create mfr-dev-litellm /path/to/dev-litellm.key
podman secret create mfr-gitlab-credentials /path/to/git-credentials   # optional
python3 scripts/devkit/agents_vm.py start
```

`start` (like `proof`) refuses unless it runs as `agents` (a uid with no passwd entry
is not `agents`) and the fence is in force for that uid:
every `MFR_VM_BLOCKED` target must be refused from the host. `agents` cannot read the
nftables ruleset (netlink needs `CAP_NET_ADMIN`), so `start` checks the fence by its
effect; that holds while those targets answer without the fence, which the fence-off
proof shows. Then it runs the long-lived container `mfr-agents` from the loaded image: `--init`,
`--userns=keep-id:uid=1000770000,gid=0 --user 1000770000:0` (the default rootless user
namespace cannot map the kit's OpenShift-style uid; this maps `agents` onto it, so its
files are the container user's without `:U`), `--cap-drop=all`, `no-new-privileges`,
`MFR_VM_WORK` mounted at `/work` with the SELinux label `MFR_VM_VOLUME_LABEL` (`Z` by
default, `z` when shared, `none` without SELinux), `HOME=/work/home` (short, so the hc socket path stays under its 107-byte
limit), the LiteLLM URL and model from the site env file, and the secrets.

**Resources.** For one controller and three workers the trial measured about 4
OpenCode instances at 210 to 360 MiB each and a 6 MB hc core, with up to 1.5 CPU per
start-up burst. The container requests 2 CPU and 2 GiB (`--cpu-shares=2048`,
`--memory-reservation=2048m`) and is limited to 4 CPU and 4 GiB of agent overhead plus
the agents' own tools, `MFR_VM_TOOL_CPUS` (default 2) and `MFR_VM_TOOL_MEMORY_MIB`
(default 4096): 6 CPU and 8 GiB by default, swap off. To watch usage, read the
container's cgroup (`cpu.stat` deltas over an interval, `memory.current`) or run
`podman stats` with an interval: `podman stats --no-stream` reports a lifetime-average
CPU (37 to 46% on an idle container in the trial).

**Controller and workers (mode a).** Open the controller inside the container
(`podman exec -it mfr-agents hc`, then `mfr-opencode` in a terminal). Workers are
non-interactive OpenCode runs, each on its own git worktree and branch:

```sh
mfr-agent-workers start --repo /work/<repo> --tasks tasks.txt   # one "<id><TAB><task>" per line
mfr-agent-workers collect --tasks tasks.txt                      # hc wait --json, hc read, summary
```

`start` writes each brief beside the worktrees and runs `hc run --bg --name w-<id> --cwd
<worktree> --no-parent -- env OPENCODE_CONFIG=<brief config> mfr-opencode run "<fixed
start line>"` for each line ([task briefs](#task-briefs)). `hc wait` falls back to the process exit, so no OpenCode
plugin is needed. The workers keep running if the controller, its terminal or a viewer
goes away; `collect` again from any shell. The workers commit on their branches; a
person pushes and opens merge requests (the shell policy denies `git push`).

### Task briefs

A worker starts with its task text and the repository, nothing else, so the brief
carries everything it is not expected to find out by reading. Write the brief before
starting the worker, in five parts:

- **TITLE**: one line naming the change.
- **PROBLEM**: what is wrong today and the required behaviour, concretely.
- **INTERFACE**: the exact names, signatures, messages, codes and values that callers
  or tests depend on, or `no interface change`.
- **WHERE**: the file paths to change.
- **MUST NOT CHANGE**: what stays as it is.

The tasks file holds one task per line, `<id><TAB><task>`. A brief of several lines goes
in its own file, named in the tasks file as `<id><TAB>@<file>` (relative to the tasks
file). `mfr-agent-workers start` does not send the brief as the prompt: it writes it to
`.briefs/<name>.md` beside the worktrees and gives the worker a config that loads it as
instructions and turns off the tools a worker does not need. OpenCode drops old turns
long before the model's limit, so a brief sent as the first message is gone a few dozen
steps in, and the worker then makes up a task from what is left. A one-line task cannot
begin with `@`. `start` refuses a name that already has a worktree or a brief, before it
writes anything, so a repeated id never changes the brief of a worker that is running.

Rules from running a hosted flash-class model, not the site model, on real fixes in this
repository ([#1196](https://github.com/yonatan895/qdrant-pdf-rag/issues/1196)):

- **One behaviour and its tests per worker.** A brief with four parts ended with nothing
  committed; the same work as single parts completed. Split "write the failing test"
  from "make it pass" when the fix touches several code paths.
- **Take the brief from the issue, word for word.** A brief rewritten from memory left
  out half of its issue.
- **A worker's report is a claim.** Exit code 0 covered runs that committed nothing or
  answered another task, and one report of passing tests on a commit that fails. Before
  a branch leaves the VM, someone other than the worker runs the named tests at the
  commit and reads the diff, and the change gets its review.
- **A run that ends in a compaction error is retried as it is**; one that repeats the
  same reads for an hour is stopped and its task split.

In one measurement on 12 scoped fixes with a hosted
flash-class model, not the site model
([#1196](https://github.com/yonatan895/qdrant-pdf-rag/issues/1196)), a full brief
solved 23 of 24 runs (12 at each of two reasoning settings) and a title and path only
solved 6 of 12. For scoped fixes use the model's default reasoning setting; the higher
one did not solve more in that measurement.

<a id="windows-viewer"></a>
## Windows: view only

Windows stays a viewer. Give the viewer an SSH key in `~agents/.ssh/authorized_keys`
that can only run the observe command:

```text
restrict,pty,command="/usr/bin/python3 -I /opt/mfr-agents/scripts/devkit/agents_vm.py observe" ssh-ed25519 AAAA... viewer
```

Keep that copy of the scripts (`/opt/mfr-agents`, from the release commit) owned by the
admin and read-only to `agents`, so no agent can change what the viewer's key runs.

From Windows (OpenSSH client): `ssh -t agents@<vm> w-1` attaches to terminal `w-1`
with `hc attach --observe` (it never types or resizes; `Ctrl+\` detaches), and
`ssh agents@<vm>` with no name lists the terminals. Any other request is refused, so the
key opens no shell. Rootless Podman finds its containers through `HOME` and
`XDG_RUNTIME_DIR`, so `observe` keeps the account's own (`/run/user/<uid>` when unset).

The same applies to herdr's own remote-machine path on the VM: with the hc client's
`HOME` or runtime dir moved (to keep the 107-byte socket path short), `podman exec`
fails with "no container with name … found". Run it from the normal `agents` login, or
pass podman's environment explicitly:

```sh
hc machine add --name dev --via 'env HOME=/home/agents XDG_RUNTIME_DIR=/run/user/<uid> podman exec -i mfr-agents sh -c'
```

## Kit identity and pinning

`images/devkit/` is a kit key input, so this change produces a new kit key. Pinning is
one step ([deploy](deploy.md#ci-policy), `scripts/devkit_key.py`): the change merges with
`scripts/tools/devkit-pin.txt` untouched; on main, `.github/workflows/devkit.yml`
builds the image (which downloads and verifies the agent tools), pushes it by digest
and prints the key and digest in its job summary; one pin PR then records them in
`devkit-pin.txt` and in `.gitlab-ci.yml`'s default `CI_PYTHON_IMAGE`. An agent opens
that pin PR from the summary; the maintainer merges it. Kit packing refuses the stale
pin until then.

## Known gaps

- The site run: load, fence, both proofs, start, a worker round, and the Windows viewer.
- The systemd layer (cgroup eBPF, `IPAddressAllow=localhost`) needs real privilege; it
  was reasoned from `systemd.resource-control(5)`, not executed.
- Mode (b), the OpenCode plugin that reports OSC 7501 status, was not run on v2.
- An interactive `hc attach` from a host TTY was not run in the trial.
- The OpenCode binary bundles JavaScript dependencies whose notices it does not list
  ([licensing](licensing.md#dev-kit)).

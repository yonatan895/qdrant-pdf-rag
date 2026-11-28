# Release notes

Owner: this file announces changes to the upstream/gap extension surface
([ADR-0009 §9](adr/0009-gap-fork.md#9-extension-surface-open-question-2-decided)).
A gap import diffs it between two releases. Append one section per change,
newest last; never rewrite a published entry.

```text
## YYYY-MM-DD GH#NNN <title>

<What changed and what the operator or gap maintainer does.>

- removed `<category>:<item>`; replaced by `<new>`; migration: <kind> in `<path>`
```

- One `- removed` line for each item that leaves the surface, including a
  task or flag kept only as a refusing stub. Omit `; replaced by ...` only
  for a retired key that nothing replaces.
- `<category>` is a key of `tests/fixtures/extension-surface.json`, and
  `<item>` is exactly as it appears there.
- `<kind>` and `<path>` name the migration in the category's owner file:
  - `retired-key` in `scripts/airgap/model_config.py`: a `RETIRED_KEYS`
    entry whose message names the new key;
  - `refusal` in the taskfile or script: the stub that refuses the old task
    or flag with a fixed message naming the new one;
  - `refusal` in another owner: a fixed refusal line naming both names
    (see ADR-0009 §9).
- `tests/test_extension_surface.py` checks every line against the code. An
  added item needs no entry.

## Entries

## 2026-10-30 GH#1118 Deploy refuses an unverified checkout

Outside dry-run, `airgap:load`, `airgap:deploy`, `airgap:ingest` and
`airgap:validate` (and so `airgap:pipeline`) now run only from a verified
signed release: the checkout `bootstrap.sh` made from the signed bundle, with
`SNEAKERNET_TRUSTED_PUB` set to the release signing public key obtained out of
band. A plain clone, a missing anchor, a signature the anchor does not verify,
HEAD other than the MANIFEST sha, or a tracked or hidden change is refused
with a fixed message. Dry-run is unchanged. Operators export
`SNEAKERNET_TRUSTED_PUB` for every stage, not only `bootstrap.sh`. Connected
developers and the connected CI and Kind lanes that run these stages from a
plain checkout set `AIRGAP_UNVERIFIED_CHECKOUT=1` together with
`CI_REHEARSAL=1` in the process environment (never in `airgap.env`, never in
the air gap). See [deploy §4](deploy.md#4-signing-and-provenance).

## 2026-10-31 GH#1118 Retire-legacy refuses an unverified checkout

Outside dry-run, `airgap:retire-legacy` (`scripts/airgap/retire_legacy.sh`,
which uses the Qdrant writer key) now runs the same verified-release guard as
load, deploy, ingest and validate, before any cluster or Qdrant contact. Run
it from the checkout `bootstrap.sh` made from the signed bundle, with
`SNEAKERNET_TRUSTED_PUB` exported. The connected opt-out is unchanged:
`AIRGAP_UNVERIFIED_CHECKOUT=1` only together with `CI_REHEARSAL=1` in the
process environment, never in the air gap.

## 2026-10-31 GH#1118 GitLab CI runs on the dev kit image

The default `CI_PYTHON_IMAGE` in `.gitlab-ci.yml` is now the dev workspace
image, `$CI_APPROVED_REGISTRY/qdrant-pdf-rag-devkit` by the digest in
`scripts/tools/devkit-pin.txt` (ADR-0009 §8). Install the kit image there
(`scripts/devkit/install.sh --registry <CI_APPROVED_REGISTRY>/qdrant-pdf-rag-devkit`)
and leave the project variable `CI_PYTHON_IMAGE` unset; a Python job (lint,
test, hazards, sim, gate-l1) on any other image fails with a fixed message. Jobs use the image's installed dev
environment and its Task and Helm, so these GitLab CI/CD variables are retired:

- `CI_HELM_ARCHIVE`: the `hygiene` job refuses it with a fixed message naming
  `scripts/devkit/link-tools.sh`. Delete the project variable. To rerun a
  pipeline for a ref older than this change, pass it as a variable of that run
  only.
- `PIP_FIND_LINKS` and `PIP_INDEX_URL`: no longer read, and not refused, since
  pip itself honours them and a platform may set them for other projects. No
  GitLab job installs a package any more.

`CI_TASK_ARCHIVE` is unchanged and still required for `test`, `sim` and
`hazards`: the image holds the Task binary only, and the artifact tests and the
doctor read the archive. These variables are not items of the derived
extension surface, so this entry has no `- removed` line.

## 2026-10-31 GH#1174 Offline reference docs in the release bundle

The bundle gains the required signed member `reference-docs.tar.gz` and three
MANIFEST fields (`reference_docs`, `reference_docs_sha256`,
`reference_docs_sets`). `bootstrap.sh` verifies it and unpacks it to
`dist/reference-docs/` (start at `INDEX.md`): this repository's docs at the
release commit and 38 version-matched upstream doc sets. Nothing to do for the
gap maintainer; `dist/reference-docs/` is untracked, like the other members.

## 2026-10-31 GH#1118 Release origin and the gap trust anchor

The signed bundle MANIFEST now declares its release origin, `origin: upstream`
(`pack.sh`; a gap release will declare `origin: gap`). Outside dry-run the
live stages accept a bundle only when its signature verifies with the anchor
for that origin: `SNEAKERNET_TRUSTED_PUB` for upstream, as before, and the new
operator key `SNEAKERNET_GAP_TRUSTED_PUB` for gap releases. A MANIFEST without
an `origin:` line is refused, so roll back to a release packed before this
change with that release's own bootstrap and scripts in its own workspace.
Keep both anchors as plain files in `/work/anchors/` on the bastion, never
inside a workspace or bundle and never as links, and never point the two
variables at the same key: each is refused with a fixed message.
`bootstrap.sh` applies the same rule when an anchor is set, the release import
accepts only `origin: upstream` under the same anchor rules, and
`scripts/devkit/install.sh` refuses an upstream anchor that is the gap key
(compared as keys, so a PEM and a DER copy of one key are refused). Both anchors are listed, commented out, in
`airgap.env.example`. Nothing is removed.

## 2026-11-17 GH#1255 Open WebUI answer depth: grant each Rosetta entry

The docs Pipe is now a manifold. Open WebUI lists three models instead of one:
`mainframe_rag_docs.standard` "Rosetta" (the default and the old behaviour),
`mainframe_rag_docs.quick` "Rosetta – quick" (`low` effort) and
`mainframe_rag_docs.thorough` "Rosetta – thorough" (`xhigh` effort, which the
default `LLM_REASONING_EFFORTS` offers). The old model id `mainframe_rag_docs`
is gone, and its read grant does not carry over. The site admin does the
following ([deploy step 5](deploy.md#owui-answer-depth)):

1. Run `airgap:deploy` as usual. It installs the new Pipe; no new
   `airgap.env` key is needed.
2. Immediately after the deploy, in the Open WebUI admin UI, grant read
   access to all three entries, quick and thorough included, to the same
   users or groups that had the old Rosetta model (maintainer decision
   2026-11-16: no entry is held back until a later probe). Until then,
   ordinary users see no model.
3. As an ordinary user, open a chat started before the upgrade. It switches
   to "Rosetta"; send one question and check the citations.

API callers that used `mainframe_rag_docs` must switch to an entry id. Some
thorough answers may end at the 270 s request deadline; thorough is visible
from the upgrade on, and the site eval measures how often that happens
afterwards. Nothing is removed from the
extension surface.

## 2026-11-17 GH#1289 Answer depth follows the offered efforts: grant all three entries

The Rosetta entries no longer carry a fixed effort. The docs Pipe sends a
symbolic depth and the agent resolves it from its own `LLM_REASONING_EFFORTS`:
quick is the lowest offered effort, thorough the highest, and standard the
configured simple/complex default, as before. A site offering
`low,medium,high` answers thorough at `high`; once the platform offers `xhigh`,
thorough uses it with no repackage. The site admin does the following
([answer depth](deploy.md#owui-answer-depth)):

1. Run `airgap:deploy` as usual; the Pipe and the agent upgrade together (an
   agent older than this change refuses the depth). No new `airgap.env` key.
2. Grant read access to all three entries, thorough included, whatever
   `LLM_REASONING_EFFORTS` offers. The interim advice to hold back thorough
   on a site without `xhigh` no longer applies.

API callers of `/v1/chat` may send `reasoning_depth` (`quick`, `standard`,
`thorough`) or the unchanged `reasoning_effort`, not both. Nothing is removed
from the extension surface.

## 2026-11-17 GH#1287 Pasted images in temporary chats, off by default

Open WebUI users can paste screenshots into a temporary chat once the site
turns images on; the reasoning model reads each image to search the manuals
and sees it when answering. Nothing changes until a site opts in: off, Rosetta
is declared not image-capable and requests for messages without attachments
are byte-identical to before. The docs Pipe still drops Open WebUI's
`<attached_files>` text, which appears only when a file is attached, and
uploads are off.
Two new optional `airgap.env` keys ([pasted images](deploy.md#owui-chat-images)):

1. Run `probe_gateway.py --probe-image` in the agent pod. Turn images on only
   after it prints `PASS`.
2. If its `image_tokens` is above 6144, set `LLM_IMAGE_TOKEN_RESERVE` to that
   figure, rounded up (256..65536).
3. Set `CHAT_IMAGES_ENABLED=true` and run `airgap:deploy`. The transcription
   call uses the lowest effort `LLM_REASONING_EFFORTS` offers, so no effort
   setting changes.

Nothing is removed from the extension surface.

## 2026-11-24 GH#1368 Answer selection and neighbour expansion on by default; five new keys

Candidate selection (#1309) and neighbour expansion (#1314) are on by default
(maintainer decision 2026-11-22, confirmed 2026-11-24). Every answer, chat and
console turn whose question names no identifier and is not a trap makes one
extra lowest-effort reasoning call over a 40-hit retrieval (none when there
are no more candidates than packed hits), and the packed hits' neighbours
within two pages join the prompt. Five new optional `airgap.env` keys
([default changes](deploy.md#default-changes-1368)):

1. `ANSWER_SELECT_ENABLED` and `ANSWER_EXPAND_ENABLED` (unset: on). Set one
   `false` and run `airgap:deploy` to switch that feature off (one site
   ablation arm each).
2. `CHAT_IMAGE_MAX_BYTES` (5242880), `CHAT_IMAGE_MAX_COUNT` (3) and
   `CHAT_IMAGE_MAX_PIXELS` (4194304): the agent's pasted-image limits, values
   unchanged. The browser and Pipe caps stay, so in Open WebUI only a lower
   value takes effect.

Confirm the reasoning key's rate limit and quota before the upgrade.
`CHAT_IMAGES_ENABLED`, `FEEDBACK_ENABLED` and rerank stay off. Nothing is
removed from the extension surface.

## 2026-11-25 GH#1077 Splunk team-tool gateway in the chart, off by default

The chart can deploy the Splunk gateway (ADR-0015) and register it in Open
WebUI as a tool server for a Team model. Nothing changes until a site opts in:
off, the render is unchanged. Enabling it adds one small pod
(`rag-splunk-gateway`) and gives Open WebUI one LiteLLM connection, limited to
the reasoning model, and the gateway as a tool server. New optional
`airgap.env` keys: `SPLUNK_GATEWAY_ENABLED`, `SPLUNK_GATEWAY_AUTH_MODE`,
`SPLUNK_GATEWAY_SECRET`, `SPLUNK_GATEWAY_CONFIG_FILE`, `SPLUNK_GATEWAY_CA_FILE`,
`SPLUNK_EGRESS_CIDRS`, `SPLUNK_EGRESS_PORT`, `SPLUNK_GATEWAY_IDP_EGRESS_CIDRS`,
`SPLUNK_GATEWAY_IDP_EGRESS_PORT`, the four `SPLUNK_GATEWAY_*` sizing keys,
`TEAM_TOOLS_GROUP_ID`, `TEAM_MODEL_EGRESS_CIDRS` and `TEAM_MODEL_EGRESS_PORT`.
To turn it on, follow the [site procedure](deploy.md#splunk-gateway-deploy):

1. The Splunk owner creates the token user, its role (the team's indexes,
   search only, `mcp_tool_execute`) and an encrypted MCP token.
2. Create the gateway Secret, add Open WebUI's LiteLLM key
   `openwebui-llm-api-key` to the `GATEWAY_API_KEY_SECRET` Secret (always, also with a
   shared `GATEWAY_API_KEY_SECRET_KEY`: validate and deploy refuse without it), and write
   the gateway configuration and the Splunk CA file at the site. Set the
   Splunk and LiteLLM egress CIDRs (and the IdP's in oidc mode).
3. Create the team group in Open WebUI and set `TEAM_TOOLS_GROUP_ID` to its id.
4. Set `SPLUNK_GATEWAY_ENABLED=true` (and `SPLUNK_GATEWAY_AUTH_MODE=bearer` for the
   pilot; the default stays `oidc`), run `airgap:deploy` and the site probe.
5. An admin turns the connection model into the Team model in the admin UI.

Nothing is removed from the extension surface.

## 2026-11-28 GH#1294 Read-only z/OS dataset reads: the FTP bridge in the chart, off by default

The chart can deploy the read-only z/OS FTP bridge (`rag-mcp-ftp`, ADR-0012) on
the agent image and point the agent's Docs-path reads at it. Until a site opts
in, nothing changes and the render stays as it was. New optional
`airgap.env` keys: `ZOS_READ_ENABLED`, `ZOS_READ_DRY_RUN`, `ZOS_READ_SECRET`,
`ZOS_READ_APPROVED_ROLES`, `ZOS_READ_DATASET_ALLOWLIST`, `ZOS_READ_ENABLED_TOOLS`,
`ZOS_READ_JES_JOBNAME_PREFIXES`, `ZOS_READ_JES_OWNER_PREFIXES`, `ZOS_FTP_HOST`,
`ZOS_FTP_HOST_IP`, `ZOS_FTP_PORT`, `ZOS_FTP_PASSIVE_PORTS`,
`ZOS_FTP_TRANSPORT_MODE`, `ZOS_FTP_PLAIN_ACCEPTED`, `ZOS_FTP_CA_FILE`,
`ZOS_FTP_ENCODING` and the four `ZOS_BRIDGE_*` sizing keys. The new chart
values are under `zosRead`. Turning it on needs the site prerequisites and
probes in the [site procedure](deploy.md#zos-read-deploy). Validate and deploy
refuse unless every Open WebUI sign-in and admin role is approved. They also
refuse with the console Route on, the agent NetworkPolicy off, or extra agent
namespaces.

Nothing is removed from the extension surface.

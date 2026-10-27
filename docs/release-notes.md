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

- One `- removed` line for each item that leaves the surface. Omit
  `; replaced by ...` when nothing replaces it.
- `<category>` is a key of `tests/fixtures/extension-surface.json`, and
  `<item>` is exactly as it appears there.
- `<kind>` is one of:
  - `retired-key`: a `RETIRED_KEYS` entry in
    `scripts/airgap/model_config.py`, whose message names the new key;
  - `alias`: the old name keeps working, and `<path>` names both;
  - `refusal`: `<path>` refuses the old name with a fixed message naming the
    new one.
- `tests/test_extension_surface.py` checks every line against the code. An
  added item needs no entry.

## Entries

None yet.

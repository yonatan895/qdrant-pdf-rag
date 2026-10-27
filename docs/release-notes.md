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

None yet.

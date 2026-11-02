"""The shells the two-shell job-step proofs collect (GH#1118 C2).

GitLab's runner runs job scripts with bash, and the kit image's `sh` is bash in
POSIX mode; GitHub's and Debian's `sh` is dash. bash is always collected. dash
is collected where it is installed, and always on GitHub (`GITHUB_ACTIONS`
set): a GitHub runner without dash then fails the dash cases ("dash is
required") instead of dropping the proof. The kit image, where GitLab's unit
shards run, has neither, so it collects only the bash cases and reports no
failure and no skip (check_junit_clean.py rejects skips).
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping


def posix_shells(environ: Mapping[str, str] | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> tuple[str, ...]:
    environ = os.environ if environ is None else environ
    if which('dash') or environ.get('GITHUB_ACTIONS') == 'true':
        return ('bash', 'dash')
    return ('bash',)

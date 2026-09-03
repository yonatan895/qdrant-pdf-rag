"""Budget resolve CLI (serving-budget track, PR-B).

Single consumer: scripts/run_local_vllm.sh evals the stdout assignments, so
stdout carries ONLY KEY='value' lines — diagnostics go to stderr. Every value
is charset-validated before emission; a future table edit producing an
unsafe value fails closed instead of injecting into a shell eval.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence

from mainframe_rag.serve.budget import (
    BudgetDeficitError,
    HostSpec,
    ProfileBundle,
    ServerPlan,
    resolve,
)
from mainframe_rag.serve.profiles import PROFILES

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mainframe_rag.serve",
        description="Resolve a serving profile to per-server vLLM launch assignments.",
    )
    parser.add_argument(
        "command",
        choices=("resolve",),
        help="resolve: print KEY='value' assignments for one profile server.",
    )
    parser.add_argument("--profile", default="LOCAL_RT_8GB", help="Declared profile name.")
    parser.add_argument(
        "--role",
        default="reasoning",
        help="Profile server role to resolve (e.g. reasoning, embed, rerank).",
    )
    parser.add_argument(
        "--total-vram-mib",
        type=float,
        default=None,
        help="Size a hypothetical host instead of the profile host (handoff math).",
    )
    parser.add_argument(
        "--reserve-mib",
        type=float,
        default=None,
        help="Driver/CUDA-context reserve for a hypothetical host.",
    )
    return parser


def _emit(key: str, value: str) -> str:
    if not _SAFE_VALUE.fullmatch(value):
        raise BudgetDeficitError(
            f"Refusing to emit unsafe shell value for {key}: {value!r}.",
            remedies=["Fix the declared table value; only [A-Za-z0-9._-] may be emitted."],
        )
    return f"{key}='{value}'"


def _assignments(plan_server: ServerPlan) -> list[str]:
    return [
        _emit("BUDGET_GPU_MEM", f"{plan_server.gpu_memory_utilization:.2f}"),
        _emit("BUDGET_MAX_LEN", str(plan_server.max_model_len)),
        _emit("BUDGET_RUNNER", plan_server.runner),
        _emit("BUDGET_CONVERT", plan_server.convert),
        _emit(
            "BUDGET_BATCHED_TOKENS",
            str(plan_server.max_num_batched_tokens or ""),
        ),
        _emit("BUDGET_EAGER", "1" if plan_server.enforce_eager else "0"),
        _emit("BUDGET_SEQS", str(plan_server.max_num_seqs)),
    ]


def cmd_resolve(args: argparse.Namespace) -> int:
    profile = PROFILES.get(args.profile)
    if profile is None:
        print(
            f"ERROR: unknown profile {args.profile!r}; known: {', '.join(sorted(PROFILES))}.",
            file=sys.stderr,
        )
        return 2
    if args.total_vram_mib is not None or args.reserve_mib is not None:
        host = HostSpec(
            total_vram_mib=args.total_vram_mib or profile.host.total_vram_mib,
            reserve_mib=(
                args.reserve_mib
                if args.reserve_mib is not None
                else profile.host.reserve_mib
            ),
        )
        profile = ProfileBundle(name=profile.name, host=host, servers=profile.servers)
    matches = [s for s in profile.servers if s.role == args.role]
    if not matches:
        roles = ", ".join(s.role for s in profile.servers)
        print(
            f"ERROR: profile {profile.name!r} has no {args.role!r} server; roles: {roles}.",
            file=sys.stderr,
        )
        return 2
    if len(matches) > 1:
        roles = ", ".join(s.model_id for s in matches)
        print(
            f"ERROR: profile {profile.name!r} has several {args.role!r} servers "
            f"({roles}); --role must select one.",
            file=sys.stderr,
        )
        return 2
    try:
        plan = resolve(
            ProfileBundle(name=profile.name, host=profile.host, servers=list(matches))
        )
    except BudgetDeficitError as exc:
        print(f"BUDGET DEFICIT: {exc}", file=sys.stderr)
        for remedy in exc.remedies:
            print(f"  - {remedy}", file=sys.stderr)
        return 1
    try:
        lines = _assignments(plan.servers[0])
    except BudgetDeficitError as exc:
        print(f"BUDGET DEFICIT: {exc}", file=sys.stderr)
        for remedy in exc.remedies:
            print(f"  - {remedy}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "resolve":
        return cmd_resolve(args)
    return 2  # unreachable: argparse enforces choices


if __name__ == "__main__":
    raise SystemExit(main())

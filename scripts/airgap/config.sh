#!/bin/sh
# AIR-GAP CONFIG (issue #1284): inspect effective operator configuration or
# validate airgap.env for unknown keys, type/bound violations, mutual
# exclusions, and deprecated/retired keys.
#
#   sh scripts/tools/run-task.sh airgap:config -- show
#   sh scripts/tools/run-task.sh airgap:config -- check
#   sh scripts/airgap/config.sh show|check
#
# Safe to paste into a receipt: never prints secret values or Secret contents.

set -eu

usage="usage: $0 show|check [--help|-h]"
SUBCMD=""
for arg in "$@"; do
    case "$arg" in
        show|check)
            if [ -n "$SUBCMD" ]; then
                echo "FAIL: expected one subcommand (show or check); $usage" >&2
                exit 2
            fi
            SUBCMD=$arg
            ;;
        --help|-h)
            echo "$usage"
            exit 0
            ;;
        *)
            echo "FAIL: unknown argument (see --help); $usage" >&2
            exit 2
            ;;
    esac
done
if [ -z "$SUBCMD" ]; then
    echo "FAIL: missing subcommand (show or check); $usage" >&2
    exit 2
fi

# `check` passes the raw env file to config_tool (--env-file) in addition to
# the exported operator env, so file-only keys are reported too. common.sh
# still sources first: a hard sourcing failure (e.g. a plaintext key) aborts
# the shell before the check runs, and the plaintext guard keeps that contract.
. "$(dirname -- "$0")/common.sh"

command -v python3 >/dev/null 2>&1 || die "python3 is required for airgap:config"
export_operator_env

if [ "$SUBCMD" = "show" ]; then
    python3 -I scripts/airgap/config_tool.py show \
        --env-file "${_AIRGAP_ENV_FILE:-}" \
        --cli-keys "${_AIRGAP_ENV_CLI_KEYS:-}"
else
    python3 -I scripts/airgap/config_tool.py check \
        --env-file "${_AIRGAP_ENV_FILE:-}"
fi

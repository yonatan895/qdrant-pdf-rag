#!/bin/sh
# Shared pinned-artifact checks. Sourced by explicit installers/packaging only.
# Never execute an archive member until all three hashes have matched the pin.
task_fail() { echo "task-artifact: $*" >&2; exit 2; }

task_read_pin() {
    TASK_PIN=$1
    [ -f "$TASK_PIN" ] || task_fail "pin record missing: $TASK_PIN"
    TASK_VERSION=$(sed -n 's/^version: *//p' "$TASK_PIN")
    TASK_ASSET=$(sed -n 's/^asset: *//p' "$TASK_PIN")
    TASK_SHA256=$(sed -n 's/^sha256: *//p' "$TASK_PIN")
    TASK_BINARY_SHA256=$(sed -n 's/^binary-sha256: *//p' "$TASK_PIN")
    TASK_LICENSE_SHA256=$(sed -n 's/^license-sha256: *//p' "$TASK_PIN")
    TASK_ORIGIN=$(sed -n 's/^origin: *//p' "$TASK_PIN")
    [ "$TASK_ASSET" = task_linux_amd64.tar.gz ] || task_fail "unsupported Task asset"
    printf '%s\n' "$TASK_VERSION" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || task_fail "invalid Task version"
    for _task_hash in "$TASK_SHA256" "$TASK_BINARY_SHA256" "$TASK_LICENSE_SHA256"; do
        [ "${#_task_hash}" -eq 64 ] || task_fail "invalid checksum in pin record"
        case "$_task_hash" in *[!0-9a-f]*) task_fail "invalid checksum in pin record" ;; esac
    done
    [ "$TASK_ORIGIN" = "https://github.com/go-task/task/releases/download/$TASK_VERSION/$TASK_ASSET" ] || task_fail "invalid Task origin"
}

task_check_platform() {
    [ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || task_fail "unsupported platform; pin covers linux-amd64 only"
}

task_verify_archive() {
    [ -f "$1" ] || task_fail "Task archive missing: $1"
    printf '%s  %s\n' "$TASK_SHA256" "$1" | sha256sum -c - >/dev/null || task_fail "Task archive checksum mismatch"
}

task_extract_verified() {
    task_verify_archive "$1"
    # Extract only named, pinned members; no archive-selected output paths.
    tar xzf "$1" -C "$2" task LICENSE
    printf '%s  %s\n' "$TASK_BINARY_SHA256" "$2/task" | sha256sum -c - >/dev/null || task_fail "Task binary checksum mismatch"
    printf '%s  %s\n' "$TASK_LICENSE_SHA256" "$2/LICENSE" | sha256sum -c - >/dev/null || task_fail "Task license checksum mismatch"
}

task_check_manifest() {
    for _task_field in version platform asset sha256 binary_sha256 license_sha256; do
        case "$_task_field" in
            version) _task_expected=$TASK_VERSION;;
            platform) _task_expected=linux-amd64;;
            asset) _task_expected=$TASK_ASSET;;
            sha256) _task_expected=$TASK_SHA256;;
            binary_sha256) _task_expected=$TASK_BINARY_SHA256;;
            license_sha256) _task_expected=$TASK_LICENSE_SHA256;;
        esac
        _task_actual=$(sed -n "s/^task_$_task_field: *//p" "$1")
        [ "$_task_actual" = "$_task_expected" ] || task_fail "MANIFEST Task $_task_field does not match the approved pin"
    done
}

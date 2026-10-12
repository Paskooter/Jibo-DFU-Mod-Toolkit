#!/usr/bin/env bash
# Windows EXE only: the frozen GUI and session marker belong to the normal user.
set -euo pipefail
[[ $# == 2 && $EUID != 0 ]] || { printf 'The Windows GUI requires a non-root Linux default user.\n' >&2; exit 2; }
binary=$1 session=$2
trap 'rm -f -- "$session"' EXIT
trap 'exit 130' HUP INT TERM
printf '%s\n' "$$" > "$session"
"$binary"

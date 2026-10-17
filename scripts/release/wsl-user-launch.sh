#!/usr/bin/env bash
# Windows EXE only: the frozen GUI and session marker belong to the normal user.
set -euo pipefail
# Read-only preflight. Weston appends sessions; an old healthy session must not
# hide a failed shared-memory initialization in the current one. Never restart
# the shared compositor or shut down any distribution from this helper.
if [[ ${1:-} == display-mode && $# -le 2 ]]; then
    log=${2:-/mnt/wslg/weston.log}
    mode=starting
    if [[ ! -r $log ]]; then printf 'missing\n'; exit 0; fi
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        if [[ $line == *'Command line:'* ]]; then mode=starting; fi
        if [[ $line == *'RDP backend: use_gfxredir = 0' ]]; then mode=copy; fi
        if [[ $line == *'RDP backend: use_gfxredir = 1' ]]; then mode=shared; fi
    done < "$log"
    printf '%s\n' "$mode"
    exit 0
fi
[[ ( $# == 2 || $# == 3 ) && $EUID != 0 ]] || { printf 'The Windows GUI requires a non-root Linux default user.\n' >&2; exit 2; }
binary=$1 session=$2 ready=${3:-}
trap 'rm -f -- "$session"; if [[ -n $ready ]]; then rm -f -- "$ready" "$ready.visible" "$ready.close"; fi' EXIT
trap 'exit 130' HUP INT TERM
printf '%s\n' "$$" > "$session"
if [[ -n $ready ]]; then
    rm -f -- "$ready" "$ready.visible" "$ready.close"
    export JIBO_GUI_READY_FILE="$ready"
fi
"$binary"

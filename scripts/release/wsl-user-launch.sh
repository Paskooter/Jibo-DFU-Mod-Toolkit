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
# Same read-only parse plus what the Windows side needs to judge a display fix:
# how many weston sessions the log holds (a restart appends "Command line:"),
# and whether the current session's shared-memory probe failed with EIO
# (microsoft/openvmm#4274, microsoft/wslg#1456). One line, key=value.
if [[ ${1:-} == display-detail && $# -le 2 ]]; then
    log=${2:-/mnt/wslg/weston.log}
    mode=starting sessions=0 eio=0
    if [[ ! -r $log ]]; then printf 'mode=missing sessions=0 eio=0\n'; exit 0; fi
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        if [[ $line == *'Command line:'* ]]; then mode=starting; eio=0; sessions=$((sessions + 1)); fi
        if [[ $line == *'rdp_allocate_shared_memory:'*'Input/output error'* ]]; then eio=1; fi
        if [[ $line == *'RDP backend: use_gfxredir = 0' ]]; then mode=copy; fi
        if [[ $line == *'RDP backend: use_gfxredir = 1' ]]; then mode=shared; fi
    done < "$log"
    printf 'mode=%s sessions=%d eio=%d\n' "$mode" "$sessions" "$eio"
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

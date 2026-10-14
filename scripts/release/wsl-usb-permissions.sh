#!/usr/bin/env bash
# Windows release only: grant the invoking user access to Jibo USB, never the GUI.
set -euo pipefail

grant_jibo_usb() {
  local sysfs=$1 devfs=$2 uid=$3 gid=$4 entry vendor product bus device node
  for entry in "$sysfs"/*; do
    [[ -r $entry/idVendor && -r $entry/idProduct && -r $entry/busnum && -r $entry/devnum ]] || continue
    read -r vendor < "$entry/idVendor" || continue
    read -r product < "$entry/idProduct" || continue
    [[ $vendor == 0955 && ( $product == 7740 || $product == 701a ) ]] || continue
    read -r bus < "$entry/busnum" || continue
    read -r device < "$entry/devnum" || continue
    [[ $bus =~ ^[0-9]{1,3}$ && $device =~ ^[0-9]{1,3}$ ]] || continue
    printf -v node '%s/%03d/%03d' "$devfs" "$((10#$bus))" "$((10#$device))"
    test -c "$node" && test ! -L "$node" || continue
    # Removal during APX -> DFU is expected. The next pass discovers the new node.
    if ! chown "$uid:$gid" --no-dereference -- "$node" 2>/dev/null || ! chmod 0600 -- "$node" 2>/dev/null; then
      # A vanished node is normal hotplug; a still-live denied node is not success.
      test -c "$node" || continue
      printf 'Could not grant Jibo USB access to %s.\n' "$node" >&2
      return 1
    fi
  done
}

usb_session_alive() {
  local session=$1 uid=$2 pid owner
  [[ -r $session ]] || return 1
  read -r pid < "$session" || return 1
  [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
  owner=$(ps -o uid= -p "$pid") || return 1
  [[ ${owner//[[:space:]]/} == "$uid" ]]
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  [[ $# == 3 ]] || { printf 'Usage: wsl-usb-permissions.sh UID GID once|SESSION_FILE\n' >&2; exit 2; }
  uid=$1 gid=$2 session=$3
  [[ $EUID == 0 && $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ && $uid != 0 ]] || {
    printf 'USB setup needs root and a non-root invoking UID/GID.\n' >&2; exit 2;
  }
  if [[ $session == once ]]; then
    grant_jibo_usb /sys/bus/usb/devices /dev/bus/usb "$uid" "$gid"
  else
    [[ $session == /* ]] || { printf 'Invalid USB session path.\n' >&2; exit 2; }
    # The normal user creates/removes this marker; root never writes user files.
    # Bound initial startup and stop even if the GUI wrapper is killed without cleanup.
    deadline=$((SECONDS + 15))
    while [[ -f $session ]]; do
      if [[ -s $session ]]; then
        usb_session_alive "$session" "$uid" || break
      elif (( SECONDS >= deadline )); then
        break
      fi
      grant_jibo_usb /sys/bus/usb/devices /dev/bus/usb "$uid" "$gid"
      sleep 0.2
    done
  fi
fi

#!/usr/bin/env bash
# Provision only the launcher's freshly imported, private Ubuntu image.
set -euo pipefail
[[ $EUID == 0 ]] || { printf 'Private Linux setup requires distro root.\n' >&2; exit 2; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 22.04 && $(uname -m) == x86_64 ]] || {
  printf 'Private Linux setup requires the pinned Ubuntu 22.04 x64 image.\n' >&2; exit 2;
}
# The pinned official WSL image already supplies every system library needed by
# the frozen payload. No apt update/install, Python, Tk or compiler download.
if ! id -u jibo >/dev/null 2>&1; then
  if getent passwd 1000 >/dev/null || getent group 1000 >/dev/null; then
    printf 'Private image unexpectedly has an account/group at ID 1000.\n' >&2; exit 2;
  fi
  groupadd --gid 1000 jibo
  useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash --password '!' jibo
fi
[[ $(id -u jibo) == 1000 && $(id -g jibo) == 1000 ]] || exit 2
# Only its own group, locked password, no sudo grant or account wizard.
usermod --groups '' --lock jibo
cat > /etc/wsl.conf <<'CONF'
[boot]
systemd=false
[user]
default=jibo
CONF
# --import sets NO_OOBE in WSL. Also remove the image's interactive wsl-setup
# command, so a future WSL change cannot introduce username/password prompts.
cat > /etc/wsl-distribution.conf <<'CONF'
[oobe]
defaultUid=1000
defaultName=Jibo-DFU-Toolkit
[shortcut]
enabled=false
[windowsterminal]
enabled=false
CONF
[[ $(id -nG jibo) == jibo ]] || exit 2
printf 'Toolkit Linux user is ready; password locked, no sudo access.\n'

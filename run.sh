#!/usr/bin/env bash
# Build the local Linux package when needed, then open the Jibo terminal UI.
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
package=${JIBO_PYZ:-$repo_dir/dist/jibo-dfu-linux-x86_64.pyz}
loader=${JIBO_LOADER:-$repo_dir/assets/loader.bin}
shofel_src=${JIBO_SHOFEL_SRC:-$repo_dir/.build/ShofEL2-for-T124}
entry_dir=$repo_dir/.build/file-level-entry
shofel_commit=31ac3a260c8a1501869aff6690b3b9ad4904ef58
usb_monitor_pid=
package_stage_dir=

die() { printf 'Jibo launcher: %s\n' "$*" >&2; exit 1; }
say() { printf 'Jibo launcher: %s\n' "$*"; }
has() { command -v "$1" >/dev/null 2>&1; }

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] ||
  die 'This package requires Linux x86_64 (including x86_64 WSL).'

need_sudo() {
  if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || die 'sudo is needed for USB access and package installation.'
    sudo -v || die 'Administrator authorization was not granted.'
  fi
}

as_root() {
  if (( EUID == 0 )); then "$@"; else sudo "$@"; fi
}

stop_usb_monitor() {
  if [[ -n $usb_monitor_pid ]]; then
    kill "$usb_monitor_pid" 2>/dev/null || true
    wait "$usb_monitor_pid" 2>/dev/null || true
  fi
}

cleanup() {
  stop_usb_monitor
  if [[ -n $package_stage_dir ]]; then rm -rf -- "$package_stage_dir"; fi
}
trap cleanup EXIT

start_wsl_usb_handoff() {
  [[ -n ${WSL_DISTRO_NAME:-} && ${JIBO_MANUAL_USB:-0} != 1 && -f $repo_dir/run.ps1 ]] || return 0
  local powershell win_script
  if [[ -n ${JIBO_WINDOWS_POWERSHELL:-} ]]; then
    powershell=$JIBO_WINDOWS_POWERSHELL
  elif has powershell.exe; then
    powershell=$(command -v powershell.exe)
  elif [[ -x /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe ]]; then
    powershell=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
  else
    say 'Windows PowerShell is unavailable from WSL; attach Jibo USB manually with usbipd.'
    return 0
  fi
  if ! has wslpath; then
    say 'wslpath is unavailable; attach Jibo USB manually with usbipd.'
    return 0
  fi
  win_script=$(wslpath -w "$repo_dir/run.ps1") || {
    say 'Could not locate the Windows USB helper; attach Jibo USB manually with usbipd.'
    return 0
  }
  if ! "$powershell" -NoProfile -ExecutionPolicy Bypass -File "$win_script" -UsbOnly -Distro "$WSL_DISTRO_NAME"; then
    say 'Automatic USB attachment did not complete. You can attach Jibo manually with usbipd.'
    return 0
  fi
  "$powershell" -NoProfile -ExecutionPolicy Bypass -File "$win_script" -UsbOnly -MonitorUsb -Distro "$WSL_DISTRO_NAME" &
  usb_monitor_pid=$!
}

launch() {
  command -v python3 >/dev/null 2>&1 || die 'Python 3 is required to run the package.'
  need_sudo
  start_wsl_usb_handoff
  say 'Opening the toolkit.'
  as_root python3 "$package" "$@"
}

if [[ -f $package && -f $repo_dir/scripts/check_package.py ]] && has python3; then
  if python3 "$repo_dir/scripts/check_package.py" "$package"; then
    launch "$@"
    exit $?
  fi
fi

say 'Preparing the local package; checking build dependencies.'

# Each distribution receives only packages needed by the missing commands.
missing=()
add_missing() {
  local item
  for item in "${missing[@]}"; do [[ $item == "$1" ]] && return; done
  missing+=("$1")
}
has python3 || add_missing python3
has git || add_missing git
has patch || add_missing patch
has make || add_missing make
has gcc || add_missing gcc
for tool in gcc as nm objcopy objdump; do
  has "arm-none-eabi-$tool" || add_missing arm-toolchain
done
if [[ -z ${JIBO_DFU_UTIL:-} ]] && ! has dfu-util; then add_missing dfu-util; fi
if has python3 && ! python3 -c 'import ctypes; ctypes.CDLL("libusb-1.0.so.0")' >/dev/null 2>&1; then
  add_missing libusb
fi

if ((${#missing[@]})); then
  if has apt-get; then
    packages=()
    for item in "${missing[@]}"; do
      case $item in
        python3|git|patch|make|gcc|dfu-util) packages+=("$item") ;;
        arm-toolchain) packages+=(gcc-arm-none-eabi binutils-arm-none-eabi) ;;
        libusb) packages+=(libusb-1.0-0) ;;
      esac
    done
    say "Installing missing packages with apt: ${packages[*]}"
    need_sudo
    as_root apt-get update
    as_root apt-get install -y "${packages[@]}"
  elif has dnf; then
    packages=()
    for item in "${missing[@]}"; do
      case $item in
        python3|git|patch|make|gcc|dfu-util) packages+=("$item") ;;
        arm-toolchain) packages+=(arm-none-eabi-gcc-cs arm-none-eabi-binutils-cs) ;;
        libusb) packages+=(libusb1) ;;
      esac
    done
    say "Installing missing packages with dnf: ${packages[*]}"
    need_sudo
    as_root dnf install -y "${packages[@]}"
  elif has pacman; then
    packages=()
    for item in "${missing[@]}"; do
      case $item in
        python3) packages+=(python) ;;
        git|patch|make|gcc|dfu-util) packages+=("$item") ;;
        arm-toolchain) packages+=(arm-none-eabi-gcc arm-none-eabi-binutils) ;;
        libusb) packages+=(libusb) ;;
      esac
    done
    say "Installing missing packages with pacman: ${packages[*]}"
    need_sudo
    as_root pacman -S --needed --noconfirm "${packages[@]}"
  else
    die "No supported package manager found; install: ${missing[*]}"
  fi
fi

for required in python3 git patch make gcc arm-none-eabi-gcc arm-none-eabi-as arm-none-eabi-nm arm-none-eabi-objcopy arm-none-eabi-objdump; do
  has "$required" || die "Required build command is still missing: $required"
done
python3 -c 'import ctypes; ctypes.CDLL("libusb-1.0.so.0")' >/dev/null 2>&1 ||
  die 'The libusb-1.0 runtime library is still missing.'

dfu_util=${JIBO_DFU_UTIL:-$(command -v dfu-util || true)}
[[ -n $dfu_util && -x $dfu_util ]] || die 'dfu-util is still missing. Set JIBO_DFU_UTIL to its executable path if it is installed elsewhere.'

[[ -f $loader ]] || die "The RAM loader is missing at $loader. Supply its path with JIBO_LOADER=/path/to/loader.bin."

if [[ ! -d $shofel_src ]]; then
  say 'Downloading the pinned ShofEL source.'
  mkdir -p -- "$(dirname -- "$shofel_src")"
  git clone https://github.com/devsparx/ShofEL2-for-T124.git "$shofel_src"
  git -C "$shofel_src" checkout --detach "$shofel_commit"
fi

say 'Building the USB entry helper.'
python3 "$repo_dir/scripts/build_file_level_entry.py" \
  --source "$shofel_src" --loader "$loader" --out "$entry_dir"
for file in shofel2_t124 intermezzo.bin dfu_stage2.bin; do
  [[ -s $entry_dir/$file ]] || die "The ShofEL build did not create $file."
done

say 'Packaging the toolkit.'
mkdir -p -- "$(dirname -- "$package")"
package_stage_dir=$(mktemp -d "$(dirname -- "$package")/.jibo-package.XXXXXXXX")
staged_package=$package_stage_dir/jibo-dfu.pyz
python3 "$repo_dir/scripts/package.py" \
  --loader "$loader" \
  --shofel2 "$entry_dir/shofel2_t124" \
  --intermezzo "$entry_dir/intermezzo.bin" \
  --dfu-stage "$entry_dir/dfu_stage2.bin" \
  --dfu-util "$dfu_util" \
  --out "$staged_package"
[[ -s $staged_package ]] || die 'Packaging did not create the toolkit.'
python3 "$repo_dir/scripts/check_package.py" "$staged_package" ||
  die 'The new package did not pass its DFU entry check.'
mv -f -- "$staged_package" "$package"
[[ -s $package ]] || die 'Packaging did not create the toolkit.'
launch "$@"

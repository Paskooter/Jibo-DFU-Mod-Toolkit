#!/usr/bin/env bash
# Build the local Linux package when needed, then open the Jibo terminal UI.
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
package=${JIBO_PYZ:-$repo_dir/dist/jibo-dfu-linux-x86_64.pyz}
loader=${JIBO_LOADER:-$repo_dir/assets/loader.bin}
shofel_src=${JIBO_SHOFEL_SRC:-$repo_dir/.build/ShofEL2-for-T124}
shofel_commit=31ac3a260c8a1501869aff6690b3b9ad4904ef58

die() { printf 'Jibo launcher: %s\n' "$*" >&2; exit 1; }
say() { printf 'Jibo launcher: %s\n' "$*"; }

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

launch() {
  command -v python3 >/dev/null 2>&1 || die 'Python 3 is required to run the package.'
  need_sudo
  say 'Opening the toolkit.'
  as_root python3 "$package" "$@"
}

if [[ -f $package ]]; then
  launch "$@"
  exit $?
fi

say 'The local package is missing; checking build dependencies.'

# Each distribution receives only packages needed by the missing commands.
missing=()
has() { command -v "$1" >/dev/null 2>&1; }
add_missing() { missing+=("$1"); }
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
    as_root pacman -Sy --needed --noconfirm "${packages[@]}"
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

if [[ ! -f $loader ]]; then
  # A local pre-release image can be used without copying it into the source tree.
  local_loader=$repo_dir/.build/signed-skills-bundle-20260923/loader.bin
  if [[ -z ${JIBO_LOADER:-} && -f $local_loader ]]; then
    loader=$local_loader
  else
    die "The pinned RAM loader is missing at $loader. Supply its path with JIBO_LOADER=/path/to/loader.bin."
  fi
fi

if [[ ! -d $shofel_src ]]; then
  say 'Downloading the pinned ShofEL source.'
  mkdir -p -- "$(dirname -- "$shofel_src")"
  git clone https://github.com/devsparx/ShofEL2-for-T124.git "$shofel_src"
  git -C "$shofel_src" checkout --detach "$shofel_commit"
fi

if git -C "$shofel_src" apply --reverse --check "$repo_dir/patches/shofel2-dfu-entry.patch" >/dev/null 2>&1; then
  say 'ShofEL toolkit patch is already applied.'
else
  say 'Applying the ShofEL toolkit patch.'
  git -C "$shofel_src" apply --check "$repo_dir/patches/shofel2-dfu-entry.patch" ||
    die 'The ShofEL patch does not apply to this source. Set JIBO_SHOFEL_SRC to the pinned source tree or remove the stale build tree.'
  git -C "$shofel_src" apply "$repo_dir/patches/shofel2-dfu-entry.patch"
fi

say 'Building the USB entry helper.'
make -C "$shofel_src" DFU_STAGE2_ENABLE_LAUNCH=1 all test
for file in shofel2_t124 intermezzo.bin dfu_stage2.bin; do
  [[ -s $shofel_src/$file ]] || die "The ShofEL build did not create $file."
done

say 'Packaging the toolkit.'
mkdir -p -- "$(dirname -- "$package")"
package_stage_dir=$(mktemp -d "$(dirname -- "$package")/.jibo-package.XXXXXXXX")
trap 'rm -rf -- "$package_stage_dir"' EXIT
staged_package=$package_stage_dir/jibo-dfu.pyz
python3 "$repo_dir/scripts/package.py" \
  --loader "$loader" \
  --shofel2 "$shofel_src/shofel2_t124" \
  --intermezzo "$shofel_src/intermezzo.bin" \
  --dfu-stage "$shofel_src/dfu_stage2.bin" \
  --dfu-util "$dfu_util" \
  --out "$staged_package"
[[ -s $staged_package ]] || die 'Packaging did not create the toolkit.'
mv -n -- "$staged_package" "$package"
[[ -s $package ]] || die 'Packaging did not create the toolkit.'
launch "$@"

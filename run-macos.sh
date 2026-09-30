#!/usr/bin/env bash
# Native macOS launcher: RCM-to-DFU entry, DFU transfers, and local-image tools.
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
die() { printf 'Jibo Mac launcher: %s\n' "$*" >&2; exit 1; }
say() { printf 'Jibo Mac launcher: %s\n' "$*" >&2; }

[[ $(uname -s) == Darwin ]] || die 'This launcher requires macOS.'
machine=$(uname -m)
case $machine in
  arm64) default_prefix=/opt/homebrew ;;
  x86_64) default_prefix=/usr/local ;;
  *) die "Unsupported Mac architecture: $machine" ;;
esac
if [[ $(sysctl -in sysctl.proc_translated 2>/dev/null || true) == 1 ]]; then
  die 'Open a native Terminal session with Rosetta disabled and use ARM dependencies.'
fi

# Use installed tools. Keeping installation separate also supports older Macs
# through MacPorts without maintaining package-manager installation workflows.
prefix=${JIBO_MAC_PREFIX:-}
if [[ -z $prefix ]]; then
  if [[ -x $default_prefix/bin/brew ]]; then
    prefix=$("$default_prefix/bin/brew" --prefix) || die 'Could not read the Homebrew prefix. Set JIBO_MAC_PREFIX to your dependency prefix.'
  elif command -v brew >/dev/null 2>&1; then
    prefix=$(brew --prefix) || die 'Could not read the Homebrew prefix. Set JIBO_MAC_PREFIX to your dependency prefix.'
  else
    prefix=/opt/local
  fi
fi
install_hint='Install dependencies with Homebrew: brew install python dfu-util libusb e2fsprogs; or with MacPorts: sudo port install python312 dfu-util libusb e2fsprogs. See docs/macos.md.'
export PATH="$prefix/opt/e2fsprogs/sbin:$prefix/opt/e2fsprogs/bin:$prefix/bin:$prefix/sbin:$PATH"
if [[ -z ${JIBO_LIBUSB:-} ]]; then
  for library in "$prefix/opt/libusb/lib/libusb-1.0.dylib" "$prefix/lib/libusb-1.0.dylib"; do
    if [[ -f $library ]]; then export JIBO_LIBUSB=$library; break; fi
  done
fi
export JIBO_DFU_UTIL=${JIBO_DFU_UTIL:-$(command -v dfu-util || true)}
python=${JIBO_PYTHON:-}
if [[ -z $python ]]; then
  if [[ -x $prefix/bin/python3 ]]; then python=$prefix/bin/python3
  elif [[ -x $prefix/bin/python3.12 ]]; then python=$prefix/bin/python3.12
  else python=$(command -v python3 || command -v python3.12 || true)
  fi
fi
[[ -n $python && -x $python ]] || die "Python 3.10 or later is required. $install_hint"
[[ -n $JIBO_DFU_UTIL && -x $JIBO_DFU_UTIL ]] || die "dfu-util is missing or not executable. $install_hint"

"$python" -c '
import platform, shutil, subprocess, sys
if sys.version_info < (3, 10):
    sys.exit("Jibo requires Python 3.10 or later.")
if platform.machine() != sys.argv[2]:
    sys.exit("Python must match the native Mac architecture: " + sys.argv[2])
sys.path.insert(0, sys.argv[1])
import jibo_dfu_bounded as bounded
import jibo_tui
try:
    bounded._load_libusb()
    for name in ("debugfs", "e2fsck", "resize2fs", "dumpe2fs"):
        if not shutil.which(name):
            sys.exit("Missing e2fsprogs command: " + name)
    result = subprocess.run([sys.argv[3], "--version"], capture_output=True,
                            text=True, timeout=10)
    if result.returncode:
        sys.exit("Could not start native dfu-util: " + result.stderr)
except (bounded.BoundedDfuError, OSError, subprocess.TimeoutExpired) as exc:
    sys.exit(str(exc))
' "$repo_dir" "$machine" "$JIBO_DFU_UTIL" || die "Dependency checks failed. $install_hint"

# Build the native RCM-to-DFU entry helper (reused instantly once intact).
# An explicit JIBO_SHOFEL2 helper skips the build entirely.
if [[ -z ${JIBO_SHOFEL2:-} ]]; then
  shofel_src=${JIBO_SHOFEL_SRC:-$repo_dir/.build/ShofEL2-for-T124}
  entry_dir=$repo_dir/.build/file-level-entry
  if [[ ! -d $shofel_src ]]; then
    command -v git >/dev/null 2>&1 ||
      die 'git is required to fetch the ShofEL source. Install the Xcode Command Line Tools.'
    say 'Downloading the pinned ShofEL source.'
    mkdir -p -- "$(dirname "$shofel_src")"
    git clone https://github.com/devsparx/ShofEL2-for-T124.git "$shofel_src" ||
      die 'Could not download the ShofEL source.'
  fi
  for build_tool in make cc; do
    command -v "$build_tool" >/dev/null 2>&1 ||
      die "The C build tools are missing ($build_tool). Install the Xcode Command Line Tools."
  done
  build_args=(--source "$shofel_src")
  if [[ -n ${JIBO_PAYLOADS_FROM:-} ]]; then
    build_args+=(--payloads-from "$JIBO_PAYLOADS_FROM")
  fi
  "$python" "$repo_dir/scripts/build_file_level_entry.py" "${build_args[@]}" ||
    die 'The native DFU entry helper could not be built. See docs/macos.md for the payload options.'
  export JIBO_SHOFEL2=$entry_dir/shofel2_t124
fi

say 'Experimental macOS workflow: RCM-to-DFU entry is available and unverified on Mac hardware. Partition actions need the Jibo DFU loader it starts.'
exec "$python" "$repo_dir/jibo_dfu.py" "$@"

#!/usr/bin/env python3
"""Check that a cached toolkit package matches this checkout and can enter DFU."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

try:
    from .package import (PINNED_LOADER_SHA256, PINNED_LOADER_SIZE,
                          candidate_loader_hash_matches)
except ImportError:
    from package import (PINNED_LOADER_SHA256, PINNED_LOADER_SIZE,
                         candidate_loader_hash_matches)


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("jibo_dfu.py", "jibo_dfu_bounded.py", "jibo_images.py",
           "jibo_updates.py", "jibo_tui.py")
TOOLS = ("tools/shofel2_t124", "tools/intermezzo.bin",
         "tools/dfu_stage2.bin", "tools/dfu-util")


def check_package(path, source_root=ROOT):
    try:
        with zipfile.ZipFile(path) as archive:
            for name in SOURCES:
                if archive.read(name) != (source_root / name).read_bytes():
                    return False, name + " has changed since the package was built"
            for name in TOOLS:
                if not archive.read(name):
                    return False, name + " is empty"
            loader = archive.read("loader.bin")
            if len(loader) != PINNED_LOADER_SIZE or \
                    hashlib.sha256(loader).hexdigest() != PINNED_LOADER_SHA256:
                return False, "the bundled RAM loader does not match the pinned image"
            for name in ("tools/shofel2_t124", "tools/dfu_stage2.bin"):
                if not candidate_loader_hash_matches(archive.read(name),
                                                     PINNED_LOADER_SHA256):
                    return False, name + " does not embed the pinned RAM loader hash"
            with tempfile.TemporaryDirectory(prefix="jibo-package-check-") as directory:
                shofel = Path(directory) / "shofel2_t124"
                shofel.write_bytes(archive.read("tools/shofel2_t124"))
                shofel.chmod(0o700)
                result = subprocess.run([str(shofel), "--dfu-stage-capability"],
                                        capture_output=True, text=True, timeout=5,
                                        check=False)
                if result.returncode != 0 or result.stdout.strip() != "dfu-stage-launch=1":
                    return False, "the bundled ShofEL tool cannot launch DFU"
    except (OSError, KeyError, ValueError, zipfile.BadZipFile,
            subprocess.TimeoutExpired) as exc:
        return False, "the package could not be checked: " + str(exc)
    return True, "ready"


def main():
    if len(sys.argv) != 2:
        print("Usage: check_package.py PACKAGE.pyz", file=sys.stderr)
        return 2
    ready, reason = check_package(Path(sys.argv[1]))
    if not ready:
        print("Jibo launcher: rebuilding package because " + reason + ".", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

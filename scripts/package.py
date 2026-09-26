#!/usr/bin/env python3
"""Create a single Linux x86_64 Python zip application from explicit inputs."""
import argparse
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PINNED_LOADER_SIZE = 432_000
PINNED_LOADER_SHA256 = "fd5fc5b1759ddbdbb0da88ac89c425ab95eb0bc494917cfe27daf71e55a31095"
BASELINE_LOADER_SHA256 = bytes.fromhex(
    "8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689")


def candidate_loader_hash_matches(content, loader_sha256):
    """Check that a stage helper embeds this loader and excludes its old pin."""
    expected = bytes.fromhex(loader_sha256)
    return content.count(expected) == 1 and BASELINE_LOADER_SHA256 not in content

BOOTSTRAP = '''import os, pathlib, subprocess, sys, tempfile, zipfile
with tempfile.TemporaryDirectory(prefix="jibo-dfu-") as directory:
    root = pathlib.Path(directory)
    with zipfile.ZipFile(sys.argv[0]) as archive:
        archive.extractall(root)
    for executable in (root / "tools").iterdir():
        executable.chmod(0o755)
    raise SystemExit(subprocess.call([sys.executable, str(root / "jibo_dfu.py"), *sys.argv[1:]]))
'''


def _read_file(parser, label, path):
    """Read a required, non-empty regular file without following a final symlink."""
    path = Path(path)
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            parser.error(label + " must be a regular, non-symlink file: " + str(path))
        content = path.read_bytes()
    except OSError as exc:
        parser.error("Could not read " + label + " " + str(path) + ": " + str(exc))
    if not content:
        parser.error(label + " must not be empty: " + str(path))
    return content


def _check_shofel_launch(parser, path):
    if not os.access(path, os.X_OK):
        parser.error("ShofEL host tool is not executable: " + str(path))
    try:
        result = subprocess.run([str(path), "--dfu-stage-capability"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        parser.error("Could not check the ShofEL DFU launch capability: " + str(exc))
    if result.returncode != 0 or result.stdout.strip() != "dfu-stage-launch=1":
        parser.error("ShofEL was built without DFU launch support. Rebuild with "
                     "DFU_STAGE2_ENABLE_LAUNCH=1 before packaging.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loader", required=True, type=Path,
                        help="Raw Jibo RAM DFU loader image; it is padded and checked against the pinned image")
    parser.add_argument("--shofel2", required=True, type=Path,
                        help="Launch-enabled shofel2_t124 executable")
    parser.add_argument("--intermezzo", required=True, type=Path,
                        help="ShofEL intermezzo.bin RCM payload")
    parser.add_argument("--dfu-stage", required=True, type=Path,
                        help="ShofEL dfu_stage2.bin RAM loader payload")
    parser.add_argument("--dfu-util", required=True, type=Path,
                        help="dfu-util executable")
    parser.add_argument("--out", required=True, type=Path,
                        help="New output .pyz path")
    args = parser.parse_args()

    supplied = {
        "loader.bin": args.loader,
        "tools/shofel2_t124": args.shofel2,
        "tools/intermezzo.bin": args.intermezzo,
        "tools/dfu_stage2.bin": args.dfu_stage,
        "tools/dfu-util": args.dfu_util,
    }
    data = {name: _read_file(parser, name, path) for name, path in supplied.items()}
    _check_shofel_launch(parser, args.shofel2)

    loader = data["loader.bin"]
    loader += bytes((-len(loader)) % 16)
    if len(loader) != PINNED_LOADER_SIZE:
        parser.error("Padded loader has {} bytes; expected the pinned {}-byte image.".format(
            len(loader), PINNED_LOADER_SIZE))
    digest = hashlib.sha256(loader).hexdigest()
    if digest != PINNED_LOADER_SHA256:
        parser.error("Padded loader SHA-256 does not match the pinned RAM DFU image.")
    for name in ("tools/shofel2_t124", "tools/dfu_stage2.bin"):
        if not candidate_loader_hash_matches(data[name], digest):
            parser.error(name + " does not embed exactly the pinned RAM loader hash.")
    data["loader.bin"] = loader

    sources = {
        "jibo_dfu.py": ROOT / "jibo_dfu.py",
        "jibo_dfu_bounded.py": ROOT / "jibo_dfu_bounded.py",
        "jibo_images.py": ROOT / "jibo_images.py",
        "jibo_updates.py": ROOT / "jibo_updates.py",
        "jibo_tui.py": ROOT / "jibo_tui.py",
        "README.md": ROOT / "README.md",
    }
    for name, path in sources.items():
        data[name] = _read_file(parser, name, path)

    for name, content in data.items():
        if b"PRIVATE KEY-----" in content:
            parser.error("Private key material detected in " + name)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("xb") as stream:
        stream.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("__main__.py", BOOTSTRAP)
            for name, content in data.items():
                archive.writestr(name, content)
    os.chmod(args.out, 0o755)
    print(args.out)


if __name__ == "__main__":
    main()

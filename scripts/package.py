#!/usr/bin/env python3
"""Create a single Linux x86_64 Python zip application from explicit inputs."""
import argparse
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jibo_dfu import FILES, load_bundle

BOOTSTRAP = '''import os, pathlib, subprocess, sys, tempfile, zipfile
with tempfile.TemporaryDirectory(prefix="jibo-dfu-") as directory:
    root = pathlib.Path(directory)
    with zipfile.ZipFile(sys.argv[0]) as archive:
        archive.extractall(root)
    for executable in (root / "tools").iterdir():
        executable.chmod(0o755)
    raise SystemExit(subprocess.call([sys.executable, str(root / "jibo_dfu.py"), *sys.argv[1:]]))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "tegrarcm", "dfu-util", "libcryptopp", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    load_bundle(args.bundle)
    selected = {"jibo_dfu.py": ROOT / "jibo_dfu.py", "jibo_images.py": ROOT / "jibo_images.py",
                "README.md": ROOT / "README.md",
                "tools/tegrarcm": args.tegrarcm, "tools/dfu-util": args.dfu_util,
                "lib/libcryptopp.so": args.libcryptopp}
    selected.update({"bundles/default/" + name: args.bundle / name for name in (*FILES, "manifest.json")})
    data = {name: path.read_bytes() for name, path in selected.items()}
    for name, content in data.items():
        if b"PRIVATE KEY-----" in content:
            raise ValueError("Private key material detected in " + name)
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

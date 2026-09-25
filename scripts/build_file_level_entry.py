#!/usr/bin/env python3
"""Build an isolated ShofEL entry pair for the experimental file-RPC loader."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SHOFEL_COMMIT = "31ac3a260c8a1501869aff6690b3b9ad4904ef58"
SHOFEL_REPOSITORY = "https://github.com/devsparx/ShofEL2-for-T124.git"
PINNED_SIZE = "#define DFU_STAGE2_LOADER_SIZE 415088u"
PINNED_HASH = (
    "    0x8f, 0x46, 0x06, 0x2f, 0x2d, 0x20, 0x18, 0x24,\n"
    "    0x33, 0x70, 0x93, 0xa1, 0xe4, 0xc1, 0x54, 0xe3,\n"
    "    0x04, 0x8c, 0x01, 0x9b, 0x14, 0x79, 0x30, 0xda,\n"
    "    0x35, 0xb9, 0xd6, 0x2e, 0x00, 0xc5, 0xe6, 0x89"
)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def run(*command, cwd=None):
    subprocess.run(command, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SHOFEL_REPOSITORY,
                        help="ShofEL Git repository, URL or local directory")
    parser.add_argument("--loader", type=Path, default=ROOT / ".build/file-level-candidate/experimental-file-rpc-loader.bin")
    parser.add_argument("--out", type=Path, default=ROOT / ".build/file-level-candidate/shofel-entry")
    args = parser.parse_args()
    if args.loader.is_symlink():
        parser.error("candidate loader must not be a symlink: " + str(args.loader))
    loader = args.loader.resolve()
    out = args.out.resolve()
    if not loader.is_file():
        parser.error("candidate loader is missing: " + str(loader))
    candidate_manifest = loader.parent / "manifest.json"
    if not candidate_manifest.is_file():
        parser.error("candidate manifest is missing: " + str(candidate_manifest))
    candidate = json.loads(candidate_manifest.read_text())
    size = loader.stat().st_size
    sha = digest(loader)
    if (candidate.get("kind") != "experimental-file-rpc-loader" or
            candidate.get("size_bytes") != size or candidate.get("sha256") != sha):
        parser.error("candidate loader does not match its file-RPC build manifest")
    if not 0 < size < 4 * 1024 * 1024:
        parser.error("candidate loader size is outside the stage-2 DRAM range")
    if out.exists():
        parser.error("output directory already exists: " + str(out))

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".shofel-entry-", dir=out.parent) as temp:
        work = Path(temp) / "source"
        run("git", "clone", "--no-checkout", args.source, str(work))
        run("git", "checkout", "--detach", SHOFEL_COMMIT, cwd=work)
        run("git", "apply", "--check", str(ROOT / "patches/shofel2-dfu-entry.patch"), cwd=work)
        run("git", "apply", str(ROOT / "patches/shofel2-dfu-entry.patch"), cwd=work)

        header = work / "include/dfu_stage2_protocol.h"
        original = header.read_text()
        if original.count(PINNED_SIZE) != 1 or original.count(PINNED_HASH) != 1:
            raise RuntimeError("the ShofEL stage protocol does not match the pinned baseline")
        candidate_hash = ",\n".join(
            "    " + ", ".join("0x" + sha[index:index + 2]
                              for index in range(start, start + 16, 2))
            for start in range(0, 64, 16)
        )
        updated = original.replace(PINNED_SIZE,
                                   "#define DFU_STAGE2_LOADER_SIZE {}u".format(size))
        updated = updated.replace(PINNED_HASH, candidate_hash)
        updated = updated.replace(
            "/* SHA-256 of the current signed-skills-bundle-20260923/loader.bin. */",
            "/* SHA-256 of the manifest-checked experimental file-RPC loader. */")
        header.write_text(updated)

        run("make", "-B", "DFU_STAGE2_ENABLE_LAUNCH=1", "all", "test", cwd=work)
        files = {}
        for name in ("shofel2_t124", "intermezzo.bin", "dfu_stage2.bin"):
            path = work / name
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError("missing built ShofEL artifact: " + name)
            files[name] = digest(path)
        capability = subprocess.check_output(
            [str(work / "shofel2_t124"), "--dfu-stage-capability"],
            text=True).strip()
        if capability != "dfu-stage-launch=1":
            raise RuntimeError("candidate ShofEL host lacks launch support")
        (work / "candidate-entry-manifest.json").write_text(
            json.dumps({"loader_sha256": sha, "loader_size": size,
                        "shofel_commit": SHOFEL_COMMIT, "files_sha256": files},
                       indent=2) + "\n")
        shutil.move(str(work), str(out))
    print("Candidate entry pair built:", out)
    print("Loader:", loader)
    print("Loader SHA-256:", sha)


if __name__ == "__main__":
    main()

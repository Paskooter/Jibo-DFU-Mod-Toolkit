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
    parser.add_argument("--loader", type=Path, default=ROOT / "assets/loader.bin")
    parser.add_argument("--out", type=Path, default=ROOT / ".build/file-level-entry")
    args = parser.parse_args()
    if args.loader.is_symlink():
        parser.error("candidate loader must not be a symlink: " + str(args.loader))
    loader = args.loader.resolve()
    out = args.out.resolve()
    if not loader.is_file():
        parser.error("candidate loader is missing: " + str(loader))
    size = loader.stat().st_size
    sha = digest(loader)
    pinned = sha == digest(ROOT / "assets/loader.bin")
    candidate_manifest = loader.parent / "manifest.json"
    if (not candidate_manifest.is_file() and
            pinned):
        candidate_manifest = ROOT / "assets/manifest.json"
    if not candidate_manifest.is_file():
        parser.error("candidate manifest is missing: " + str(candidate_manifest))
    candidate = json.loads(candidate_manifest.read_text())
    patch_sha = hashlib.sha256((ROOT / "firmware/file-level.patch").read_bytes()).hexdigest()
    if (candidate.get("kind") != "experimental-file-rpc-loader" or
            candidate.get("size_bytes") != size or candidate.get("sha256") != sha or
            (not pinned and (candidate.get("protocol") != "jibo-file-v2" or
                             candidate.get("source_file_level_patch_sha256") != patch_sha))):
        parser.error("candidate loader does not match its file-RPC build manifest")
    if not 0 < size < 4 * 1024 * 1024:
        parser.error("candidate loader size is outside the stage-2 DRAM range")
    if out.exists():
        manifest_path = out / "candidate-entry-manifest.json"
        try:
            built = json.loads(manifest_path.read_text())
            files = built["files_sha256"]
            if (built["loader_sha256"] == sha and built["loader_size"] == size and
                    built["shofel_commit"] == SHOFEL_COMMIT and
                    all(digest(out / name) == files[name]
                        for name in ("shofel2_t124", "intermezzo.bin", "dfu_stage2.bin"))):
                print("Reusing the matching ShofEL entry helper:", out)
                return
        except (OSError, KeyError, ValueError, TypeError):
            pass
        parser.error("existing ShofEL entry helper does not match this loader: " + str(out))

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

        # The stage payload also carried a private copy of the original hash.
        # Make its request and readback checks use the shared protocol value.
        payload = work / "payloads/dfu_stage2.c"
        source = payload.read_text()
        pinned_array = "static const u8 expected_sha256[32] = {\n" + PINNED_HASH + "\n};\n"
        if source.count(pinned_array) != 1 or source.count("expected_sha256[i]") != 2:
            raise RuntimeError("the stage payload does not match the pinned hash baseline")
        source = source.replace(pinned_array, "")
        source = source.replace("expected_sha256[i]", "DFU_STAGE2_EXPECTED_SHA256[i]")
        payload.write_text(source)

        run("make", "-B", "DFU_STAGE2_ENABLE_LAUNCH=1", "all", "test", cwd=work)
        files = {}
        for name in ("shofel2_t124", "intermezzo.bin", "dfu_stage2.bin"):
            path = work / name
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError("missing built ShofEL artifact: " + name)
            files[name] = digest(path)
        for name in ("shofel2_t124", "dfu_stage2.bin"):
            binary = (work / name).read_bytes()
            if binary.count(bytes.fromhex(sha)) != 1 or bytes.fromhex(
                    "8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689") in binary:
                raise RuntimeError("built {} does not contain only the candidate loader hash".format(name))
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

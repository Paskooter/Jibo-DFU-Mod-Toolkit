#!/usr/bin/env python3
"""Build an isolated ShofEL entry pair for the experimental file-RPC loader."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
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
ARM_PAYLOADS = ("intermezzo.bin", "dfu_stage2.bin")


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def run(*command, cwd=None):
    subprocess.run(command, cwd=cwd, check=True)


def host_platform():
    """Return the host identity that a built shofel2_t124 binary belongs to."""
    return "-".join((sys.platform, platform.machine()))


def candidate_libusb_prefixes():
    """Return libusb prefixes in the same order the toolkit's libusb loader uses."""
    prefixes = []
    override = os.environ.get("JIBO_LIBUSB")
    if override:
        prefixes.append(str(Path(override).resolve().parent.parent))
    prefixes.extend(("/opt/homebrew/opt/libusb", "/usr/local/opt/libusb", "/opt/local"))
    return list(dict.fromkeys(prefixes))


def resolve_libusb_prefix(explicit):
    for prefix in ([explicit] if explicit else candidate_libusb_prefixes()):
        if any((Path(prefix) / branch).is_file()
               for branch in ("lib/libusb-1.0.dylib", "lib/libusb-1.0.so", "lib/libusb-1.0.so.0")):
            return Path(prefix)
    raise RuntimeError(
        "libusb-1.0 development files were not found for the native ShofEL host build. "
        "Pass --libusb-prefix or set JIBO_LIBUSB to the libusb prefix or dylib path.")


def darwin_host_make_variables(libusb_prefix):
    """Return make overrides that build the ShofEL host with clang and libusb."""
    for command in ("cc", "clang", "/usr/bin/gcc"):
        if shutil.which(command):
            break
    else:
        raise RuntimeError("A C compiler is required; install the Xcode Command Line Tools.")
    prefix = Path(libusb_prefix)
    includes = ["-I" + str(prefix / "include"), "-I" + str(prefix / "include/libusb-1.0")]
    return ("CC_x86=" + command,
            "CFLAGS_x86=-Wall -Werror -I include -MMD -DJIBO_LIBUSB_BACKEND=1 "
            "-DDFU_STAGE2_ENABLE_LAUNCH=1 " + " ".join(includes),
            "LIBS_x86=-L" + str(prefix / "lib") + " -lusb-1.0")


def validate_payload_source(source, loader_sha, patch_sha, commit):
    """Verify a reusable ARM payload directory against its pinned manifest.

    Raises RuntimeError when the payloads do not exactly match the current
    loader, patch, or ShofEL commit, or when their digests differ.
    """
    source = Path(source)
    built = None
    for manifest_name in ("manifest.json", "candidate-entry-manifest.json"):
        manifest = source / manifest_name
        if not manifest.is_file():
            continue
        try:
            built = json.loads(manifest.read_text())
        except ValueError as exc:
            raise RuntimeError(str(source) + " has an unreadable payload manifest: " + str(exc))
        break
    if built is None:
        raise RuntimeError(str(source) + " has no payload manifest")
    if built.get("shofel_commit") != commit:
        raise RuntimeError(str(source) + " was built from a different ShofEL commit")
    if built.get("patch_sha256") != patch_sha:
        raise RuntimeError(str(source) + " was built with a different ShofEL patch")
    if built.get("loader_sha256") != loader_sha:
        raise RuntimeError(str(source) + " was built for a different RAM loader")
    files = built["files_sha256"]
    for name in ARM_PAYLOADS:
        path = source / name
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(str(source) + " is missing " + name)
        if digest(path) != files[name]:
            raise RuntimeError(str(source) + "/" + name + " does not match its manifest digest")
    return source


def copy_reusable_payloads(work, source):
    """Copy ARM payloads from a verified intact entry build instead of rebuilding."""
    for name in ARM_PAYLOADS:
        source_file = Path(source) / name
        if not source_file.is_file() or source_file.stat().st_size == 0:
            raise RuntimeError("The reusable entry build is missing " + name)
        shutil.copy2(source_file, Path(work) / name)


def verify_built_pair(work, loader_sha):
    """Verify the entry pair artifacts and return their digests."""
    files = {}
    for name in ("shofel2_t124",) + ARM_PAYLOADS:
        path = work / name
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("missing built ShofEL artifact: " + name)
        files[name] = digest(path)
    for name in ("shofel2_t124", "dfu_stage2.bin"):
        binary = (work / name).read_bytes()
        if binary.count(bytes.fromhex(loader_sha)) != 1 or bytes.fromhex(
                "8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689") in binary:
            raise RuntimeError("built {} does not contain only the candidate loader hash".format(name))
    capability = subprocess.check_output(
        [str(work / "shofel2_t124"), "--dfu-stage-capability"],
        text=True).strip()
    if capability != "dfu-stage-launch=1":
        raise RuntimeError("candidate ShofEL host lacks launch support")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SHOFEL_REPOSITORY,
                        help="ShofEL Git repository, URL or local directory")
    parser.add_argument("--libusb-prefix", default=None,
                        help="libusb prefix for the macOS host build (Homebrew or MacPorts keg)")
    parser.add_argument("--payloads-from", default=None, type=Path,
                        help="Reuse the ARM payloads from a manifest-verified payload "
                             "source instead of rebuilding them (macOS without an ARM "
                             "toolchain); defaults to the pinned assets/entry-payloads")
    parser.add_argument("--build-payloads", action="store_true",
                        help="Build the ARM payloads from source with arm-none-eabi-gcc "
                             "instead of reusing a verified payload source")
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
    patch_path = ROOT / "patches/shofel2-dfu-entry.patch"
    patch_sha = digest(patch_path)
    candidate_manifest = loader.parent / "manifest.json"
    if (not candidate_manifest.is_file() and
            sha == digest(ROOT / "assets/loader.bin")):
        candidate_manifest = ROOT / "assets/manifest.json"
    if not candidate_manifest.is_file():
        parser.error("candidate manifest is missing: " + str(candidate_manifest))
    candidate = json.loads(candidate_manifest.read_text())
    if (candidate.get("kind") != "experimental-file-rpc-loader" or
            candidate.get("size_bytes") != size or candidate.get("sha256") != sha):
        parser.error("candidate loader does not match its file-RPC build manifest")
    if not 0 < size < 4 * 1024 * 1024:
        parser.error("candidate loader size is outside the stage-2 DRAM range")
    replace_existing = False
    payload_source = None
    if args.payloads_from and args.build_payloads:
        parser.error("--payloads-from and --build-payloads are mutually exclusive")
    if out.exists():
        manifest_path = out / "candidate-entry-manifest.json"
        try:
            built = json.loads(manifest_path.read_text())
            files = built["files_sha256"]
            generated = (built["shofel_commit"] == SHOFEL_COMMIT and
                         all(digest(out / name) == files[name]
                             for name in ("shofel2_t124",) + ARM_PAYLOADS))
        except (OSError, KeyError, ValueError, TypeError):
            generated = False
        if not generated:
            parser.error("existing ShofEL entry helper is not an intact generated build: " + str(out))
        matches_current = (built.get("loader_sha256") == sha and
                           built.get("loader_size") == size and
                           built.get("patch_sha256") == patch_sha)
        if matches_current and built.get("host_platform") == host_platform() \
                and not args.build_payloads:
            print("Reusing the matching ShofEL entry helper:", out)
            return
        if matches_current and not args.build_payloads:
            payload_source = out
        replace_existing = True

    on_darwin = sys.platform == "darwin"
    if on_darwin and not args.build_payloads:
        if args.payloads_from:
            try:
                payload_source = validate_payload_source(
                    args.payloads_from, sha, patch_sha, SHOFEL_COMMIT)
            except (RuntimeError, OSError, KeyError) as exc:
                parser.error(str(exc))
        elif payload_source is None:
            # Pinned payloads shipped with the toolkit; validated against the
            # current loader, patch, and commit before use.
            try:
                payload_source = validate_payload_source(
                    ROOT / "assets" / "entry-payloads", sha, patch_sha, SHOFEL_COMMIT)
            except (RuntimeError, OSError, KeyError):
                payload_source = None
        elif payload_source is not None:
            try:
                payload_source = validate_payload_source(
                    payload_source, sha, patch_sha, SHOFEL_COMMIT)
            except (RuntimeError, OSError, KeyError) as exc:
                parser.error(str(exc))
        if payload_source is None and not shutil.which("arm-none-eabi-gcc"):
            raise RuntimeError(
                "The ARM payloads need either the pinned assets/entry-payloads directory "
                "(missing or stale for this loader and patch) or arm-none-eabi-gcc "
                "(brew install arm-none-eabi-gcc, bottled for Apple silicon, "
                "or port install arm-none-eabi-gcc). See docs/macos.md.")

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".shofel-entry-", dir=out.parent) as temp:
        work = Path(temp) / "source"
        run("git", "clone", "--no-checkout", args.source, str(work))
        run("git", "checkout", "--detach", SHOFEL_COMMIT, cwd=work)
        run("git", "apply", "--check", str(patch_path), cwd=work)
        run("git", "apply", str(patch_path), cwd=work)

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

        if on_darwin:
            host_vars = darwin_host_make_variables(
                resolve_libusb_prefix(args.libusb_prefix))
            if payload_source is not None:
                run("make", "-B", "shofel2_t124", *host_vars, cwd=work)
                copy_reusable_payloads(work, payload_source)
            else:
                run("make", "-B", *host_vars, "all", "test", cwd=work)
        else:
            run("make", "-B", "DFU_STAGE2_ENABLE_LAUNCH=1", "all", "test", cwd=work)
        files = verify_built_pair(work, sha)
        (work / "candidate-entry-manifest.json").write_text(
            json.dumps({"loader_sha256": sha, "loader_size": size,
                        "patch_sha256": patch_sha, "host_platform": host_platform(),
                        "shofel_commit": SHOFEL_COMMIT, "files_sha256": files},
                       indent=2) + "\n")
        if replace_existing:
            previous = Path(temp) / "previous-entry"
            out.rename(previous)
            try:
                work.rename(out)
            except OSError:
                previous.rename(out)
                raise
        else:
            shutil.move(str(work), str(out))
    print("Candidate entry pair built:", out)
    print("Loader:", loader)
    print("Loader SHA-256:", sha)


if __name__ == "__main__":
    main()

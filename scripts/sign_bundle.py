#!/usr/bin/env python3
"""Offline T124 bundle preparation. No USB operations; private key stays external."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile


def call(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True, **kwargs)


def sign(key, data, work):
    source = work / "sign-input"
    source.write_bytes(data)
    result = call(["openssl", "dgst", "-sha256", "-sign", key,
                   "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32", source])
    if len(result.stdout) != 256:
        raise ValueError("A 2048-bit RSA key is required")
    return result.stdout


def verify(public, data, signature, work):
    (work / "verify-input").write_bytes(data)
    (work / "verify-signature").write_bytes(signature)
    call(["openssl", "dgst", "-sha256", "-verify", public,
          "-signature", work / "verify-signature", "-sigopt", "rsa_padding_mode:pss",
          "-sigopt", "rsa_pss_saltlen:32", work / "verify-input"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("loader", "bct", "key", "tegrarcm", "mkbctpart", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Output directory already exists; choose a new directory")
    loader = args.loader.read_bytes()
    if b"Jibo RAM DFU v1:" not in loader:
        parser.error("Loader is missing the dedicated recovery entry marker")
    loader += bytes((-len(loader)) % 16)
    bct = bytearray(args.bct.read_bytes())
    if len(bct) != 8192:
        parser.error("BCT must be exactly 8192 bytes")
    with tempfile.TemporaryDirectory(prefix="jibo-sign-") as temp:
        work = Path(temp)
        # Only the temporary directory contains the private-key conversion.
        key_der = work / "private.der"
        call(["openssl", "rsa", "-in", args.key, "-traditional", "-outform", "DER", "-out", key_der])
        os.chmod(key_der, 0o600)
        public = work / "public.pem"
        call(["openssl", "pkey", "-in", args.key, "-pubout", "-out", public])
        modulus = bytes.fromhex(call(["openssl", "rsa", "-in", args.key, "-noout", "-modulus"])
                                .stdout.decode().strip().split("=", 1)[1])
        if len(modulus) != 256:
            parser.error("Expected a 2048-bit RSA key")
        bct[528:784] = modulus[::-1]
        bct[7032:7288] = sign(args.key, loader, work)[::-1]
        (work / "base.bct").write_bytes(bct)
        (work / "loader.bin").write_bytes(loader)
        (work / "padded.bin").write_bytes(loader)
        result = subprocess.run([str(args.mkbctpart.resolve()), "-b", str(work / "base.bct"),
                                 "-B", str(work / "loader.bin"), "-p", str(work / "padded.bin"),
                                 str(work / "generated.bin")], capture_output=True)
        # This archived mkbctpart can return 255 after writing its output.
        # Validate the produced descriptor and signatures, never status alone.
        generated = work / "generated.bin"
        if not generated.exists() or generated.stat().st_size < 8192:
            raise RuntimeError("mkbctpart did not produce a BCT: " + result.stderr.decode(errors="replace"))
        final_loader = (work / "padded.bin").read_bytes()
        if final_loader != loader:
            raise ValueError("mkbctpart changed the already aligned loader")
        bct = bytearray(generated.read_bytes()[:8192])
        # T124 BootLoader[0] descriptor begins at 0x1b4c. Length is +12.
        length = struct.unpack_from("<I", bct, 0x1b58)[0]
        if length != len(loader):
            raise ValueError("BCT loader descriptor size mismatch: " + str(length))
        if struct.unpack_from("<II", bct, 0x1b5c) != (0x80108000, 0x80108000):
            raise ValueError("BCT loader address mismatch")
        bct[800:1056] = sign(args.key, bytes(bct[1712:8192]), work)[::-1]
        verify(public, loader, bytes(bct[7032:7288])[::-1], work)
        verify(public, bytes(bct[1712:8192]), bytes(bct[800:1056])[::-1], work)
        (work / "rcm.bct").write_bytes(bct)
        call([args.tegrarcm.resolve(), "--gen-signed-msgs", "--pkc=" + str(key_der),
              "--signed-msgs-file=" + str(work / "rcm"), "--bct=" + str(work / "rcm.bct"),
              "--bootloader=" + str(work / "loader.bin"), "--loadaddr=0x80108000", "--soc=124"])
        names = ("loader.bin", "rcm.bct", "rcm.qry", "rcm.ml", "rcm.bl")
        artifacts = {name: (work / name).read_bytes() for name in names}
        if any(not data for data in artifacts.values()):
            raise ValueError("An RCM artifact is empty")
        manifest = {"schema": 1, "entry": "jibo-ram-dfu-v1", "profile": args.profile,
                    "soc": 124, "load_address": "0x80108000",
                    "persistent_writes_on_entry": False, "hardware_verified": False,
                    "public_modulus_sha256": hashlib.sha256(modulus).hexdigest(),
                    "base_bct_sha256": hashlib.sha256(args.bct.read_bytes()).hexdigest(),
                    "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in artifacts.items()}}
        args.out.mkdir(parents=True, mode=0o700)
        for name, data in artifacts.items():
            (args.out / name).write_bytes(data)
        (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print("Created offline candidate bundle:", args.out)


if __name__ == "__main__":
    main()

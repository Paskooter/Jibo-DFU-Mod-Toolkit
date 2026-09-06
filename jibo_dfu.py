#!/usr/bin/env python3
"""Jibo T124 DFU entry. Runtime consumes public, pre-signed artifacts only."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RCM = ("0955", "7740")
DFU = ("0955", "701a")
MARKER = "jibo-dfu-v1"
FILES = ("loader.bin", "rcm.bct", "rcm.qry", "rcm.ml", "rcm.bl")


class DfuError(Exception):
    pass


def devices(root=Path("/sys/bus/usb/devices")):
    found = []
    for path in sorted(root.glob("*")):
        try:
            pair = tuple((path / name).read_text().strip().lower()
                         for name in ("idVendor", "idProduct"))
        except OSError:
            continue
        if pair in (RCM, DFU):
            found.append({"port": path.name, "state": "rcm" if pair == RCM else "dfu"})
    return found


def select_device(found, port=None):
    selected = [d for d in found if port is None or d["port"] == port]
    if len(selected) > 1:
        raise DfuError("Multiple robots connected; select one using --port.")
    return selected[0] if selected else None


def load_bundle(directory):
    directory = Path(directory).resolve()
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["schema"] != 1 or manifest["entry"] != "jibo-ram-dfu-v1":
            raise DfuError("Unsupported loader manifest.")
        if manifest["persistent_writes_on_entry"] is not False:
            raise DfuError("This tool requires a loader that preserves persistent storage.")
        if manifest["soc"] != 124 or manifest["load_address"] != "0x80108000":
            raise DfuError("Unsupported RCM target or load address.")
        if set(manifest["files"]) != set(FILES):
            raise DfuError("Bundle must contain precisely the required five artifact records.")
        for name in FILES:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise DfuError("Missing or symlinked bundle artifact: " + name)
            data = path.read_bytes()
            record = manifest["files"][name]
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise DfuError("Bundle integrity check failed: " + name)
        if (directory / "rcm.bct").stat().st_size != 8192:
            raise DfuError("T124 BCT must be 8192 bytes.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DfuError("Invalid bundle: " + str(exc)) from exc
    return manifest


def run(argv, timeout=30):
    try:
        env = os.environ.copy()
        if (ROOT / "lib").is_dir():
            env["LD_LIBRARY_PATH"] = str(ROOT / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DfuError(str(exc)) from exc
    if result.returncode:
        raise DfuError("Command failed: " + argv[0] + "\n" + result.stdout + result.stderr)
    return result.stdout + result.stderr


def tool(name, override=None):
    candidate = override or str(ROOT / "tools" / name)
    if Path(candidate).is_file():
        return str(Path(candidate).resolve())
    if override:
        raise DfuError("Tool does not exist: " + override)
    located = shutil.which(name)
    if not located:
        raise DfuError("Missing " + name + "; install it or provide --" + name + ".")
    return located


def dfu_alternatives(executable, port):
    output = run([executable, "-d", "0955:701a", "--path", port, "-l"])
    names = re.findall(r'name="([^"]+)"', output)
    return names, output


def enter(bundle, port, tegrarcm, dfu_util, timeout=30, allow_untested=False):
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect USB and hold recovery while resetting the robot.")
    port = selected["port"]
    if selected["state"] == "dfu":
        names, _ = dfu_alternatives(dfu_util, port)
        if not names:
            raise DfuError("DFU device has no accessible alternatives; check USB permissions.")
        return {"port": port, "state": "dfu", "already_running": True,
                "loader_verified": MARKER in names, "alternatives": names}
    manifest = load_bundle(bundle)
    if not manifest.get("hardware_verified", False) and not allow_untested:
        raise DfuError("This bundle is an untested hardware candidate. Use --allow-untested for its first hardware test.")
    root = Path(bundle).resolve()
    argv = [tegrarcm, "--usb-port-path=" + port, "--download-signed-msgs",
            "--signed-msgs-file=" + str(root / "rcm"), "--bct=" + str(root / "rcm.bct"),
            "--bootloader=" + str(root / "loader.bin"), "--loadaddr=0x80108000",
            "--usb-timeout=5000"]
    run(argv, timeout=max(timeout, 30))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = select_device(devices(), port)
        if current and current["state"] == "dfu":
            names, _ = dfu_alternatives(dfu_util, port)
            if MARKER not in names:
                raise DfuError("USB entered DFU but the expected loader marker is absent; no partition transfer was attempted.")
            return {"port": port, "state": "dfu", "already_running": False,
                    "loader_verified": True, "alternatives": names}
        time.sleep(0.25)
    raise DfuError("RCM transfer completed but DFU did not appear on port " + port +
                   ". Check USB passthrough and reset into RCM before retrying.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("detect", help="Show matching USB devices without sending commands")
    check = sub.add_parser("verify-bundle", help="Check local artifacts without touching USB")
    check.add_argument("bundle", type=Path)
    entry = sub.add_parser("enter", help="Load recovery into RAM and leave the robot in DFU")
    entry.add_argument("--bundle", type=Path, default=ROOT / "bundles" / "default")
    entry.add_argument("--port", help="Linux USB topology path, e.g. 1-2")
    entry.add_argument("--tegrarcm")
    entry.add_argument("--dfu-util")
    entry.add_argument("--timeout", type=float, default=30)
    entry.add_argument("--allow-untested", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "detect":
            result = devices()
        elif args.command == "verify-bundle":
            result = load_bundle(args.bundle)
        else:
            if not 0 < args.timeout <= 600:
                raise DfuError("Timeout must be between 0 and 600 seconds.")
            # Existing DFU does not require tegrarcm or any bundle.
            device = select_device(devices(), args.port)
            rcm = tool("tegrarcm", args.tegrarcm) if device and device["state"] == "rcm" else "tegrarcm"
            result = enter(args.bundle, args.port, rcm, tool("dfu-util", args.dfu_util),
                           args.timeout, args.allow_untested)
        print(json.dumps(result, indent=2))
        return 0
    except DfuError as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

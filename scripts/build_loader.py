#!/usr/bin/env python3
"""Build in a fresh directory using a local Jibo U-Boot tree and Buildroot host tools."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "common/main.c": "a777b4a553bea33c9d75211f91605c68b044074eb2c0b3d0a827a3c6d8c4d7f2",
    "include/configs/kein-baseboard.h": "a8fffb1950f8b01764e3ccfb0f95c5dbcc0dc9806fc553ed39a44ab71d0b3c42",
    "drivers/dfu/dfu_mmc.c": "105cf58ee218e30034b266538d2bce9739ad95bd2d061daaae3126d878168dee",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--host", required=True, type=Path, help="Buildroot output/host directory")
    parser.add_argument("--out", required=True, type=Path, help="New build directory")
    parser.add_argument("--cid-serial-candidate", action="store_true",
                        help="include experimental eMMC-CID USB serial identity support")
    parser.add_argument("--file-level-candidate", action="store_true",
                        help="include the experimental file-RPC mailbox and stable CID serial")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Build directory already exists")
    source, out, host = args.source.resolve(), args.out.resolve(), args.host.resolve()
    if source in out.parents or out in source.parents:
        parser.error("Source and output must not contain one another")
    contents = {name: (source / name).read_text() for name in EXPECTED}
    # The locally available tree has an earlier diagnostic patch. Remove it
    # only in the new build copy, then require the known baseline hashes.
    name = "include/configs/kein-baseboard.h"
    value = contents[name]
    if "/* Recovery audit only:" in value:
        start = value.index("/* Recovery audit only:")
        end = value.index('#include "tegra-common-usb-gadget.h"', start)
        contents[name] = value[:start] + value[end:]
    for name, value in contents.items():
        if hashlib.sha256(value.encode()).hexdigest() != EXPECTED[name]:
            parser.error("Unsupported U-Boot source fingerprint: " + name)
    shutil.copytree(source, out, ignore=shutil.ignore_patterns(".git"))
    for name, value in contents.items():
        (out / name).write_text(value)
    board = out / "arch/arm/mach-tegra/board2.c"
    value = board.read_text()
    if "#ifdef CONFIG_JIBO_RAM_AUDIT" in value:
        start = value.index("#ifdef CONFIG_JIBO_RAM_AUDIT")
        end = value.index("#endif", start) + len("#endif\n")
        board.write_text(value[:start] + value[end:])
    subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / "firmware/entry.patch")], cwd=out, check=True)
    if args.cid_serial_candidate or args.file_level_candidate:
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i",
                        str(ROOT / "firmware/cid-serial.patch")], cwd=out, check=True)
    if args.file_level_candidate:
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i",
                        str(ROOT / "firmware/file-level.patch")], cwd=out, check=True)
    shutil.copyfile(ROOT / "firmware/jibo_dfu_entry.h", out / "common/jibo_dfu_entry.h")
    env = os.environ.copy()
    env.update(PATH=str(host / "usr/bin") + os.pathsep + env["PATH"],
               LD_LIBRARY_PATH=str(host / "usr/lib"), CCACHE_DISABLE="1",
               CCACHE_DIR=str(out / ".ccache"), SOURCE_DATE_EPOCH="1788566400")
    if args.file_level_candidate:
        config = out / ".config"
        if not config.is_file():
            parser.error("File-level candidate requires the pinned source .config")
        contents = config.read_text()
        if "CONFIG_JIBO_DFU_FILE_RPC=y" not in contents:
            contents = contents.replace(
                "# CONFIG_JIBO_DFU_FILE_RPC is not set\n", "")
            config.write_text(contents.rstrip() + "\nCONFIG_JIBO_DFU_FILE_RPC=y\n")
        subprocess.run(["make", "ARCH=arm",
                        "CROSS_COMPILE=arm-buildroot-linux-gnueabihf-",
                        "HOSTCFLAGS=-O2 -I" + str(host / "usr/include"),
                        "HOSTLDFLAGS=-L" + str(host / "usr/lib"),
                        "olddefconfig"], cwd=out, env=env, check=True)
        if "CONFIG_JIBO_DFU_FILE_RPC=y" not in config.read_text():
            parser.error("File-RPC symbol is unavailable in the selected U-Boot config")
    subprocess.run(["make", "ARCH=arm", "CROSS_COMPILE=arm-buildroot-linux-gnueabihf-",
                    "HOSTCFLAGS=-O2 -I" + str(host / "usr/include"),
                    "HOSTLDFLAGS=-L" + str(host / "usr/lib"), "-j4", "u-boot-dtb-tegra.bin"],
                   cwd=out, env=env, check=True)
    config = (out / "include/autoconf.mk").read_text()
    if "CONFIG_ENV_IS_NOWHERE=y" not in config or "CONFIG_ENV_IS_IN_MMC=y" in config:
        raise RuntimeError("Unexpected persistent environment configuration")
    if args.file_level_candidate:
        kconfig = (out / "include/config/auto.conf").read_text()
        if "CONFIG_JIBO_DFU_FILE_RPC=y" not in kconfig:
            raise RuntimeError("Candidate build omitted CONFIG_JIBO_DFU_FILE_RPC")
        if not (out / "fs/ext4/jibo_file_rpc.o").is_file():
            raise RuntimeError("Candidate build omitted the file-RPC object")
    print("Built candidate:", out / "u-boot-dtb-tegra.bin")


if __name__ == "__main__":
    main()

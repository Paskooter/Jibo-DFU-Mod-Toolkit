# Reproducing the RAM DFU loader

The included file-level loader is built from the source snapshot in `vendor/jibo-ram-dfu-v1-source.tar.gz` plus this repository's `firmware/entry.patch`, `firmware/cid-serial.patch`, `firmware/file-level.patch`, and `firmware/jibo_dfu_entry.h`. The validated build uses the Buildroot 2015.11 ARM host toolchain, GCC 4.9.3.

The matching host toolchain used for validation was at `/home/super/jibo-audit/ram-uboot-build/output/host`. That path belongs to a separate local build tree and is not included in this repository or the source archive. Provide your own matching `output/host` directory when reproducing the loader.

From the toolkit repository root:

```sh
mkdir -p /tmp/jibo-loader-source /tmp/jibo-loader-build
tar -xzf vendor/jibo-ram-dfu-v1-source.tar.gz -C /tmp/jibo-loader-source
python3 scripts/build_loader.py \
  --source /tmp/jibo-loader-source/jibo-ram-dfu-v1-source/u-boot \
  --host /home/super/jibo-audit/ram-uboot-build/output/host \
  --out /tmp/jibo-loader-build/ram-dfu-loader \
  --file-level-candidate
```

The included padded image is 432,000 bytes with SHA-256 `6c44d0a5371f734e727083e759f713c40f168dc0f68a332b51d32c5d17a6f263`. Its checked manifest is `assets/manifest.json`.

Verify the padded output with:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib

raw = Path('/tmp/jibo-loader-build/ram-dfu-loader/u-boot-dtb-tegra.bin').read_bytes()
padded = raw + bytes((-len(raw)) % 16)
assert len(padded) == 432000
assert hashlib.sha256(padded).hexdigest() == '6c44d0a5371f734e727083e759f713c40f168dc0f68a332b51d32c5d17a6f263'
print('Pinned loader reproduced')
PY
```

The script enables the file RPC and eMMC-CID USB serial, then checks that the file RPC object was compiled. Its protocol and supported ext4 layouts are described in [the file-level protocol](../firmware/file-level-protocol.md).

The ShofEL entry helper checks the loader's exact size and SHA-256 before transferring it. Build the matching helper with:

```sh
python3 scripts/build_file_level_entry.py --loader assets/loader.bin \
  --out .build/file-level-entry
```

The builder checks the image against its manifest, starts from the pinned ShofEL source commit, applies the stage-2 patch, replaces the stage-2 size and hash, and runs the ShofEL tests. A matching local build is reused. This loader entered DFU on Moth and passed a direct mode-file write and metadata readback; Wi-Fi file writes still need a hardware check.

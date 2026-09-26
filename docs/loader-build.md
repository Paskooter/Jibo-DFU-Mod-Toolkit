# Reproducing the RAM DFU loader

The included, hardware-tested `jibo-file-v1` loader is pinned in `assets/loader.bin` with its manifest. It was built from the source snapshot in `vendor/jibo-ram-dfu-v1-source.tar.gz` and the file-level patch in the commit that introduced it. The **current** `firmware/file-level.patch` extends that source to `jibo-file-v2`; building this branch produces an experimental v2 image, **not** a byte-identical reproduction of the bundled v1 image. The original build used the Buildroot 2015.11 ARM host toolchain, GCC 4.9.3.

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

The included v1 image is 432,000 bytes with SHA-256 `6c44d0a5371f734e727083e759f713c40f168dc0f68a332b51d32c5d17a6f263`. Its checked manifest is `assets/manifest.json`.

Verify the padded output with:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib

bundled = Path('assets/loader.bin').read_bytes()
assert len(bundled) == 432000
assert hashlib.sha256(bundled).hexdigest() == '6c44d0a5371f734e727083e759f713c40f168dc0f68a332b51d32c5d17a6f263'
print('Bundled v1 loader verified')
PY
```

The bundled loader enables the v1 file RPC and eMMC-CID USB serial. Its protocol and supported ext4 layouts are described in [the file-level protocol](../firmware/file-level-protocol.md). Stock Release 13.0.0 client scripts are too large and use legacy direct block pointers, so they require the separate, experimental v2 loader described below.

To compile the opt-in v2 file-level DFU candidate from the same source and toolchain, use a different output directory and add `--file-level-candidate` to the build command. The script checks that the file RPC object was compiled and writes a padded `experimental-file-rpc-loader.bin` plus SHA-256 `manifest.json` beside the output directory. It refuses to overwrite either artifact. The resulting image is separate from `assets/loader.bin` and is not included in the normal toolkit package.

For the default paths expected by the ShofEL candidate builder:

```sh
python3 scripts/build_loader.py \
  --source /tmp/jibo-loader-source/jibo-ram-dfu-v1-source/u-boot \
  --host /path/to/matching/output/host \
  --out .build/file-level-candidate/source \
  --file-level-candidate
```

The ShofEL entry helper checks the loader's exact size and SHA-256 before transferring it. Build the matching v2 helper with:

```sh
python3 scripts/build_file_level_entry.py \
  --loader .build/file-level-candidate/experimental-file-rpc-loader.bin \
  --out .build/file-level-candidate/shofel-entry
```

The builder checks the image against its manifest, starts from the pinned ShofEL source commit, applies the stage-2 patch, replaces the stage-2 size and hash, and runs the ShofEL tests. A matching local build is reused. The bundled v1 loader entered DFU on Moth and passed a direct mode-file write and metadata readback. The v2 candidate still needs hardware validation.

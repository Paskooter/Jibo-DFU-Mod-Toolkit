# Reproducing the RAM DFU loader

The included file-level loader is built from the source snapshot in `vendor/jibo-ram-dfu-v1-source.tar.gz` plus this repository's `firmware/entry.patch`, `firmware/cid-serial.patch`, `firmware/file-level.patch`, `firmware/dfu-queue.patch`, and `firmware/jibo_dfu_entry.h`. The validated build uses the Buildroot 2015.11 ARM host toolchain, GCC 4.9.3.

The matching host toolchain used for validation was at `/home/super/jibo-audit/ram-uboot-build/output/host`. That path belongs to a separate local build tree and is not included in this repository or the source archive. Provide your own matching `output/host` directory when reproducing the loader.

From the toolkit repository root:

```sh
mkdir -p /tmp/jibo-loader-source /tmp/jibo-loader-build
tar -xzf vendor/jibo-ram-dfu-v1-source.tar.gz -C /tmp/jibo-loader-source
python3 scripts/build_loader.py \
  --source /tmp/jibo-loader-source/jibo-ram-dfu-v1-source/u-boot \
  --host /home/super/jibo-audit/ram-uboot-build/output/host \
  --out /tmp/jibo-loader-build/ram-dfu-loader \
  --file-level-candidate --dfu-queue-candidate
```

The included padded image is 432,064 bytes with SHA-256
`7105edf6d7d9b68e32aef3f2c331a348e0612210cc9bd1c06bb26bba33b8e8c0`. Its checked
manifest is `assets/manifest.json`. This build accepts one-block extents and
legacy one-block direct-pointer files. Dirty filesystems remain unsupported for
fast file access; let Linux recover them before retrying, or choose the full-var path.

`--dfu-queue-candidate` applies `firmware/dfu-queue.patch` for faster partition transfers:

- Each SETUP clears stale ep0 completion bits in `ci_udc`. Without this, a host that queues
  requests could have a DFU_DNLOAD completed, and written, before its data arrived.
- The ep0 buffer and advertised DFU transfer size grow from 4 KiB to 32 KiB. DFU_UPLOAD
  returns the requested `wLength`, so dfu-util 0.9, which limits itself to 4 KiB on Linux,
  keeps working. Requests longer than the buffer are refused.
- The read-only `jibo-dfu-queue-v1` alternate tells the host that queued writes are safe.

Without `--dfu-queue-candidate` the script still reproduces the previous 432,000-byte image,
SHA-256 `fd5fc5b1759ddbdbb0da88ac89c425ab95eb0bc494917cfe27daf71e55a31095`.

Verify the padded output with:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib

raw = Path('/tmp/jibo-loader-build/ram-dfu-loader/u-boot-dtb-tegra.bin').read_bytes()
padded = raw + bytes((-len(raw)) % 16)
assert len(padded) == 432064
assert hashlib.sha256(padded).hexdigest() == '7105edf6d7d9b68e32aef3f2c331a348e0612210cc9bd1c06bb26bba33b8e8c0'
print('Queued-transfer loader reproduced')
PY
```

The script enables the file RPC and eMMC-CID USB serial, then checks that the file RPC object was compiled. Its protocol and supported ext4 layouts are described in [the file-level protocol](../firmware/file-level-protocol.md).

The ShofEL entry helper checks the loader's exact size and SHA-256 before transferring it. Build the matching helper with:

```sh
python3 scripts/build_file_level_entry.py --loader assets/loader.bin \
  --out .build/file-level-entry
```

The builder checks the image against its manifest, starts from the pinned ShofEL source commit, applies the stage-2 patch, replaces the stage-2 size and hash, and runs the ShofEL tests. A matching local build is reused. The previous loader entered DFU on Moth and passed a direct mode-file write and metadata readback. On 2026-09-28 this build entered DFU and passed pipelined reads at 4, 8, 16 and 32 KiB, queued 16 and 32 KiB var writes checked by readback, dfu-util 0.9 reads, the GPT probe and a file-RPC stat. File-RPC writes, Wi-Fi file writes and full updates have not yet run on it.

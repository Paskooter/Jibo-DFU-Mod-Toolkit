# Reproducing the pinned RAM DFU loader

The v1 loader is built from the source snapshot in `vendor/jibo-ram-dfu-v1-source.tar.gz` plus this repository's `firmware/entry.patch` and `firmware/jibo_dfu_entry.h`. The validated build uses the Buildroot 2015.11 ARM host toolchain, GCC 4.9.3.

The matching host toolchain used for validation was at `/home/super/jibo-audit/ram-uboot-build/output/host`. That path belongs to a separate local build tree and is not included in this repository or the source archive. Provide your own matching `output/host` directory when reproducing the loader.

From the toolkit repository root:

```sh
mkdir -p /tmp/jibo-loader-source /tmp/jibo-loader-build
tar -xzf vendor/jibo-ram-dfu-v1-source.tar.gz -C /tmp/jibo-loader-source
python3 scripts/build_loader.py \
  --source /tmp/jibo-loader-source/jibo-ram-dfu-v1-source/u-boot \
  --host /home/super/jibo-audit/ram-uboot-build/output/host \
  --out /tmp/jibo-loader-build/ram-dfu-loader
```

The build produced a 415,087-byte raw image with SHA-256 `9162897553bba41abfdce3c547f0e71b7f2ebff83ddc0b4e4592d4876c74c8e5`. The toolkit pads it with one zero byte to 415,088 bytes; the padded image SHA-256 is `8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689`, matching `assets/loader.bin`.

Verify the padded output with:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib

raw = Path('/tmp/jibo-loader-build/ram-dfu-loader/u-boot-dtb-tegra.bin').read_bytes()
padded = raw + bytes((-len(raw)) % 16)
assert len(raw) == 415087
assert hashlib.sha256(raw).hexdigest() == '9162897553bba41abfdce3c547f0e71b7f2ebff83ddc0b4e4592d4876c74c8e5'
assert len(padded) == 415088
assert hashlib.sha256(padded).hexdigest() == '8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689'
print('Pinned loader reproduced')
PY
```

# Jibo RAM DFU v1 corresponding source

This archive contains the clean U-Boot source snapshot, the Jibo DFU patch and build script corresponding to `assets/loader.bin`.

The pinned loader is 415,088 bytes with SHA-256 `8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689`. The raw U-Boot output is 415,087 bytes; the toolkit adds one zero byte to reach a 16-byte boundary before packaging.

## Reproduce the image

Use Python 3, GNU make, `patch`, and the matching Buildroot 2015.11 host toolchain with `arm-buildroot-linux-gnueabihf-gcc` 4.9.3. The build script pins `SOURCE_DATE_EPOCH=1788566400` and validates the three source fingerprints before compiling.

From this archive's root, create the output parent, then pass the Buildroot `output/host` directory and a new output directory:

```sh
mkdir -p build
python3 jibo-loader/scripts/build_loader.py --source u-boot --host /path/to/buildroot/output/host --out build/ram-dfu-loader
```

Verify the padded result:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib
image = Path('build/ram-dfu-loader/u-boot-dtb-tegra.bin').read_bytes()
image += bytes((-len(image)) % 16)
assert len(image) == 415088
assert hashlib.sha256(image).hexdigest() == '8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689'
print('Pinned loader reproduced')
PY
```

The same raw image can be passed to this toolkit's `scripts/package.py --loader`; that packager performs the padding and hash check.

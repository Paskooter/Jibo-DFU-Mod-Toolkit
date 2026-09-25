# Jibo DFU Mod Toolkit

A guided Linux terminal tool for Jibo recovery, backups, mode and Wi-Fi settings, and official update packages. The workflow has two USB states:

1. Put the robot in **RCM/APX** (`0955:7740`). The toolkit uses ShofEL to load its recovery program into RAM.
2. The robot reappears in **DFU** (`0955:701a`). All partition reads and writes use DFU. ShofEL is used only for the RCM-to-DFU entry step.

Run `sudo python3 dist/jibo-dfu-linux-x86_64.pyz` to open the guided menu if you have already built the local package. The menu enables entry only in RCM and partition actions only after the Jibo DFU loader appears. The `.pyz` is generated locally; GitHub contains the source and build instructions, not the compiled loader or package.

## Start from a clone

You need Linux x86_64, Python 3, `dfu-util`, `libusb-1.0`, GCC, Make, and an ARM bare-metal toolchain for ShofEL. Building the RAM loader also needs the Jibo U-Boot source and its matching Buildroot host toolchain. The toolkit does not include a production signing key and does not need one for ShofEL entry.

```sh
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit

git clone --branch improvements/IncreasedUSBReadWriteSpeed \
  https://github.com/devsparx/ShofEL2-for-T124.git ../ShofEL2-for-T124
cd ../ShofEL2-for-T124
git apply ../Jibo-DFU-Mod-Toolkit/patches/shofel2-dfu-entry.patch
make DFU_STAGE2_ENABLE_LAUNCH=1 all test
cd ../Jibo-DFU-Mod-Toolkit
```

Build the pinned RAM DFU loader from your local Jibo source tree. `--out` must name a new directory. The build script checks the source fingerprint before applying the toolkit's firmware patch.

```sh
python3 scripts/build_loader.py \
  --source /path/to/jibo/uboot \
  --host /path/to/buildroot/output/host \
  --out .build/loader-build
```

Package the loader, ShofEL entry files, DFU tool, and Python UI. The packager pads the raw U-Boot output and checks it against the loader pinned by this ShofEL build. It does not include signed RCM artifacts or a signing key.

```sh
python3 scripts/package.py \
  --loader .build/loader-build/u-boot-dtb-tegra.bin \
  --shofel2 ../ShofEL2-for-T124/shofel2_t124 \
  --intermezzo ../ShofEL2-for-T124/intermezzo.bin \
  --dfu-stage ../ShofEL2-for-T124/dfu_stage2.bin \
  --dfu-util "$(command -v dfu-util)" \
  --out dist/jibo-dfu-linux-x86_64.pyz
sudo python3 dist/jibo-dfu-linux-x86_64.pyz
```

If you already have the matching 415,088-byte `loader.bin`, pass it to `--loader` and skip the U-Boot build. To run from source instead of a `.pyz`, place that image at `./loader.bin`, place `shofel2_t124`, `intermezzo.bin`, and `dfu_stage2.bin` together in `./tools/`, then run `sudo python3 jibo_dfu.py`. These local files are ignored by Git. `sudo` is needed for USB access; the toolkit keeps backups under the invoking owner's `~/Jibo-Backups`.

## Using the menu

With the robot in RCM/APX, choose **Enter DFU with ShofEL** first. The current loader uses the Meerkat Rev02 RAM profile confirmed on Moth, so select it only for a robot whose hardware profile matches. Once DFU is active, the menu offers:

| Action | What it does |
| --- | --- |
| Check partition layout | Reads 32 KiB of the live eMMC GPT into memory and checks the Jibo partition sizes. No backup or eMMC write. |
| Back up var | Reads the 500 MiB `var` partition and saves a SHA-256 manifest. Reuses a verified existing backup for the same DFU device unless refreshed. |
| Inspect or edit a local var image | Views mode and Wi-Fi presence, or creates an edited copy without changing the robot. |
| Set robot mode | Chooses `normal`, `developer`, `int-developer`, or `oobe`; reads current `var`, saves one rollback backup, writes the edit, then reads it back. |
| Configure Wi-Fi | Adds a chosen network through the same backup, write, and readback workflow. |
| Write an edited var image | Writes an offline image after backup and confirmation, then verifies its readback. |
| Install an official update package | Selects a package from `./updates` and either preserves current `var` settings or replaces `var` for fresh setup. Checks live GPT sizes, backs up each partition before its first write, expands images to their final partition sizes offline, and verifies each write by readback. |

A successful 32 KiB layout check leaves the loader's `emmc-000` upload cursor advanced. Reset the robot into RCM/APX and re-enter DFU before another layout check or an update in that session. Other named partition alternatives, including `var`, are separate.

For scripts, the corresponding commands are:

```sh
sudo python3 dist/jibo-dfu-linux-x86_64.pyz enter-dfu-shofel --port 1-1 --confirm-meerkat-rev02
sudo python3 dist/jibo-dfu-linux-x86_64.pyz backup-var --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz inspect-var /path/to/var.img
sudo python3 dist/jibo-dfu-linux-x86_64.pyz list-updates
```

Use `--out /path/to/new/directory` with `backup-var` to force a fresh read for comparison. The default reuses one verified baseline; `--refresh` reads the current state and discards the new copy if it is byte-identical to the saved baseline. The update workflow similarly reuses one validated rollback backup per device and partition.

## Hardware results and remaining work

On Moth, ShofEL started the RAM loader and the robot entered DFU on the same USB port. A bounded 32 KiB read validated the GPT. A full DFU read of `var` took 116.9 seconds and produced exactly 524,288,000 bytes with SHA-256 `4a58631e0c6eb0559bef7d2827676a1bce7965886f2887afbdc65dedce18416b`. That matched Moth's September read-only ShofEL `var` backup byte for byte. The temporary comparison copy was removed; neither validation wrote eMMC. An older full eMMC dump from June has different `var` contents, so it is not the matching reference for this test.

A mode change to `int-developer` has also been confirmed on a connected Jibo. Full update installation and readback, automatic board-profile selection, a complete user-area eMMC backup, and other robot revisions still need hardware validation. The toolkit does not enable SSH.

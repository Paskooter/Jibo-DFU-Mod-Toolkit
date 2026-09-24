# Jibo DFU Mod Toolkit

This is a Linux tool for Jibo owners. Its normal interface is a guided terminal screen. You do **not** need to build or use a `.pyz` package: run the Python file directly.

## Start from GitHub

Clone the repository and enter its directory:

```sh
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit
```

The tool runs with Python 3.8 or newer. On Debian or Ubuntu, install Git, Python, and the host tools:

```sh
sudo apt update
sudo apt install git python3 dfu-util e2fsprogs
```

`e2fsprogs` supplies `debugfs`, `e2fsck`, `dumpe2fs`, and `resize2fs`, used for safe image edits and offline update preparation.

Start the guided menu:

```sh
python3 jibo_dfu.py
```

This opens the terminal interface; it is not a separate desktop window. Use **↑/↓** to choose an action, **Enter** to open it, **r** to refresh USB status, and **q** to quit. Actions that need DFU appear dimmed until the robot is ready; selecting one shows why it is unavailable. If Linux reports a USB permission error, run `sudo python3 jibo_dfu.py`. Backups still go to the home directory of the user who launched it.

## Get the robot ready

The screen leads through **connect → RCM/APX → DFU → choose an action**. RCM/APX is the robot's USB recovery entry state. The toolkit loads the matching recovery program into RAM to make DFU available; only DFU exposes the partition actions.

- In **DFU**, with this project's recovery loader showing its Jibo marker, the menu can read or edit `var` and install supported full-flash packages. `dfu-util` is required.
- In **RCM/APX**, DFU-based partition and update actions need recovery loaded first. Choose **Enter DFU from RCM/APX** to load it into RAM. A source clone does not include the signed recovery bundle or the `tegrarcm` host tool; the owner must provide the matching bundle in `bundles/default/` and make `tegrarcm` available. Do not use a bundle made for a different board profile.
- Alternatively, an explicit ShofEL `var` backup can read the live GPT and `var` sectors directly over USB while the robot remains in RCM/APX. It needs the ShofEL host, `intermezzo.bin`, and `emmc_server.bin`, but no signed recovery bundle or production key. The operation only invokes `EMMC_READ`; it does not write or erase eMMC.

The recovery bundle is omitted from GitHub because it is a hardware-profile-specific signed artifact. The repository does not contain a signing key. The `.pyz` file is also a generated local package and is not needed to run the menu from source.

If **Enter DFU** stops at `read RCM query version: USB transfer failure`, the robot is still in RCM/APX: the loader has not started, and no partition action occurred. Reset the robot into RCM/APX, reconnect its USB cable or WSL2 USB passthrough, and retry. Check that the device appears as NVIDIA APX (`0955:7740`) before entering DFU; successful entry changes it to `0955:701a`. If only one robot fails, check its signing profile against the bundle. ShofEL2 read-only diagnostics can work through a BootROM exploit even when a signed RCM bundle is incompatible; their success does not prove the bundle's key matches that robot. Jibo's archived [fuse guide](https://pvindex.org/confluence/display/ENG/Fused+vs.+Un-fused+robots) documents different signing profiles for fused robots.

## Use the terminal interface

The screen refreshes USB state each time it returns from an action. The first action is available only in RCM/APX. Live partition actions become available when the Jibo DFU loader is detected. Local image actions remain available without a robot. Every choice, path entry, hidden password, result, and write confirmation uses the same terminal screen. Use **Esc** to cancel a step; confirmation starts on **Cancel**, so select **Confirm** explicitly to write. A terminal is required for the guided interface; scripts can use the command-line subcommands.

| Action | What it does |
| --- | --- |
| **Enter DFU from RCM/APX** | Loads the matching recovery program into RAM; available only while the robot is in RCM/APX. |
| **Back up var** | Reads the 500 MiB partition and saves a private baseline on this computer. A verified baseline for that robot is reused. |
| **Back up var with ShofEL (read-only)** | Available in RCM/APX when ShofEL is installed; validates the GPT and reads the exact `var` extent over USB without writing eMMC. |
| **Set robot mode** | Prepares a mode change, displays the write plan, asks for confirmation on the terminal screen, then reads the partition back to verify it. |
| **Configure Wi-Fi** | Adds a network while preserving saved networks, asks for confirmation on the terminal screen, and verifies by reading back. Password entry is hidden. |
| **Install an official update package** | Lists packages in `updates/`, asks whether to preserve or replace `var`, saves one original backup per written partition, prepares exact-size images, writes and reads back each partition, then requests a reset. |
| **Write an edited var image** | Shows the write plan, asks for confirmation on the terminal screen, and verifies the partition readback. |
| **Inspect or edit a local backup** | Works offline. Inspection hides network details; editing creates a separate image. |

The read and write operations show a spinner and elapsed time. A successful mode or Wi-Fi write leaves the robot in DFU; a successful full-flash update requests a reset after verifying every partition.

For the ShofEL transport, place the built `shofel2_t124`, `intermezzo.bin`, and `emmc_server.bin` together in `tools/` or on `PATH`, or pass the host path explicitly. The host needs the USB-port and read-framing changes in [this patch](patches/shofel2-rcm-backup.patch), based on the upstream `improvements/IncreasedUSBReadWriteSpeed` branch. Build it from source with GCC, Make, and the `arm-none-eabi` toolchain:

```sh
git clone --branch improvements/IncreasedUSBReadWriteSpeed https://github.com/devsparx/ShofEL2-for-T124.git ../ShofEL2-for-T124
cd ../ShofEL2-for-T124
git apply ../Jibo-DFU-Mod-Toolkit/patches/shofel2-rcm-backup.patch
make shofel2_t124 intermezzo.bin emmc_server.bin
cd ../Jibo-DFU-Mod-Toolkit
```

Then run the backup with the exact USB port shown by `python3 jibo_dfu.py detect`:

```sh
sudo python3 jibo_dfu.py benchmark-rcm --shofel ../ShofEL2-for-T124/shofel2_t124 --port 1-1
sudo python3 jibo_dfu.py backup-var --transport shofel --shofel ../ShofEL2-for-T124/shofel2_t124 --port 1-1
```

The benchmark reads and discards an 8 MiB sample so you can check the USB transfer rate before a full backup. It does not leave a sample image on disk.

The same command works with a generated `.pyz` by replacing `python3 jibo_dfu.py` with the `.pyz` path; keep `--shofel` pointed at the external ShofEL executable. The toolkit runs it from the executable's directory so both adjacent payloads resolve correctly. To bundle ShofEL for the `.pyz` menu, pass `--shofel2 /path/to/shofel2_t124`, `--intermezzo /path/to/intermezzo.bin`, and `--emmc-server /path/to/emmc_server.bin` to `scripts/package.py`; all three files are stored under `tools/` and the menu discovers them there.

The selected RCM/APX USB port is passed to ShofEL explicitly. The toolkit validates the primary GPT CRC and Jibo partition layout before reading the 500 MiB `var` range. It stores the image and manifest under the same private backup root used by the DFU workflow. ShofEL backups use a hashed Tegra chip ID for reuse; raw chip IDs are not stored. The patched payload sends safe 4 KiB USB frames; the host collects up to 64 KiB per read and handles short USB transfers without losing byte order. The actual transfer rate depends on the robot and USB connection. Read operations show a byte-count progress bar. Generated `.pyz` packages include ShofEL only when all three optional build inputs are supplied; otherwise pass `--shofel` to the backup command.

## Install an official full-flash update

Create the package folder and put an official `jibo-pvt-flash-build*.tar.bz2` file in it. An extracted `flash_jibo/` package directory works too:

```sh
mkdir -p updates
cp /path/to/jibo-pvt-flash-build-5.4.2-production.tar.bz2 updates/
sudo python3 jibo_dfu.py
```

Choose **Install an official update package**, select the package with the arrow keys, and choose how to handle `var`. **Preserve var** keeps the robot's identity, current mode, Wi-Fi, and user configuration; if it currently says `oobe`, preserving it also keeps that setting. **Fresh var** writes the package's `var.ext4`, discarding those local settings and returning to the package's initial setup state. The tool saves a rollback copy of the original `var` before either kind of update. It saves one original backup for each other partition it writes and reuses a verified backup on later updates, so it does not create another full backup every time. Backups stay under `~/Jibo-Backups/`; large temporary prepared images and readbacks are removed after the operation. Allow ample free disk space and time for the multi-gigabyte transfers.

The tool reads the robot's GPT table over DFU and checks the exact partition sizes before preparing images. It expands the ext4 filesystems **on this computer** to those sizes, including the `skills` size reported by that robot. This avoids the manual post-flash resize required by older preserve-var scripts: Jibo's one-time resize marker is stored in `var`, so a preserved `var` may skip that first-boot resize. [Official GPT layout](https://pvindex.org/gitea/PlatformTeam/buildroot.jibo/src/branch/master/board/nvidia/avionic/gpt-table), [first-boot resize script](https://pvindex.org/gitea/PlatformTeam/buildroot.jibo/src/branch/master/board/nvidia/avionic/rootfs_overlay/var/etc/first_boot_resize).

**Recovery loader requirement:** The earlier recovery bundle omitted the roughly 11 GB `skills` partition, so the update action correctly stayed unavailable. The updated loader source exposes bounded `skills-000`, `skills-001`, and later alternatives strictly inside the live GPT `skills` partition. The host backs up and verifies them as one logical partition. A newly built, signed bundle with these alternatives is required; the GitHub source checkout does not include that bundle or its private signing key. The stock-update flasher instead sends a much smaller image to one `skills` alternative and relies on first-boot resizing.

For scripts, supply a package path and one explicit var policy:

```sh
sudo python3 jibo_dfu.py flash-update updates/jibo-pvt-flash-build-5.4.2-production.tar.bz2 --preserve-var --port 1-1 --confirm 'FLASH UPDATE'
```

Use `--fresh-var` instead to replace `var`, or `--dry-run` to validate a package and the live partition layout without writing. `python3 jibo_dfu.py list-updates` lists packages in `updates/`. These must be **full-flash** bundles containing raw `rootfs.ext4`, `services.ext4`, and `skills.ext4` images; smaller OTA tarballs use a different on-robot updater and cannot be sent directly over DFU. [Official flash procedure](https://pvindex.org/confluence/display/ENG/How+to+Flash+a+Robot+-+Simple), [OTA package format](https://pvindex.org/confluence/display/RM/Creating+OTA+Packages).

Full-flash installation and the offline expansion path have been checked with automated tests and local package images, but have **not yet been run on a robot**. An earlier signed recovery bundle entered DFU on one Jibo profile; the new skills-enabled bundle has been built and signed locally but has not been loaded on the robot. Other board or fuse/key combinations can require different recovery artifacts. The [official simple flashing guide](https://pvindex.org/confluence/display/ENG/How+to+Flash+a+Robot+-+Simple) covers PVT4 and directs other revisions to its advanced procedure. Do not flash a package until its hardware compatibility is known.

## Backups and safety

The first live write preparation saves one rollback image of `var` under `~/Jibo-Backups/`. Later operations reuse that verified baseline instead of making another persistent 500 MiB copy. Each operation temporarily reads the current partition so it can account for changes since the baseline; temporary images are removed after success, cancellation, or a failure before writing. Files from a failure after a write starts are kept for recovery. An explicit refresh is the only normal way to request another baseline.

The backup may contain robot identity, Wi-Fi, keys, and calibration data. Keep it private; the toolkit does not upload it. Inspection avoids printing saved network details, and Wi-Fi passwords are not displayed or written to operation logs.

RCM-to-DFU entry, a `var` read, and a mode change to `int-developer` were tested on one Jibo. The first mode edit reverted after boot because a pending ext4 journal replayed the older `oobe` file over the edit. The current editor replays and checks that journal on a temporary copy **before** changing `mode.json`; the owner reports that the corrected mode-setting option properly changed the robot to `int-developer`. The saved original backup is unchanged. The terminal menu uses an explicit Cancel/Confirm choice before a write; `--confirm` remains available for scripted commands.

## What is available and what is not

Available now: USB detection, recovery-bundle integrity checks, DFU entry for the tested profile, `var` backup and inspection over DFU or read-only ShofEL, offline mode/Wi-Fi editing, guarded `var` write/readback, and a full-flash package workflow with optional `var` preservation.

Still being built: SSH/firewall changes, full user-area eMMC backup, automatic board-profile selection, and support for additional Jibo hardware populations. The ShofEL backup path and the update workflow still need hardware validation; SSH is not enabled by this tool.

For the guided workflow, use `python3 jibo_dfu.py` and follow the terminal screen. Advanced command-line subcommands are available with `python3 jibo_dfu.py --help`, but are not needed for normal use.

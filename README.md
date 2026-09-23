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
- In **RCM/APX**, the robot has no partition access yet. Choose **Enter DFU from RCM/APX**, the first action. A source clone does not include the signed recovery bundle or the `tegrarcm` host tool. The owner must provide the matching bundle in `bundles/default/` and make `tegrarcm` available. Do not use a bundle made for a different board profile. If the robot is already in DFU, the missing bundle is not needed for the partition workflows.

The recovery bundle is omitted from GitHub because it is a hardware-profile-specific signed artifact. The repository does not contain a signing key. The `.pyz` file is also a generated local package and is not needed to run the menu from source.

## Use the terminal interface

The screen refreshes USB state each time it returns from an action. The first action is available only in RCM/APX. Live partition actions become available when the Jibo DFU loader is detected. Local image actions remain available without a robot. If the program is run without an interactive terminal, it falls back to numbered prompts.

| Action | What it does |
| --- | --- |
| **Enter DFU from RCM/APX** | Loads the matching recovery program into RAM; available only while the robot is in RCM/APX. |
| **Back up var** | Reads the 500 MiB partition and saves a private baseline on this computer. A verified baseline for that robot is reused. |
| **Set robot mode** | Prepares a mode change, displays the write plan, waits for `WRITE VAR`, then reads the partition back to verify it. |
| **Configure Wi-Fi** | Adds a network while preserving saved networks, waits for `WRITE VAR`, and verifies by reading back. Password entry is hidden. |
| **Install an official update package** | Lists packages in `updates/`, asks whether to preserve or replace `var`, saves one original backup per written partition, prepares exact-size images, writes and reads back each partition, then requests a reset. |
| **Write an edited var image** | Shows the write plan, waits for `WRITE VAR`, and verifies the partition readback. |
| **Inspect or edit a local backup** | Works offline. Inspection hides network details; editing creates a separate image. |

The read and write operations show a spinner and elapsed time. A successful mode or Wi-Fi write leaves the robot in DFU; a successful full-flash update requests a reset after verifying every partition.

## Install an official full-flash update

Create the package folder and put an official `jibo-pvt-flash-build*.tar.bz2` file in it. An extracted `flash_jibo/` package directory works too:

```sh
mkdir -p updates
cp /path/to/jibo-pvt-flash-build-5.4.2-production.tar.bz2 updates/
sudo python3 jibo_dfu.py
```

Choose **Install an official update package**, select the package number, and choose how to handle `var`. **Preserve var** keeps the robot's identity, current mode, Wi-Fi, and user configuration; if it currently says `oobe`, preserving it also keeps that setting. **Fresh var** writes the package's `var.ext4`, discarding those local settings and returning to the package's initial setup state. The tool saves a rollback copy of the original `var` before either kind of update. It saves one original backup for each other partition it writes and reuses a verified backup on later updates, so it does not create another full backup every time. Backups stay under `~/Jibo-Backups/`; large temporary prepared images and readbacks are removed after the operation. Allow ample free disk space and time for the multi-gigabyte transfers.

The tool reads the robot's GPT table over DFU and checks the exact partition sizes before preparing images. It expands the ext4 filesystems **on this computer** to those sizes, including the `skills` size reported by that robot. This avoids the manual post-flash resize required by older preserve-var scripts: Jibo's one-time resize marker is stored in `var`, so a preserved `var` may skip that first-boot resize. [Official GPT layout](https://pvindex.org/gitea/PlatformTeam/buildroot.jibo/src/branch/master/board/nvidia/avionic/gpt-table), [first-boot resize script](https://pvindex.org/gitea/PlatformTeam/buildroot.jibo/src/branch/master/board/nvidia/avionic/rootfs_overlay/var/etc/first_boot_resize).

For scripts, supply a package path and one explicit var policy:

```sh
sudo python3 jibo_dfu.py flash-update updates/jibo-pvt-flash-build-5.4.2-production.tar.bz2 --preserve-var --port 1-1 --confirm 'FLASH UPDATE'
```

Use `--fresh-var` instead to replace `var`, or `--dry-run` to validate a package and the live partition layout without writing. `python3 jibo_dfu.py list-updates` lists packages in `updates/`. These must be **full-flash** bundles containing raw `rootfs.ext4`, `services.ext4`, and `skills.ext4` images; smaller OTA tarballs use a different on-robot updater and cannot be sent directly over DFU. [Official flash procedure](https://pvindex.org/confluence/display/ENG/How+to+Flash+a+Robot+-+Simple), [OTA package format](https://pvindex.org/confluence/display/RM/Creating+OTA+Packages).

Full-flash installation and the offline expansion path have been checked with automated tests and local package images, but have **not yet been run on a robot**. The current signed recovery bundle has been verified on one Jibo profile; other board or fuse/key combinations can require different recovery artifacts. The [official simple flashing guide](https://pvindex.org/confluence/display/ENG/How+to+Flash+a+Robot+-+Simple) covers PVT4 and directs other revisions to its advanced procedure. Do not flash a package until its hardware compatibility is known.

## Backups and safety

The first live write preparation saves one rollback image of `var` under `~/Jibo-Backups/`. Later operations reuse that verified baseline instead of making another persistent 500 MiB copy. Each operation temporarily reads the current partition so it can account for changes since the baseline; temporary images are removed after success, cancellation, or a failure before writing. Files from a failure after a write starts are kept for recovery. An explicit refresh is the only normal way to request another baseline.

The backup may contain robot identity, Wi-Fi, keys, and calibration data. Keep it private; the toolkit does not upload it. Inspection avoids printing saved network details, and Wi-Fi passwords are not displayed or written to operation logs.

RCM-to-DFU entry, a `var` read, and one `var` write with immediate DFU readback were tested on one Jibo. The first mode edit reverted after boot because a pending ext4 journal replayed the older `oobe` file over the edit. The current editor replays and checks that journal on a temporary copy **before** changing `mode.json`; this corrected workflow is verified offline but still needs a live reboot test. The saved original backup is unchanged. The menu requires a typed `WRITE VAR` before a var write.

## What is available and what is not

Available now: USB detection, recovery-bundle integrity checks, DFU entry for the tested profile, `var` backup and inspection, offline mode/Wi-Fi editing, guarded `var` write/readback, and a full-flash package workflow with optional `var` preservation.

Still being built: SSH/firewall changes, full user-area eMMC backup, ShofEL transport, automatic board-profile selection, and support for additional Jibo hardware populations. Update flashing is not yet hardware-tested, and SSH is not enabled by this tool.

For the guided workflow, use `python3 jibo_dfu.py` and follow the terminal screen. Advanced command-line subcommands are available with `python3 jibo_dfu.py --help`, but are not needed for normal use.

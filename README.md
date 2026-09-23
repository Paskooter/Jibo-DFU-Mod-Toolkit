# Jibo DFU Mod Toolkit

This is a Linux tool for Jibo owners. Its normal interface is a guided numbered menu in the terminal. You do **not** need to build or use a `.pyz` package: run the Python file directly.

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

`e2fsprogs` supplies `debugfs` and `e2fsck`, used when inspecting or editing a backup.

Start the guided menu:

```sh
python3 jibo_dfu.py
```

This opens the menu in the terminal; it is not a separate desktop window. Try it before connecting a robot to see the options. If Linux later reports a USB permission error, run `sudo python3 jibo_dfu.py`. Backups still go to the home directory of the user who launched it.

## Get the robot ready

The menu shows whether USB sees the robot in `RCM/APX` or `DFU` mode.

- In **DFU**, with this project's recovery loader showing its Jibo marker, options 1–6 can read or edit the `var` partition. `dfu-util` is required.
- In **RCM/APX**, the robot has no partition access yet. Option 7 can load the RAM-only recovery program, but a source clone does not include the signed recovery bundle or the `tegrarcm` host tool. The owner must provide the matching bundle in `bundles/default/` and make `tegrarcm` available. Do not use a bundle made for a different board profile. If the robot is already in DFU, the missing bundle is not needed for options 1–6.

The recovery bundle is omitted from GitHub because it is a hardware-profile-specific signed artifact. The repository does not contain a signing key. The `.pyz` file is also a generated local package and is not needed to run the menu from source.

## Use the menu

Choose a number at `Choose an option:`. The menu refreshes the USB state each time it returns to the main screen.

| Option | What it does |
| --- | --- |
| **1 — Back up var** | Reads the 500 MiB partition and saves a private baseline on this computer. A verified baseline for that robot is reused. |
| **2 — Inspect a var backup** | Shows the saved mode and whether Wi-Fi is configured; it hides network details. |
| **3 — Set mode** | Prepares a mode change, displays the write plan, waits for `WRITE VAR`, then reads the partition back to verify it. |
| **4 — Configure Wi-Fi** | Adds a network while preserving saved networks, waits for `WRITE VAR`, and verifies by reading back. Password entry is hidden. |
| **5 — Edit a backup offline** | Makes a new local image for a mode or Wi-Fi change. The robot is not written to. |
| **6 — Write an edited var image** | Shows the write plan, waits for `WRITE VAR`, and verifies the partition readback. |
| **7 — Confirm DFU or enter recovery** | Confirms an already-running DFU loader, or enters DFU from RCM if the matching local recovery files are available. |
| **8 — Support status** | Explains what is available and what is still being built. |

The read and write operations show a spinner and elapsed time while transferring 500 MiB. A successful write leaves the robot in DFU; the tool does not reset it automatically.

## Backups and safety

The first live write preparation saves one rollback image of `var` under `~/Jibo-Backups/`. Later operations reuse that verified baseline instead of making another persistent 500 MiB copy. Each operation temporarily reads the current partition so it can account for changes since the baseline; temporary images are removed after success, cancellation, or a failure before writing. Files from a failure after a write starts are kept for recovery. An explicit refresh is the only normal way to request another baseline.

The backup may contain robot identity, Wi-Fi, keys, and calibration data. Keep it private; the toolkit does not upload it. Inspection avoids printing saved network details, and Wi-Fi passwords are not displayed or written to operation logs.

RCM-to-DFU entry, a `var` read, and one `var` write with immediate DFU readback were tested on one Jibo. The edited mode was visible in the immediate readback, but a later dump after boot showed `oobe` again. The cause is still under investigation; do not assume a verified DFU readback means a mode change will persist through boot. The menu requires a typed `WRITE VAR` before a partition write.

## What is available and what is not

Available now: USB detection, recovery-bundle integrity checks, DFU entry for the tested profile, `var` backup and inspection, offline mode/Wi-Fi editing, and guarded `var` write/readback workflows.

Still being built: SSH/firewall changes, full user-area eMMC backup, ShofEL transport, automatic board-profile selection, and support for additional Jibo hardware populations. The current tool writes only the `var` partition; it does not change other partitions or enable SSH.

For the guided workflow, use `python3 jibo_dfu.py` and follow the numbered prompts. Advanced command-line subcommands are available with `python3 jibo_dfu.py --help`, but are not needed for normal use.

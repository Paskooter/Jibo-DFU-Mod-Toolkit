# Jibo DFU Mod Toolkit

This toolkit helps an owner inspect and back up a Jibo's `var` partition, prepare a small set of changes, and write a changed `var` image back through USB DFU. It is still an engineering candidate. In the owner's current hardware session, RCM-to-DFU entry and a `var` read completed, but the mode change stopped at the read-only filesystem check before any partition write. Partition writes and readback are not yet hardware validated. The recovery bundle remains an untested candidate for the profile recorded in its manifest.

## Start here

On Linux, connect the robot by USB. If you have the packaged app, run the guided menu:

```sh
python3 dist/jibo-dfu-linux-x86_64.pyz
```

The menu explains the current USB state and walks through backup, inspection, mode changes, Wi-Fi setup, and verified write-back. A spinner and elapsed time appear while the 500 MiB partition is read or written. From a source checkout, use `python3 jibo_dfu.py` or `python3 jibo_dfu.py interactive`. If Linux reports USB permission errors, rerun with `sudo`; when launched through `sudo`, backup files are still stored in the invoking user's home directory and remain owned by that user.

The simplest safe first operation is a backup. The robot must already be running this project's recovery loader in DFU mode:

```sh
python3 jibo_dfu.py list
python3 jibo_dfu.py backup-var
```

The first backup is placed in a private directory under `~/Jibo-Backups/` and includes the raw 500 MiB image and a SHA-256 manifest. Repeating `backup-var` reuses the verified backup for that same robot instead of creating another copy. Use `python3 jibo_dfu.py backup-var --refresh` when you intentionally want to capture the robot's current state again. Backups are keyed to a hash of the USB serial when available, and otherwise to the USB port path. Treat the image as private: it can contain robot identity, network, key, and calibration data. The tool does not upload it anywhere.

## What it can do today

- Detect Jibo RCM/APX and DFU USB states, and identify the recovery loader marker.
- Check the local recovery bundle's hashes.
- Load the current RAM-only DFU candidate from RCM, with an explicit untested-hardware flag.
- Back up the 500 MiB `var` partition from the marked recovery loader.
- Inspect the saved mode and whether Wi-Fi is configured, without printing saved network details.
- Make an offline copy of a var image with an allowlisted mode change or an added Wi-Fi network.
- Back up, write, and read back a changed var image, requiring a typed `WRITE VAR` confirmation and leaving the robot in DFU afterward.

The offline editor needs `debugfs` and `e2fsck` from the Linux `e2fsprogs` package. It checks the filesystem, leaves the source image intact, and writes a new copy. If the check fails, the report includes the `e2fsck` exit status and diagnostic output; live mode and Wi-Fi operations also record that no partition write was attempted. Wi-Fi setup adds a network while preserving other saved networks, refuses an SSID that is already present, and never displays or records the password. Protected networks use a derived WPA key in the image; open networks require an explicit choice.

## Common commands

Inspect a backup and prepare a local edit:

```sh
python3 jibo_dfu.py inspect-var ~/Jibo-Backups/var-backup-*/var.img
python3 jibo_dfu.py edit-mode ~/Jibo-Backups/var-backup-123/var.img --mode developer
python3 jibo_dfu.py edit-wifi ~/Jibo-Backups/var-backup-123/var.img --ssid 'My Wi-Fi'
```

`edit-mode` and `edit-wifi` create a sibling `*-edited.img` file. The Wi-Fi command asks for its password using a hidden prompt. For an open network, add `--open-network`.

To perform a live change, use the matching command after the robot is in marked DFU mode:

```sh
python3 jibo_dfu.py set-mode --mode developer
python3 jibo_dfu.py configure-wifi --ssid 'My Wi-Fi'
```

The Wi-Fi password is requested without echo. Each live command reads the current var image into temporary work space, prepares one edited image, shows the write plan and image hash, then asks you to type `WRITE VAR`. The first live write preparation creates one rollback backup if none exists for that robot. Later writes reuse that baseline instead of saving another persistent 500 MiB copy. Each operation still reads the current image temporarily so it can account for changes since the backup; that temporary dump is removed after a verified success, cancellation, or failure before writing. A failure after a write starts keeps its work files for recovery. You can pass `--confirm 'WRITE VAR'` for scripted use. A write is accepted only for the expected 500 MiB image and the project's recovery marker. After writing, the toolkit uploads var again and checks its SHA-256 against the edited image. It does not reset the robot automatically.

To write an image prepared earlier, use:

```sh
python3 jibo_dfu.py write-var /path/to/var-edited.img
```

This also reuses the saved rollback backup, creating one only if needed. Successful operation directories keep a small manifest and remove the temporary full-size images; transfer or readback failures preserve the work files for recovery.

## Entering recovery

The tool cannot turn on a disconnected or powered-off robot. Use the robot's recovery/reset controls to make it appear as RCM/APX, then run:

```sh
python3 jibo_dfu.py enter --allow-untested
```

`--allow-untested` is required because this recovery candidate has not been tried on real hardware. The loader is designed to run from RAM and avoid persistent writes on entry, but that design claim still needs independent hardware validation before anyone should rely on it. If multiple robots are connected, select one with `--port`, for example `--port 1-2`.

For source use, the host needs Python 3.8+, `tegrarcm`, `dfu-util`, and (for image editing) `debugfs` and `e2fsck` from `e2fsprogs`. The single-file Linux x86_64 package under `dist/` bundles the Python application, host tools, and current local recovery bundle. It still needs compatible system libraries for libusb, libudev, Crypto++, libstdc++, and glibc.

## What is still unfinished

- Real-robot testing of RCM entry, var upload, data preservation, write, and readback.
- Verified bundles for additional Jibo signing populations and board revisions.
- Version-profiled SSH/firewall changes for both root filesystem slots.
- Full filesystem backup and the optional ShofEL raw eMMC backup path.
- Restore/recovery procedures and a clean public release with source and license notices for bundled components.

There is no SSH/firewall patch command or full eMMC dump command yet. The toolkit will not guess at firewall rules or label a set of named filesystem partitions as a complete device backup.

## Package and development

The package builder uses an explicit allowlist and refuses bundled private-key material. Bundle signing is a separate offline developer operation; end users do not need or receive the signing key.

```sh
python3 scripts/package.py --bundle bundles/default \
  --tegrarcm /path/to/tegrarcm --dfu-util /path/to/dfu-util \
  --libcryptopp /path/to/libcryptopp.so --out dist/jibo-dfu-linux-x86_64.pyz
```

The local bundle contains signed artifacts for one candidate profile and is ignored by Git. `PROJECT_SPEC.md` is a local planning document and must remain uncommitted. Never add real robot captures, backups, Wi-Fi configuration, identity data, private keys, or generated build files to source control. Do not push this repository to a remote without the owner's explicit instruction.

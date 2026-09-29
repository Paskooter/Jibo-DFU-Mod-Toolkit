# Jibo DFU Mod Toolkit

A guided Linux terminal tool for Jibo recovery, backups, mode and Wi-Fi settings, and official update packages. The workflow has two USB states:

1. Put the robot in **RCM/APX** (`0955:7740`). The toolkit uses ShofEL to load its recovery program into RAM.
2. The robot reappears in **DFU** (`0955:701a`). All partition reads and writes use DFU. ShofEL is used only for the RCM-to-DFU entry step.

The guided menu enables DFU entry when the robot is in RCM and partition actions after it appears in DFU. The launchers below open that menu. The `.pyz` package is generated locally and reused on later runs.

## Quick path: stock Jibo to int-developer

If you are here to mod a stock robot, use the guided menu to change its existing installation to `int-developer` mode. **If the robot has not joined your Wi-Fi yet, configure Wi-Fi in the toolkit too:** changing the mode alone does not give it a network connection.

1. Start the toolkit with `./run.sh` on Linux or `.\run.ps1` from PowerShell on a Windows PC with WSL 2. Connect one robot by USB and put it in RCM/APX mode.
2. Choose **Enter DFU with ShofEL (RAM loader)**. Wait for the menu to show the robot in DFU. The included Meerkat Rev02 entry selects RAM slot 2 or 3 from the hardware strap; slot 3 has only offline checks so far. See [hardware coverage](#hardware-results-and-remaining-work) for other revisions.
3. If the robot already has your Wi-Fi saved, choose **Set robot mode** and select **int-developer**. If it has never connected to your Wi-Fi, choose **Set mode and configure Wi-Fi**, select **int-developer**, then enter your Wi-Fi name and password twice. That action applies both settings together. If a saved Wi-Fi password is wrong, choose **Add or update a Wi-Fi network** and enter the same SSID with the corrected password.
4. Review and confirm the change. The toolkit saves or reuses a `var` rollback backup and checks the result. After a fresh RTM flash, the stock Wi-Fi and network startup files can be too large for the quick editor; if the menu offers a full `var` transfer, choose it and leave the robot connected until readback finishes. When setting Wi-Fi on RTM2, that transfer also applies the later TI radio startup settings. The flash itself does not make this Wi-Fi change.
5. When the toolkit reports the change verified, restart the robot normally.

## Linux quick start

On an x86_64 Linux system (including WSL 2), clone the repository and run the launcher:

```sh
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit
./run.sh
```

The launcher checks the local `dist/jibo-dfu-linux-x86_64.pyz` before opening it. If it is missing, stale, or contains a ShofEL helper that cannot start DFU, the launcher asks for `sudo` authorization in the same terminal, installs missing build packages on supported distributions, builds the launch-enabled ShofEL helper, replaces the `.pyz`, and opens the menu. You do not need to install ShofEL separately. You can also start it with `sudo ./run.sh`. USB access requires administrator privileges. Backups are saved under the invoking owner's `~/Jibo-Backups`.

When `./run.sh` is started inside WSL, it also tries to use Windows PowerShell and usbipd-win to attach a connected Jibo and reattach it when it changes from APX to DFU. If Windows interop or usbipd-win is unavailable, attach it manually. Use `JIBO_MANUAL_USB=1 ./run.sh` to skip automatic USB handoff.

The first build needs an internet connection for ShofEL and package installation. The launcher's default loader is the included `assets/loader.bin`; `JIBO_LOADER=/path/to/loader.bin ./run.sh` selects a different local copy of the same pinned image. The U-Boot source and notices are in `vendor/jibo-ram-dfu-v1-source.tar.gz`; the file access changes are in `firmware/file-level.patch` and `firmware/cid-serial.patch`, and the faster transfer changes are in `firmware/dfu-queue.patch`. An existing package can be run directly with `sudo python3 dist/jibo-dfu-linux-x86_64.pyz`.

## Windows with WSL

Use an x64 Windows PC with WSL 2 and Ubuntu. For a first-time setup, open **PowerShell as Administrator** and install WSL with Ubuntu 22.04 LTS:

```powershell
wsl --install -d Ubuntu-22.04
```

Restart Windows if prompted, then open **Ubuntu 22.04** once to create its Linux username and password. In PowerShell, update WSL and check that Ubuntu shows `VERSION 2`:

```powershell
wsl --update
wsl --list --verbose
```

If Ubuntu shows `VERSION 1`, run `wsl --set-version Ubuntu-22.04 2`. Install [usbipd-win](https://github.com/dorssel/usbipd-win) from PowerShell with Windows Package Manager, then reopen PowerShell so `usbipd` is on your path:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
```

If `winget` is unavailable, install the `.msi` from the [usbipd-win releases](https://github.com/dorssel/usbipd-win/releases). [Microsoft's WSL instructions](https://learn.microsoft.com/windows/wsl/install), [Ubuntu's WSL guide](https://ubuntu.com/wsl/docs/latest/howto/install-ubuntu-wsl2/), and [Microsoft's USB connection guide](https://learn.microsoft.com/windows/wsl/connect-usb) cover the setup and USB prerequisites.

Open PowerShell in the downloaded or cloned toolkit folder and run:

```powershell
.\run.ps1 -Distro Ubuntu-22.04
```

The launcher selects that WSL 2 distribution, shares and attaches a connected Jibo USB device, then runs `run.sh` inside WSL. The robot can change from APX (`0955:7740`) to DFU (`0955:701a`); the launcher watches both USB identities while the menu is open. Sharing a new USB identity may show a Windows administrator prompt. Run `.\run.ps1 -Distro Ubuntu-22.04 -ManualUsb` if you prefer to attach from another PowerShell window with `usbipd bind --busid <BUSID>` (administrator) and `usbipd attach --wsl --busid <BUSID>`. The Linux `./run.sh` also works directly inside WSL with manual USB attachment. If PowerShell's script policy blocks the launcher, use `powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 -Distro Ubuntu-22.04` for that invocation.

macOS support is deferred.

## Manual build and scripting

The launcher builds the package automatically; these are the equivalent manual steps when you need to inspect or customize the build. You need Python 3, `dfu-util`, `libusb-1.0`, GCC, Make, and an ARM bare-metal toolchain. The toolkit does not include or need a production signing key for ShofEL entry.

```sh
python3 scripts/build_file_level_entry.py \
  --loader assets/loader.bin --out .build/file-level-entry
```

This helper checks the loader manifest, clones the pinned ShofEL revision, applies the matching entry patch, and tests the resulting host tool and RAM stage. A matching local build is reused.

Package the included loader, ShofEL entry files, DFU tool, and Python UI. The packager checks the loader against its pinned size and SHA-256 and rejects a ShofEL host built without DFU launch support. It does not include signed RCM artifacts or a signing key.

```sh
python3 scripts/package.py \
  --loader assets/loader.bin \
  --shofel2 .build/file-level-entry/shofel2_t124 \
  --intermezzo .build/file-level-entry/intermezzo.bin \
  --dfu-stage .build/file-level-entry/dfu_stage2.bin \
  --dfu-util "$(command -v dfu-util)" \
  --out dist/jibo-dfu-linux-x86_64.pyz
sudo python3 dist/jibo-dfu-linux-x86_64.pyz
```

To rebuild the loader itself, extract `vendor/jibo-ram-dfu-v1-source.tar.gz` and follow [the loader build notes](docs/loader-build.md) with `--file-level-candidate`. That separate build needs the matching Buildroot host toolchain. To run the Python source directly instead of a `.pyz`, place the matching image at `./loader.bin`, place `shofel2_t124`, `intermezzo.bin`, and `dfu_stage2.bin` together in `./tools/`, then run `sudo python3 jibo_dfu.py`.

## Using the menu

See [RTM images and factory calibration](docs/rtm-images-and-calibration.md)
for the archived RTM packages and the factory's calibration steps.

With the robot in RCM/APX, choose **Enter DFU with ShofEL** first. The menu refreshes the robot state and available actions automatically when USB changes; `r` also refreshes manually. The current loader uses Meerkat Rev02 RAM slots 2 and 3; only slot 2 has been confirmed on hardware. Select it for a robot whose hardware profile matches. Once DFU is active, the menu offers:

| Action | What it does |
| --- | --- |
| Set robot mode | Chooses `normal`, `developer`, `int-developer`, or `oobe`; saves or reuses one rollback backup, edits only `/jibo/mode.json`, and checks the file and its permissions. |
| Add or update a Wi-Fi network | Saves or reuses the `var` backup, adds the network or updates a matching SSID in `/var/etc/wpa_supplicant.conf`, and checks the result. Protected network passwords are entered twice. On RTM2 it also adjusts TI radio startup when needed. |
| Set mode and configure Wi-Fi | Changes the settings in one reviewed file transaction when the fast writer supports the files. If it does not, one full `var` read, edit, write, and readback handles the settings and any needed RTM2 startup adjustment. |
| Install an official update package | Selects a package from `./updates` and preserves current `var`, replaces it for fresh setup, or creates fresh `var` with this robot's saved identity and camera calibration in OOBE mode. Checks live GPT sizes, saves or reuses one `var` rollback backup, and writes compact ext4 images. First boot grows the flashed filesystems to their partition sizes. Preserving `var` updates only its existing resize script so an old completion marker cannot skip that step. The normal path accepts DFU's completed transfer; `flash-update --verify-readback` checks the transferred image bytes. Other partitions are restored from the package and are not backed up first. |
| Save or check a var backup | Reads the 500 MiB `var` partition and saves a SHA-256 manifest. Reuses a verified existing backup for the same DFU device unless refreshed. |
| More tools and local images | Checks the partition layout; backs up or restores chosen partitions; writes an edited `var` image; or inspects and edits a local backup. Local image edits do not change the robot. |

The GPT check reads the complete `jibo-dfu-v1` alternate. Its final short transfer resets the loader's read cursor, so another check or an update can run without restarting DFU.

**Transfer speed:** Whole-partition reads and writes go through `jibo_dfu_pipeline.py`, which talks to Linux usbfs directly and keeps DFU requests queued instead of waiting a USB round trip for every block. The included loader uses 32 KiB blocks and accepts queued writes; it lists the read-only `jibo-dfu-queue-v1` alternate to say so. With an older loader, reads are still queued, but writes send one block at a time. Over WSL with usbipd-win, a 500 MiB `var` read took 52.0 s, against 115 s with dfu-util 0.9. A readback-checked `var` write took 76.4 s, against about 2 MiB/s with dfu-util. Set `JIBO_DFU_TRANSFER=dfu-util` to use dfu-util for these transfers. Small file-level requests always use dfu-util.

Compact preserve-`var` flashing currently accepts the known 13.0.0 first-boot script. It checks the selected package's script and rootfs boot hook before writing. Older scripts can format `rootfsB` during first boot, so an unrecognized script is rejected rather than rearmed. The file editor now handles the resize script's legacy one-block mapping. If a different layout fails its safety checks, the toolkit reads the current 500 MiB `var`, edits a temporary copy, writes it back after the compact package images, and checks the new script and permissions before reset. The temporary image is removed when the operation ends; the existing rollback backup is reused.

The included loader supports bounded changes to existing files. It reads the target file once before the change, then checks the file and its ownership and permissions after writing. The first `var` change saves a rollback image; later `var` changes reuse any verified `var` image for the same robot, even if the live filesystem has changed. Other partitions are backed up only when selected. The file writer currently handles only existing regular files of at most 4 KiB in one allocated block, mapped by one extent or one legacy direct pointer. Stock RTM Wi-Fi configs may occupy four 1 KiB blocks because of comments, and stock network startup files occupy two. A full-`var` Wi-Fi edit removes comment-only and blank lines from these stock files while keeping their active settings. That can make later file edits fit in one block. When a mode or Wi-Fi file uses an unsupported layout, or `var` has an unclean ext4 journal, the menu offers the slower full-`var` edit as an explicit choice, with cancel selected by default. The journal check protects direct writes and remains in force after a robot boot until Linux cleanly unmounts `var` or the full transfer replays the journal on a local copy. Full image writes and update packages still use partition transfers. See [the file-level protocol](firmware/file-level-protocol.md). An earlier direct mode-file write passed content and metadata readback; the new direct-pointer path and a direct Wi-Fi write have not yet been checked on hardware.

**Selectable backup and restore:** In **More tools**, choose **Back up selected partitions** and mark individual GPT partitions, or select all. Existing verified copies for the same robot are reused. The resulting `backup-set.json` lists the saved images without making duplicate copies. **Restore selected partitions** lets you choose all or a subset from that set. It checks the robot identity, live GPT extents, image sizes and hashes before asking for confirmation, then reads every restored partition back to check it. These are the GPT partitions exposed for complete DFU transfer. The loader's raw `emmc-*` alternatives are read-only; this flow does not restore the raw GPT, gaps between partitions, or eMMC Boot0/Boot1. A byte-for-byte full-chip restore is not available.

Jibo's archived `PlatformTeam/system-manager` starts `wpa_supplicant` using `/var/etc/wpa_supplicant.conf`. The archived `jiborobot/jibo-wifi` saves networks with `SAVE_CONFIG`; its separate `/var/etc/networks.conf` stores optional static IP settings for those SSIDs, not a second credentials database. This menu adds a DHCP Wi-Fi network to the supplicant file or replaces a saved network with the same SSID, while preserving other saved networks. It does not configure static IP addresses or test association while the robot is in DFU. Interactive Wi-Fi password entry asks for the password twice; scripts using `--password-stdin` still provide one line.

Stock RTM2 starts the TI `wlan0` supplicant directly. The later RTM3 image disables TI radio power saving before starting it. When you set Wi-Fi, the toolkit applies those two startup commands if it finds the original RTM2 hook in `var`; it leaves the RTM3 hook alone. This startup difference is a plausible cause of Wi-Fi failing after a fresh RTM2 flash, but connection still needs a live robot test. For a WPA2/WPA3 mixed network that still does not connect after restarting, try a 2.4 GHz WPA2-Personal network with WPA3 and required protected management frames disabled to isolate router compatibility. The toolkit currently saves WPA-PSK credentials and uses the WPA2 side of a mixed network; WPA3-only operation has not been verified.

For advanced file inspection, `stat-partition-file /jibo/mode.json --partition var` reads the file's UID, GID, permissions, and inode. `set-mode` and `configure-wifi` use the quick file path when the active loader advertises it; `set-mode-file` and `configure-wifi-file` require that path explicitly.

For scripts, the corresponding commands are:

```sh
sudo python3 dist/jibo-dfu-linux-x86_64.pyz enter-dfu-shofel --port 1-1 --confirm-meerkat-rev02
sudo python3 dist/jibo-dfu-linux-x86_64.pyz backup-var --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz list-partitions --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz backup-partitions --all --port 1-1
sudo python3 dist/jibo-dfu-linux-x86_64.pyz restore-partitions /path/to/backup-set.json --partition var --port 1-1 --yes
sudo python3 dist/jibo-dfu-linux-x86_64.pyz inspect-var /path/to/var.img
sudo python3 dist/jibo-dfu-linux-x86_64.pyz list-updates
```

Use `--out /path/to/new/directory` with `backup-var` to force a fresh read for comparison. The default reuses one verified baseline for the same robot; `--refresh` reads the current state and discards the new copy if it is byte-identical to the saved baseline. Use `--partition NAME` more than once with `backup-partitions` or `restore-partitions` to pick several partitions. The update workflow saves or reuses a `var` rollback backup. Full readback of update images is optional with `--verify-readback`; it checks only the stock image prefixes that were sent, before first-boot expansion.

If an update stops mid-flash, leave the robot in DFU. `flash-update PACKAGE --preserve-var --resume-from /path/to/update-manifest.json --yes` reuses the existing `var` backup without dumping live `var` again. It skips a recorded completed compact transfer when its image hash matches; incomplete compact transfers are read back before deciding whether to repeat them. A record made by the older full-partition writer is reflashed from the compact stock images because its hashes refer to a different image layout. If the failure happened before any backup or partition write, rerun normally without `--resume-from`. Add `--verify-readback` to compare each new transfer before reset.

## Hardware results and remaining work

On Moth, ShofEL started the RAM loader and the robot entered DFU on the same USB port. Two consecutive 17 KiB GPT marker reads in one DFU session validated the partition layout. A full DFU read of `var` took 116.9 seconds and produced exactly 524,288,000 bytes with SHA-256 `4a58631e0c6eb0559bef7d2827676a1bce7965886f2887afbdc65dedce18416b`. That matched Moth's September read-only ShofEL `var` backup byte for byte. The temporary comparison copy was removed; neither validation wrote eMMC. An older full eMMC dump from June has different `var` contents, so it is not the matching reference for this test.

A mode change to `int-developer` has also been confirmed on a connected Jibo. The owner has verified the Windows/WSL USB helper through RCM-to-DFU reattachment, other USB state changes, unplug/replug, and power-off/power-on. On Aero, a compact 13.0.0 package transfer with preserved `var` completed and the robot booted. The first-boot resize tag was written after the script's `set -e` resize sequence. Live SSH inspection found rootfsA, rootfsB, and services ext4 block counts exactly equal to their GPT partition sizes; skills occupies all but 2,560 bytes of its 10,991,139,328-byte partition. `var` retained `int-developer` and its original 500 MiB size. Selectable GPT partition backup and restore are implemented but have not yet been checked end to end on hardware. A raw full eMMC user-area backup and restore are not implemented. The toolkit does not enable SSH.

The official 5.4.2 and 13.0.0 flash packages each include one `u-boot-flasher.bin` and one base `meerkat_rev02.bct` (plus its signed copy). Their base BCT files are byte-identical, but `bct_dump` shows **two distinct DDR3 configurations**, in RAM slots 2 and 3; two EMC timing fields differ. Jibo's `flash-dfu.sh` passes the BCT and flasher separately to `tegrarcm`, supplying those RAM configurations through the BCT. [Jibo's archived flashing instructions](https://pvindex.org/confluence/display/ENG/Building+a+single+step+flash+system+for+Jibo) also prescribe different build configurations for EVT and DVT2, with an explicit Meerkat Rev02 BCT selection for DVT2. One bundled flasher binary therefore does not establish that every board revision uses the same RAM entry path.

The toolkit's ShofEL entry now selects the official Meerkat Rev02 **RAM slot 2 or 3** profile from the robot's RAM strap and rejects other codes before initializing RAM. The prior slot 2 entry passed live DFU checks on Moth. The new two-slot build passes compilation and offline profile checks, but has not yet been run on a robot with slot 3. [Jibo's board history](https://pvindex.org/confluence/display/ENG/Boards,+Revisions+and+History) identifies shipping JB1014 mainboard revisions, while its [board ID table](https://pvindex.org/confluence/display/ENG/Mainboard+(JB1014)+Board+ID+Resistor+Definitions) describes separate board ID straps; neither source maps those revisions to Tegra `RAM_CODE`. The distribution of slots 2 and 3 among robots is therefore unknown. Earlier EVT hardware also had a different flashing configuration, so coverage of every robot still needs evidence and a live check on a slot 3 unit.

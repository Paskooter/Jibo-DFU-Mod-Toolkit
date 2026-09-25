# Jibo DFU Mod Toolkit

A guided Linux terminal tool for Jibo recovery, backups, mode and Wi-Fi settings, and official update packages. The workflow has two USB states:

1. Put the robot in **RCM/APX** (`0955:7740`). The toolkit uses ShofEL to load its recovery program into RAM.
2. The robot reappears in **DFU** (`0955:701a`). All partition reads and writes use DFU. ShofEL is used only for the RCM-to-DFU entry step.

The guided menu enables DFU entry when the robot is in RCM and partition actions after it appears in DFU. The launchers below open that menu. The `.pyz` package is generated locally and reused on later runs.

## Linux quick start

On an x86_64 Linux system (including WSL 2), clone the repository and run the launcher:

```sh
git clone https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit.git
cd Jibo-DFU-Mod-Toolkit
./run.sh
```

The launcher uses `dist/jibo-dfu-linux-x86_64.pyz` if it exists. Otherwise, it asks for `sudo` authorization in the same terminal, installs missing build packages on supported distributions, builds the pinned ShofEL USB entry helper, creates the `.pyz`, and opens the menu. You can also start it with `sudo ./run.sh`. USB access requires administrator privileges. Backups are saved under the invoking owner's `~/Jibo-Backups`.

When `./run.sh` is started inside WSL, it also tries to use Windows PowerShell and usbipd-win to attach a connected Jibo and reattach it when it changes from APX to DFU. If Windows interop or usbipd-win is unavailable, attach it manually. Use `JIBO_MANUAL_USB=1 ./run.sh` to skip automatic USB handoff.

The first build needs an internet connection for ShofEL and package installation. The launcher's default loader is the included `assets/loader.bin`; `JIBO_LOADER=/path/to/loader.bin ./run.sh` selects a different local copy of the same pinned image. Its complete corresponding U-Boot source, build instructions, patch, and notices are in `vendor/jibo-ram-dfu-v1-source.tar.gz`. An existing package can be run directly with `sudo python3 dist/jibo-dfu-linux-x86_64.pyz`.

## Windows with WSL

Use a Windows laptop with an **existing WSL 2 Linux distribution** and [usbipd-win](https://github.com/dorssel/usbipd-win) installed. The PowerShell launcher does not install WSL. Open PowerShell in the downloaded or cloned toolkit folder and run:

```powershell
.\run.ps1
```

It selects a WSL 2 distribution, shares and attaches a connected Jibo USB device, then runs `run.sh` inside WSL. The robot can change from APX (`0955:7740`) to DFU (`0955:701a`); the launcher watches both USB identities while the menu is open. Sharing a new USB identity may show a Windows administrator prompt. Run `.\run.ps1 -ManualUsb` if you prefer to attach from another PowerShell window with `usbipd bind --busid <BUSID>` (administrator) and `usbipd attach --wsl --busid <BUSID>`. The Linux `./run.sh` also works directly inside WSL with manual USB attachment. If PowerShell's script policy blocks the launcher, use `powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1` for that invocation.

macOS support is deferred.

## Manual build and scripting

The launcher builds the package automatically; these are the equivalent manual steps when you need to inspect or customize the build. You need Python 3, `dfu-util`, `libusb-1.0`, GCC, Make, and an ARM bare-metal toolchain. The toolkit does not include or need a production signing key for ShofEL entry.

```sh
git clone --branch improvements/IncreasedUSBReadWriteSpeed \
  https://github.com/devsparx/ShofEL2-for-T124.git ../ShofEL2-for-T124
cd ../ShofEL2-for-T124
git checkout --detach 31ac3a260c8a1501869aff6690b3b9ad4904ef58
git apply ../Jibo-DFU-Mod-Toolkit/patches/shofel2-dfu-entry.patch
make DFU_STAGE2_ENABLE_LAUNCH=1 all test
cd ../Jibo-DFU-Mod-Toolkit
```

Package the included loader, ShofEL entry files, DFU tool, and Python UI. The packager checks the loader against its pinned size and SHA-256. It does not include signed RCM artifacts or a signing key.

```sh
python3 scripts/package.py \
  --loader assets/loader.bin \
  --shofel2 ../ShofEL2-for-T124/shofel2_t124 \
  --intermezzo ../ShofEL2-for-T124/intermezzo.bin \
  --dfu-stage ../ShofEL2-for-T124/dfu_stage2.bin \
  --dfu-util "$(command -v dfu-util)" \
  --out dist/jibo-dfu-linux-x86_64.pyz
sudo python3 dist/jibo-dfu-linux-x86_64.pyz
```

To rebuild the loader itself, extract `vendor/jibo-ram-dfu-v1-source.tar.gz` and follow its `README.md`. That separate build needs the matching Buildroot host toolchain. To run the Python source directly instead of a `.pyz`, place the matching image at `./loader.bin`, place `shofel2_t124`, `intermezzo.bin`, and `dfu_stage2.bin` together in `./tools/`, then run `sudo python3 jibo_dfu.py`.

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

A mode change to `int-developer` has also been confirmed on a connected Jibo. The new Windows USB helper has been checked against a Jibo already attached in DFU; its automatic reattachment during an RCM-to-DFU transition has not yet been tested end to end. Full update installation and readback, automatic board-profile selection, a complete user-area eMMC backup, and other robot revisions still need hardware validation. The toolkit does not enable SSH.

# Release builds (maintainers)

## What is built

`.github/workflows/release.yml` builds four unsigned files:

- `Jibo-DFU-Windows-x64.exe`: one-file console bootstrapper with the complete
  prebuilt Linux payload. Windows x64 only; the GUI remains in WSLg. Existing
  WSL distributions need glibc 2.35+ (Ubuntu 22.04+); older/musl distributions are
  diagnosed before extraction and the launcher offers a compatible distro.
- `Jibo-DFU-Linux-x86_64.AppImage`: native Linux x86_64; glibc 2.35 or newer
  (Ubuntu 22.04+), an X11/XWayland desktop and `pkexec` or `sudo`.
- `Jibo-DFU-macOS-arm64.dmg`: native Apple silicon app.
- `Jibo-DFU-macOS-x86_64.dmg`: native Intel app. These are **two native DMGs**, not
  universal2. The current macOS 15 CI runners establish the tested OS baseline;
  older macOS versions are not claimed until tested.

Each native payload embeds Python, Tcl/Tk, the unchanged toolkit Python sources,
prebuilt launch-enabled ShofEL host, matching ARM payloads/RAM loader, dfu-util,
libusb and e2fsprogs (`debugfs`, `e2fsck`, `resize2fs`, `dumpe2fs`, `mke2fs`). No
compiler, Git, Homebrew, system Python or network is required to start the Linux or
Mac GUI. Downloading a firmware update is still an explicitly networked operation.

PyInstaller 6.16.0 is used because it bundles CPython, its native extension modules
and Tk on all three host platforms. **Onedir inside AppImage/DMG**, rather than
onefile inside another container, avoids a second runtime extraction. The Windows
bootstrapper is onefile. Original sources are bundled as data and prepended to the
module search path: the GUI's private `get_source()` copy of `jibo_tui` still works.
The frozen adapter dispatches the existing pipelined-transfer subprocess through
its embedded interpreter rather than silently falling back to dfu-util.
`run.sh`, `run-macos.sh`, the text menu and source operations are untouched.
`run.ps1` keeps its USB algorithm; one manual-help string now parses on both
Windows PowerShell 5.1 and PowerShell 7.

## Windows authorization (release EXE only)

The Windows EXE does **not** run source `run.sh`. Its former route was
`bootstrap.ps1 -> AppRun -> sudo jibo-dfu`, which asked for the WSL account's
Linux password and ran the GUI as root. The EXE now uses this separate path:

1. Select a WSL 2 distribution with a **non-root default user**. Extract/check the
   prebuilt payload and copy the two EXE-only shell helpers as that user.
2. Reuse `run.ps1 -UsbOnly` for Windows usbipd binding/attachment. Binding keeps its
   existing UAC consent. A failed/refused handoff stops before opening the GUI.
3. Ask for Windows UAC consent for `wsl.exe --distribution <distro> --user root
   --exec bash .../wsl-usb-permissions.sh <UID> <GID> once`. No Linux sudo/password
   is used. If UAC uses a different Windows administrator account which cannot
   access this user's distro, setup fails instead of switching distro/user.
4. After consent, a Windows-launched `wsl.exe --user root` watcher grants only
   Jibo APX/DFU nodes (`0955:7740`, `0955:701a`) to the invoking UID/GID with mode
   `0600`. It rediscovers nodes during USB mode changes; it needs neither systemd
   nor udev, makes no persistent udev/sudoers changes, and writes no user files.
5. Run the same frozen `jibo-dfu` directly through a non-root user wrapper, not
   through AppRun. The GUI, firmware-image work, backups and files retain the
   selected user's UID and HOME. Cleanup removes the session marker and stops
   both watchers; the root watcher also checks its normal-user GUI-wrapper PID.

This can produce two Windows approval prompts when a device is not yet bound:
usbipd's existing bind approval and the WSL USB-access approval. No administrator
prompt authorizes a robot write; the existing per-write reviews remain mandatory.
There are no apt/Tk/compiler installs in the EXE path because they are bundled.
Installing WSL/WSLg, a distribution or usbipd, when missing, retains the existing
explicit setup approval/UAC. Ubuntu's first-run **creation** of a Linux account
and password is still the distro's provisioning UI; the toolkit never requests
that password to authenticate a launch or USB operation. Existing distros need
no Linux password for this path. The source `run.ps1`/`run-gui.cmd` launchers still
use `run.sh` and its existing Linux sudo behavior; this change targets the EXE.

Native Linux/macOS source launchers, release AppRun and frozen native adapter are
byte-identical to release-builds base `03a25aa`. Windows helpers are embedded only
in the Windows EXE; Linux/macOS payload assembly is unchanged. Test coverage is
synthetic command-boundary coverage, not interactive UAC or real USB acceptance.

## Run a build without a release

On Actions → **Single-file release builds** → **Run workflow**, choose any branch.
The workflow must first be merged into the default branch for GitHub's manual-run
UI to expose it. During development, pushes to `feature/release-builds` also run it.
No tag or release is made by a branch/manual run.

Download the `Jibo-DFU-release-files` Actions artifact: it contains the four files,
`SHA256SUMS`, `LICENSE`, `THIRD-PARTY-NOTICES.md`, the complete corresponding-source
archive, per-architecture Mac interpreter-dependency source archives, and
per-platform JSON receipts (byte sizes, SHA-256, branch commit). The
separate platform artifacts are useful for diagnosis. `linux-stage` is an internal
Windows-build input, not a user download.

## Cut and publish a release

1. Review the branch, merge it into main and test the build on real Windows/Mac
   hosts and a Jibo. Read `docs/release-builds-report.md`; CI is not a USB acceptance
   test. Do not ship a known-broken platform.
2. Pick the version, then from your reviewed clean checkout:
   ```sh
   git tag -a v1.0.0 -m 'Jibo DFU Toolkit v1.0.0'
   git push origin v1.0.0
   ```
   Only maintainers perform these commands; the build agent must not create tags.
3. Wait for every build and the aggregate job to pass. The tag job creates a
   **DRAFT** release and uploads all files. It never publishes it. A rerun refuses
   to touch an already published release.
4. Download the draft files, run `sha256sum -c SHA256SUMS` (macOS:
   `shasum -a 256 -c SHA256SUMS`), inspect license/source attachments, and complete
   the hardware checklist below. Preserve the matching source archive for at
   least three years. Publish the draft manually in GitHub Releases.
5. Link the four platform binaries from jibo.io. Keep the matching source, notices
   and checksums available from the same release.

## Verification and remaining acceptance

CI executes bundled dfu-util and all ext4 tools, checks ShofEL's launch capability,
loads the bundled libusb and Tcl, checks file integrity and private GUI-flow loading,
and renders the actual first Tk window without USB access. Mac data hashes are
fixed before PyInstaller's standard Mach-O preparation: relocated native binaries
are instead checked by execution, dependency closure and the outer DMG SHA-256.
Nothing edits the sealed Mac app afterward. CI verifies that local ad-hoc seal
before packing and after mounting the DMG; this is verification, not an identity
signing or notarization step. Linux uses Xvfb and
extract-and-run to avoid a CI FUSE requirement. macOS mounts each produced DMG and
launches the app from it. Windows executes the final `.exe` version/self-check and
PowerShell parse/helper tests without installing WSL or prompting for UAC.

Before publishing, test these **on real hosts**:

- Windows fresh setup: approved/declined WSL installation, reboot/resume, Ubuntu
  account initialization, usbipd installer and UAC cancellation; existing distro
  selection; WSLg window; APX → DFU and unplug/replug reattachment.
- Linux desktop: polkit prompt/cancellation, terminal sudo fallback, FUSE and
  `--appimage-extract-and-run`, offline first launch as a non-root user.
- Both Macs: Finder launch after Gatekeeper override, native architecture, offline
  first launch without Homebrew/Python, USB enumeration and RCM → DFU entry.
- All platforms: existing per-write reviews, backups and readback checks with a
  robot; **CI never flashes a robot**.

## Extraction, sources and troubleshooting

Linux: `chmod +x Jibo-DFU-Linux-x86_64.AppImage`, then
`./Jibo-DFU-Linux-x86_64.AppImage --appimage-extract`. The embedded tree is in
`squashfs-root/`; use `AppRun` for normal USB-authorized launches. On systems without
FUSE, run the download with `--appimage-extract-and-run`.

Mac: open the DMG and inspect the app's `Contents/Resources/runtime` and
`Contents/Frameworks/runtime` (PyInstaller uses symlinks between these). Windows:
`Jibo-DFU-Windows-x64.exe --extract-payload payload.tar.gz` exports the WSL payload.
The LGPL libusb shared library can be replaced with a compatible modified build in
`runtime/lib`; replace/rebuild the manifest when deliberately relinking. The
integrity check is a corruption diagnostic, not a signature or restriction on
modification. Rebuild with `scripts/release/build.py` on the target CI architecture
and its declared build prerequisites; source URLs and SHA-256 pins live in that
script. No build process runs on the user's first launch.

Both native launchers accept `--version`, `--self-check` and the CI-only
`--gui-smoke` (read-only, fake USB boundary). The Windows wrapper accepts
`--version`, `--self-check` and `--extract-payload FILE`.

## Signing later

No signing identities, signing secrets, Authenticode, notarization or publishing
steps are configured. Builds are unsigned for distribution. PyInstaller performs
its standard local Mach-O preparation, including the ad-hoc metadata required by
Apple silicon; this is not Developer ID signing or notarization. Future signing
would plug in after freezing the Windows executable/Mac app and before DMG packing,
artifact hashing and smoke testing. Add it only after a separate owner decision.

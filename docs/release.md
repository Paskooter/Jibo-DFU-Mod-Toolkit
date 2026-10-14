# Release builds (maintainers)

## What is built

`.github/workflows/release.yml` builds four unsigned files:

- `Jibo-DFU-Windows-x64.exe`: one-file console bootstrapper with the complete
  prebuilt Linux payload. Windows x64 only; the GUI remains in WSLg. Automatic selection uses only a healthy
  `Jibo-DFU-Toolkit` or standard Ubuntu 22.04+ WSL 2 distribution; otherwise the
  launcher asks to create its own private Linux, without account/password prompts.
- `Jibo-DFU-Linux-x86_64.AppImage`: native Linux x86_64; glibc 2.35 or newer
  (Ubuntu 22.04+), an X11/XWayland desktop and `pkexec` or `sudo`.
- `Jibo-DFU-macOS-arm64.dmg`: native Apple silicon app.
- `Jibo-DFU-macOS-x86_64.dmg`: native Intel app. These are **two native DMGs**, not
  universal2. The current macOS 15 CI runners establish the tested OS baseline;
  older macOS versions are not claimed until tested.

Each native payload embeds Python, Tcl/Tk, the unchanged toolkit Python sources,
prebuilt launch-enabled ShofEL host, matching ARM payloads/RAM loader, dfu-util,
libusb, cryptography (Ed25519 verification) and e2fsprogs (`debugfs`, `e2fsck`, `resize2fs`, `dumpe2fs`, `mke2fs`). No
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

1. Reuse a healthy **Jibo-DFU-Toolkit** first. Otherwise, use a standard Ubuntu
   distro registered as `Ubuntu` or `Ubuntu-YY.MM`, with `/etc/os-release`
   `ID=ubuntu`, version 22.04+, WSL 2, x86_64, glibc 2.35+, WSLg and a non-root
   default user. Prefer the matching Windows WSL default, then the highest actual
   Ubuntu version. Print the selected name; there is no distro picker. Tool and
   custom distributions are never probed, offered or selected automatically.
   Only an explicit `Jibo-DFU-Windows-x64.exe -Distro NAME` can override the name
   restriction; WSL 2, architecture, glibc, WSLg and non-root checks still apply.
   Extract/check the frozen payload and copy the EXE-only USB helpers as that user.
2. Reuse `run.ps1 -UsbOnly` for Windows usbipd binding/attachment. Binding keeps its
   existing UAC consent. A failed/refused handoff stops before opening the GUI.
3. Grant Jibo USB access with `wsl.exe --distribution <distro> --user root --exec
   bash .../wsl-usb-permissions.sh <UID> <GID> once`, run as the current Windows
   user. WSL lets the owner of a distribution enter it as root without a Linux
   password, so no sudo, Linux password or extra Windows prompt is needed. The
   command deliberately does not use an elevated `wsl.exe`: an elevated WSL session
   can be separate from the user's own (it may not see their distributions or the
   attached USB device). A failed grant stops before the watcher or the GUI.
4. A `wsl.exe --user root` watcher, started the same way, grants only Jibo APX/DFU
   nodes (`0955:7740`, `0955:701a`) to the invoking UID/GID with mode `0600`. It
   rediscovers nodes during USB mode changes; it needs neither systemd nor udev,
   makes no persistent udev/sudoers changes, and writes no user files. `wsl.exe`
   parses its own options before `--exec` with a plain space split, so options are
   passed bare and distro names must match WSL's rule `[A-Za-z0-9._-]`; only the
   command after `--exec` is quoted.
5. Run the same frozen `jibo-dfu` directly through a non-root user wrapper, not
   through AppRun. The GUI, firmware-image work, backups and files retain the
   selected user's UID and HOME. Cleanup removes the session marker and stops
   both watchers; the root watcher also checks its normal-user GUI-wrapper PID.

The only Windows approval prompt in a normal launch is usbipd's existing bind
approval, the first time a robot's USB port is shared. No administrator prompt
authorizes a robot write; the existing per-write reviews remain mandatory.
When no matching Ubuntu exists, setup asks:

> Set up a private Linux for the toolkit? One-time download of about 345 MB.
> No Linux account or password needed. [y/N]

Declining performs no download or import. Approval downloads the official
[Ubuntu 22.04.5 x64 WSL image](https://releases.ubuntu.com/jammy/ubuntu-22.04.5-wsl-amd64.wsl),
checks both **360,684,292 bytes** and SHA-256
`4499c4fe257f2fc83145b429ce211a0a43fd590e70d6261ede616210947d9f8f`, then imports
`Jibo-DFU-Toolkit` under `%LOCALAPPDATA%\Jibo-DFU-Toolkit\wsl` as the current
Windows user, with WSL version 2. The pin comes from
[Microsoft's distribution manifest](https://raw.githubusercontent.com/microsoft/WSL/master/distributions/DistributionInfo.json).
A mismatch registers nothing. The downloaded image is deleted after setup.

The import is deliberately `wsl --import`, not `wsl --install --from-file`:
[Microsoft documents tar imports and default-user configuration](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro),
and [WSL's ImportDistribution implementation](https://github.com/microsoft/WSL/blob/master/src/windows/common/WslClient.cpp)
sets `LXSS_IMPORT_DISTRO_FLAGS_NO_OOBE`. The image's `/etc/wsl-distribution.conf`
contains an interactive `/usr/lib/wsl/wsl-setup` command; private provisioning
removes it as an additional safeguard. Provisioning creates `jibo` (UID/GID 1000),
locks its password, grants no sudo access, disables unnecessary systemd startup,
and sets `[user] default=jibo` in `/etc/wsl.conf`. Terminate/restart checks that
`id -un` returns `jibo`. A failed new provision is unregistered; existing distros
and existing private storage are never overwritten or removed automatically.

The pinned image already contains every system dependency the frozen payload
needs: **zero additional apt packages**, no Python/Tk/compiler installation and
no Linux account/password wizard. Once Windows prerequisites and Linux are set
up, first-window startup and self-check work offline. Firmware downloads still
require internet. Installing WSL/WSLg or usbipd, when missing, retains explicit
approval/UAC; importing/provisioning private Linux itself needs neither UAC nor a
Linux password. Source `run.ps1`/`run-gui.cmd` still use `run.sh` and its existing
Linux sudo behavior; these instructions concern the EXE.

Every EXE invocation starts with `Jibo DFU Toolkit (build <revision>)`, so the
first line identifies the download being tested.

### Command window visibility

Setup, download progress, consent questions and administrator approval stay
visible. The command window moves out of the way only after the GUI confirms that
its main window is on screen. A slow or failed GUI launch never triggers hiding.

- **Windows Console Host:** the command window is hidden. If hiding is unavailable,
  the launcher tries minimizing instead.
- **Windows Terminal:** the launcher minimizes rather than hides the window.
  Terminal's console handle is a proxy, not its displayed window; full hiding is
  not reliable. Minimizing may affect other tabs in that Terminal window too.
  Use `-ShowConsole` when launching from a shared terminal window.

For troubleshooting, run:

```powershell
.\Jibo-DFU-Windows-x64.exe -ShowConsole
.\Jibo-DFU-Windows-x64.exe -Distro Ubuntu-22.04 -ShowConsole
```

A failed GUI/WSL launch or stopped USB watcher restores the command window before
printing the error and asking **Press Enter to close**. Terminal failures also
show a separate error dialog, since restoring a proxy cannot guarantee that the
correct tab is visible. Later USB administrator prompts restore the window too;
readiness cannot hide it while approval is pending. If USB support fails while
the GUI is running, the launcher shows the error but lets you close the GUI when
safe rather than terminating a possible transfer. A normal GUI close removes the
session/readiness markers and stops both USB helpers. GUI exit codes are retained.

See Microsoft's [console handle documentation](https://learn.microsoft.com/en-us/windows/console/getconsolewindow)
and [Terminal show/hide discussion](https://github.com/microsoft/terminal/issues/15311)
for the host limitation. This behavior has automated mocked-Win32 and mapped-Tk
coverage; interactive host behavior still needs the real-Windows checks below.

To remove the private Linux, run `wsl --unregister Jibo-DFU-Toolkit` in PowerShell.
**This deletes its entire Linux filesystem**, including backups saved inside it.
Copy anything you need to Windows first. Other distributions are not removed.

Windows helpers are embedded only in the Windows EXE. Source Linux and macOS launchers retain their existing behavior.

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
   hosts and a Jibo. CI is not a USB acceptance test. Do not ship a known-broken platform.
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

- Windows fresh setup: approved/declined WSL installation, reboot/resume, private Linux
  import/default-user verification, usbipd installer and UAC cancellation; existing distro
  selection; WSLg window; APX → DFU and unplug/replug reattachment. With both Console
  Host and Windows Terminal set as the default terminal, confirm the banner appears
  first, setup stays visible, and the command window hides (Console Host) or minimizes
  (Terminal) only after the GUI opens. Check `-ShowConsole`, a failed GUI launch,
  a stopped USB helper, and another bind/UAC prompt after the GUI opens. Confirm
  visible errors and a clean close with no remaining helpers.
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
`--version`, `--self-check`, `--extract-payload FILE`, and normal-launch options
`-Distro NAME` and `-ShowConsole` (which may be combined).

## Signing later

No signing identities, signing secrets, Authenticode, notarization or publishing
steps are configured. Builds are unsigned for distribution. PyInstaller performs
its standard local Mach-O preparation, including the ad-hoc metadata required by
Apple silicon; this is not Developer ID signing or notarization. Future signing
would plug in after freezing the Windows executable/Mac app and before DMG packing,
artifact hashing and smoke testing. Add it only after a separate owner decision.

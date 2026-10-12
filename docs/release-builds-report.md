# Release-build verification report

## Revision and runs

- Branch: `feature/release-builds`, based on `origin/main` `711dae0`.
- Verified implementation / artifact revision: **`e4caf56530a469e9ba81878ec5a89b00ff9fceb1`**.
- [Single-file release builds — SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716).
- [Existing macOS source portability workflow — SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722708).
- This report records those exact artifacts. It is added in a later documentation-only commit; subsequent builds carry their own revision, byte sizes and hashes in `build-info-*.json`. Do not substitute another run's checksums for these files.

No main push, tag, release creation/publication, signing identity, signing secret,
repository-setting change or robot contact was performed. Branch runs upload
Actions artifacts only. The tag-only **draft** creation path is configured and
statically tested but was deliberately not exercised: maintainers alone cut tags.

## Platform results

| Target | CI result and job | Verified |
| --- | --- | --- |
| Linux x86_64 | [SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114697398639) | Downloaded AppImage executes `--version` / `--self-check`; Xvfb first-window smoke; bundled tools/libusb/Tcl and the frozen pipeline child execute. The separate [Linux payload build](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114696682437) also passes. |
| Windows x64 | [SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114697398636) | Final one-file `.exe` executes version/self-check and validates its embedded prebuilt Linux payload. PowerShell parser/helpers, argument preservation, native failures, compatible/old/musl distro checks, and complete missing-GUI/missing-distro refusal paths pass without admin actions. No real WSLg/USB session is claimed. |
| macOS Apple silicon arm64 | [SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114697398711) | Native tool execution, bundled libusb/Tcl, private GUI flow source loading, frozen pipeline child, Mach-O dependency audit, read-only local ad-hoc seal verification, and actual mounted-DMG version/self-check/first Tk window. |
| macOS Intel x86_64 | [SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114697399270) | Same checks on the Intel runner and its actual mounted DMG. |
| Aggregate files/checksums | [SUCCESS](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/job/114698391868) | Complete four-binary set, shared and both Mac corresponding-source archives, notices, per-platform receipts, and `SHA256SUMS`; no branch release is created. |

The Mac deliverable is **two native DMGs**, not universal2. They are unsigned for
distribution: PyInstaller's required local Mach-O/ad-hoc preparation is not
Developer ID signing or notarization. `codesign --verify` is read-only verification,
not a signing step. No signing credentials are requested or configured.

## Downloaded artifacts and measured sizes

The [combined Actions artifact](https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38214722716/artifacts/11731197099)
was downloaded and **all 13 entries in `SHA256SUMS` verified locally**. Sizes below
are the actual individual files, not the compressed Actions ZIP size.

| File | Bytes | MiB |
| --- | ---: | ---: |
| `Jibo-DFU-Windows-x64.exe` | 74,143,843 | 70.71 |
| `Jibo-DFU-Linux-x86_64.AppImage` | 62,282,232 | 59.40 |
| `Jibo-DFU-macOS-arm64.dmg` | 70,612,909 | 67.34 |
| `Jibo-DFU-macOS-x86_64.dmg` | 70,610,463 | 67.34 |
| `Jibo-DFU-corresponding-source.tar.gz` | 48,019,829 | 45.80 |
| `Jibo-DFU-macOS-arm64-corresponding-source.tar.gz` | 53,655,798 | 51.17 |
| `Jibo-DFU-macOS-x86_64-corresponding-source.tar.gz` | 53,654,457 | 51.17 |

Binary SHA-256 values:

```text
ffb749500a2912fecb766bb26d4b1a529db03b54d72adade5ae4e57228cb5520  Jibo-DFU-Linux-x86_64.AppImage
24da78a722d2b45001f3d18eb623ea9f93e177fb4672e9c614689cb6fedd02fb  Jibo-DFU-Windows-x64.exe
645d4eaf778915eb8877db8bf24996282224fa727e4da2db18d77e04e94ebee3  Jibo-DFU-macOS-arm64.dmg
f9b715aeefe12b14b182ae794ed937562023e1478342f87a29c531013781e11f  Jibo-DFU-macOS-x86_64.dmg
```

Platform Actions artifact names are `Jibo-DFU-Windows-x64`,
`Jibo-DFU-Linux-x86_64`, `Jibo-DFU-macOS-arm64`, and `Jibo-DFU-macOS-x86_64`.
The combined artifact is `Jibo-DFU-release-files`; `linux-stage` is an internal
build input, not a user-facing download.

## Local regression and artifact checks

- Baseline: `python3 -m pytest -q` → **346 passed, 17 skipped**, 47 subtests.
- Added release tests: **16 passed** (`tests/test_release_builds.py`).
- Updated source suite: **362 passed, 17 skipped**, 47 subtests.
- Real Tk source suite under the provided Tk environment and Xvfb:
  `JIBO_GUI_TEST_DISPLAY=1 xvfb-run -a python3 -m pytest -q` →
  **379 passed**, 47 subtests; no skipped window tests.
- The downloaded Linux AppImage was independently launched locally with
  `--appimage-extract-and-run --self-check` and under Xvfb with `--gui-smoke`.
  Result: revision matches, dfu-util **0.11**, all five ext4 tools **1.47.1**,
  ShofEL **`dfu-stage-launch=1`**, Tcl/Tk **8.6.12**, pipeline subprocess **true**,
  actual first window **1080 × 720**, USB access **false**.
- Both CI Mac apps report Tcl/Tk **9.0.4** and first-window **true**, USB access
  **false**. The build prefix is hidden before smoke testing; Mach-O references
  are bundle-relative or macOS system libraries, not Homebrew/build paths.
- All three corresponding-source archives were inspected for required source and
  license members: dfu-util, libusb, e2fsprogs, U-Boot/Jibo loader, ShofEL2,
  toolkit source, `LICENSE` and notices are present.
- `run.sh`, `run.ps1`, `run-macos.sh`, backend/GUI modules, the existing `.pyz`
  packager and source entry builder are unchanged from the base revision.

## Build issues resolved

- e2fsprogs installation tried to install host udev/systemd/cron files; release
  configuration now disables those host-service installs.
- Ubuntu's older ARM GCC rejected an unrelated ShofEL example. Release builds
  select only the actual distributed ARM payloads, without modifying source
  launcher behavior or changing payload source to accommodate packaging.
- Intel runner Python links conflicted with preinstalled files; the build-only
  Homebrew install uses its supported `--overwrite` option.
- Mach-O relocation changes binary bytes. Mac manifests now hash immutable data
  before freezing and never mutate a sealed app afterward; native tools are
  checked by execution, dependency audit and whole-DMG checksums.
- License-only `.framework` directory copies looked like executable bundles to
  macOS; notice paths are now data-only names with original-path mappings.
- Dependency source inputs can be raw certificate data, not only tar archives;
  raw/ZIP/tar inputs and their license texts are retained correctly.
- PowerShell test wrappers now propagate child exit status and isolate fixture
  state across script scopes. Windows CI explicitly gates every native command
  exit code, so later successful tests cannot hide a failed binary smoke test.

## Still needs real-host/hardware acceptance

Before publishing, use the checklist in [release.md](release.md):

- A real Windows 10/11 x64 host: SmartScreen override; approved/declined WSL and
  usbipd installation/UAC; reboot and Ubuntu account initialization; actual WSLg
  window; distro selection; APX `0955:7740` → DFU `0955:701a` and reconnect loop.
- Real Mac hosts of both architectures: Finder/Gatekeeper first-open path,
  offline startup without Homebrew/Python, IOKit/libusb permissions, and actual
  RCM-to-DFU entry and transfers. The CI DMG launch is not a quarantine/Finder test.
- Linux desktop as a normal user: real polkit cancellation/approval, sudo fallback,
  FUSE mount startup as well as extraction, offline startup and USB permissions.
- Real Jibo backups, write reviews, readback checks and transfer/reconnect behavior
  from these new packages. **No hardware flashing or device operation was tested
  by this release-build task.**
- Maintainer tag/draft creation, final review and manual publication. The workflow
  refuses to update a published release and never publishes a draft automatically.

The repository did not have a blanket license for the toolkit's original code.
The added `LICENSE` is an index preserving existing permissions, not an invented
new grant. Bundled third-party terms, complete requested corresponding sources,
source-offer/retention notice and rebuilding instructions accompany the binaries.

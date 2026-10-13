# WIN-UAC report — implementation verified locally; Windows CI rerun required

> **Superseded in part (owner hardware test of `ad4e33b`):** the elevated WSL
> USB-access step failed on Windows with exit 127. The launcher passed quoted
> options to `wsl.exe`, which parses its own options without quote handling, so the
> line ran as a shell command in the default distro. That step no longer uses UAC:
> root inside the user's own distro needs no elevation or Linux password. The
> launcher also offers a fresh Ubuntu 22.04 when the default distro is incompatible.
> See `docs/release.md` (Windows authorization). Items below that describe a
> "UAC-gated" WSL root step or two UAC prompts describe the superseded design.

## Disposition

- **VERIFIED:** The EXE bootstrap has a password-free WSL root USB-preparation path gated by Windows UAC, and launches the frozen GUI explicitly as the selected non-root Linux user. Synthetic tests execute the actual bootstrap/wrappers and reject refusal/failure paths.
- **VERIFIED:** The actual Windows EXE built and passed version/self-check in Actions at `726dfc4f06a60937c2e0b6784af4230c80474f2c`; Linux and both native Mac release jobs passed.
- **BLOCKED / UNKNOWN:** The Windows job then failed in a new argv-test oracle, not during EXE build/self-check. Its replacement is committed and passes locally, but was NOT run on Windows. The lane's single permitted push was used; root must review/push the local harness correction and rerun CI. Do not describe this lane as accepted or release-ready.

## Revisions and boundaries — VERIFIED

Worktree: `/home/shell/work/dfu-win-uac`; branch: `feature/windows-uac`.
Base: `03a25aa58c6fd15e71dc39212c217fee57c1e29d`.
Clean local verification revision: `d8625513d48b6946020478470b80f769796d90fd`.
Pushed / CI build revision: `726dfc4f06a60937c2e0b6784af4230c80474f2c`.
The only difference after the push is the test harness and progress documentation; application/runtime files are identical.
The report-closing commit is documentation-only; its SHA is supplied in the final lane summary rather than trying to embed its own circular hash here.

Lane commits before report closure (`git log --format='%H %s' 03a25aa..HEAD`):

| SHA | Subject | Remote |
| --- | --- | --- |
| `fb337c7400706df43ebeb529c9ec4e856f6a0b7b` | docs: record Windows UAC lane discovery and CI scope | Pushed |
| `a8d499a821ac23c94f75f5c8260a3073ba0b40f4` | fix(windows): use UAC-approved WSL root USB setup and non-root GUI | Pushed |
| `726dfc4f06a60937c2e0b6784af4230c80474f2c` | test(windows): falsify privilege boundaries and surface native failures | Pushed |
| `d8625513d48b6946020478470b80f769796d90fd` | test(windows): replace PowerShell argv oracle with raw CLR probe | Local only |

Exactly one push was made: `git push origin feature/windows-uac:feature/windows-uac`.
Read-back with `git ls-remote --heads origin refs/heads/feature/windows-uac` returned the exact CI revision above.
No other branch push, merge, tag, release, deploy, robot contact or secret/real-backup read was performed.
All credentials used for read-only GitHub API/log requests stayed in memory and were not printed or committed.

## Password origin and privileged-step inventory — VERIFIED by file reads

The suspected source `run.sh` was not the EXE path:

- Base `scripts/release/windows_launcher.py:37-38` invokes `bootstrap.ps1`; its current invocation is `windows_launcher.py:40-41`.
- Base `bootstrap.ps1:114` launched `AppRun`, **not** `run.sh`.
- Unchanged `scripts/release/AppRun:18-23` bypasses pkexec on WSL and executes `sudo --preserve-env=DISPLAY,XAUTHORITY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR .../jibo-dfu`. This was the existing-distro EXE's Linux-password authentication step, and elevated the whole GUI.
- Separate source Windows launcher `run.ps1:441` invokes `run.sh`; there, `run.sh:26-35` implements `need_sudo -> sudo -v` and `as_root`, and `run.sh:99-110` validates sudo and launches the Python package as root. That source-only path remains unchanged.

Every privileged step in the Windows **EXE** bootstrap:

| Step | Current source | Privilege / authorization |
| --- | --- | --- |
| Missing WSL/WSLg install and update | `bootstrap.ps1:62-66` | Existing `Invoke-ApprovedAdmin` / `Start-Process -Verb RunAs`; Windows UAC, explicit setup approval |
| Missing or incompatible Ubuntu distribution install | `bootstrap.ps1:77-80,107-110` | Existing Windows UAC; Ubuntu first-run account/password **creation** remains distro provisioning |
| Missing usbipd install through winget | `bootstrap.ps1:119-124` | Existing interactive Windows installer / UAC |
| usbipd bind when a matching Jibo is not shared | `run.ps1:162-200` | Existing validated BUSID, direct admin bind if already elevated, otherwise `-Verb RunAs` / UAC |
| Jibo USB node ownership/mode preparation | `bootstrap.ps1:27-35`; `wsl-usb-permissions.sh:5-24,36-43` | Windows-approved `wsl.exe --distribution <distro> --user root --exec bash ... UID GID once`; no Linux sudo/password |
| Reapply Jibo-only permissions across hotplug/mode changes | `bootstrap.ps1:34-35`; `wsl-usb-permissions.sh:44-57` | Windows-launched WSL root watcher, only after successful UAC setup |

`usbipd attach` / monitoring (`run.ps1:203-253`) do not request Linux root or a Linux password.
Payload mkdir/tar/cp, integrity check, session marker, GUI and cleanup run as the explicit normal Linux user (`bootstrap.ps1:126-146,158-161`; wrapper `wsl-user-launch.sh:4-9`).
No EXE apt/dnf/pacman/Tk/compiler installation or udev setup exists: those runtime components are bundled.
The source Windows path additionally has `run.sh`'s root package-manager installs (`run.sh:79-96,142-183`), sudo validation, and root GUI/menu launch. Those are not redirected or removed because the requested change is EXE-only and native Linux/macOS effects must stay unchanged.

## Implementation and bounded scope — VERIFIED

- Root-default distros are rejected before payload extraction or elevation (`bootstrap.ps1:126-130`).
- Existing USB bind/attach is reused; a nonzero one-shot handoff stops before GUI (`bootstrap.ps1:151-155`).
- Native Start-Process arguments are quoted rather than concatenating untrusted shell commands (`bootstrap.ps1:20-35`).
- Root helper accepts only numeric invoking IDs and hardcodes real sysfs/USB roots for its executable path (`wsl-usb-permissions.sh:36-45`). Fixture roots are available only by sourcing the grant function in tests.
- USB selection is exactly VID 0955 and PID 7740/701a; derived bus/device numbers are validated; non-character devices/symlinks are refused (`wsl-usb-permissions.sh:8-16`). Ownership becomes invoking UID:GID with mode 0600, never world-writable.
- A vanished hotplug node can be retried; a still-live denied chmod/chown returns an error (`wsl-usb-permissions.sh:18-22`).
- Root never writes user files. The normal-user wrapper writes/removes the session marker and launches the same frozen binary (`wsl-user-launch.sh:4-9`). The watcher checks wrapper PID ownership and bounds initial startup at 15 seconds (`wsl-usb-permissions.sh:27-33,48-56`).
- GUI stdout/stderr do not contaminate the numeric exit status; PowerShell's error preference is restored (`bootstrap.ps1:37-45`).
- Cleanup removes the normal-user session, waits for the root watcher to exit, and stops the usbipd watcher (`bootstrap.ps1:159-166`). Device-node ownership/mode are not restored on GUI exit; they last until the device node is recreated. No persistent udev, sudoers, stored password, NOPASSWD or root-owned backup rule is installed.
- Windows EXE-only helpers are embedded by the Windows build adapter, not the shared Linux/macOS payload assembly. EXE self-check rejects missing embedded helpers (`windows_launcher.py:20-23`).
- `run.ps1` changed only a manual-help string at line 76 to parse on both PowerShell generations; no USB algorithm was rewritten.

## Files changed — VERIFIED by git diff

- `run.ps1` — literal quoting of one manual USB instruction.
- `scripts/release/bootstrap.ps1` — Windows root USB setup, normal-user GUI, safe native arguments, cancellation/exit/cleanup handling.
- `scripts/release/build.py` — embed the two Windows-only shell helpers.
- `scripts/release/windows_launcher.py` — embedded-helper self-check inventory.
- `scripts/release/wsl-usb-permissions.sh` — new root-only Jibo node grant/watch helper.
- `scripts/release/wsl-user-launch.sh` — new normal-user GUI/session wrapper.
- `scripts/release/test-bootstrap.ps1` — parsing, helper/cancellation checks, raw CLR argv oracle, synthetic full-bootstrap cases.
- `tests/test_release_builds.py` — selection/failure/lifetime/ownership-environment/embedding tests and native-launcher baseline hashes.
- `docs/release.md` — exact EXE authorization flow and its limitations.
- `docs/windows-uac-PROGRESS.md` — committed progress/evidence.
- `docs/windows-uac-REPORT.md` — this report.

No GUI/TUI/backend Python modules, workflow files, `run-gui.cmd`, `run.sh` or native Mac launchers changed.
Ignored local scratch tools/logs are under `.build/`; no binary build output or credentials were committed.

## Tests — VERIFIED by execution

Final suite execution began with a clean worktree at `d8625513d48b6946020478470b80f769796d90fd`.

| Exact command (worktree root) | Result |
| --- | --- |
| `python3 -m unittest discover -s tests -p test_run_sh.py -v` (baseline) | 10 pass, 0 fail/error/skip |
| `TMPDIR=/dev/shm .build/uac-py311/bin/python -m pytest tests/test_release_builds.py -q` | 23 pass, 0 fail/skip |
| `TMPDIR=/dev/shm .build/uac-py311/bin/python -m pytest -q -rs` | 368 pass, 0 fail, 18 skip, 47 passing subtests; 11.50s |
| `PATH="$PWD/.build/uac-py311/bin:$PATH" TMPDIR=/dev/shm python3 -m unittest discover -s tests -v` | 273 pass, 0 fail/error/skip; 8.708s |
| `TMPDIR=/dev/shm .build/uac-py311/bin/python -m pytest tests/test_release_builds.py tests/test_run_sh.py tests/test_macos.py tests/test_gui_launchers.py -q` | 66 pass, 0 fail, 1 skip, 6 passing subtests |
| `.build/uac-tools/pwsh -NoProfile -File scripts/release/test-bootstrap.ps1` | Exit 0; 8 full-bootstrap scenarios pass plus parsing/helpers and a real raw-CLR argv roundtrip; all WSL/USB/admin boundaries mocked |
| `bash -n run.sh scripts/release/wsl-usb-permissions.sh scripts/release/wsl-user-launch.sh` | Exit 0 |
| `git diff --check` | Exit 0 |

Local Python was 3.11.15; PowerShell was 7.6.6. The corrected native argv probe uses already installed Mono locally and Add-Type on Windows.
Full pytest skips are 17 opt-in Tk/display window checks and 1 missing `gi` integration; no skip is presented as a pass.
The 8 synthetic bootstrap cases are missing WSLg refusal, missing distro refusal, GUI exit 41, root-default refusal, UAC cancellation, admin failure, USB handoff refusal, and immediate root-watcher failure.
Synthetic USB node tests replace the character-device boundary and record chmod/chown, never granting permissions to real hardware.
A separate temporary-HOME execution of the unchanged normal-user wrapper created a synthetic file with UID:GID 1000:1000 matching the caller, preserved exit 41, and removed its session marker; no real backup was read or produced.
Baseline/full-disk pytest timed out after 180 seconds / 38 tests. The isolated combined-edit test passed in 0.15s on tmpfs; complete suites then passed without source changes.
An initial active-Python unittest run had 3 import errors because pytest was absent; an isolated pytest-enabled Python 3.11 venv resolved that dependency, without changing the host Hermes environment.

## Falsification — VERIFIED, all mutations restored

1. Broke `wsl-usb-permissions.sh:11` by replacing `[[ $vendor == 0955 && ( $product == 7740 || $product == 701a ) ]]` with a product-only check. Command: `.build/uac-py311/bin/python -m pytest tests/test_release_builds.py::test_windows_usb_permission_helper_only_grants_jibo_nodes -q`. **1 failed**: unauthorized synthetic vendor 1234 at `/001/005` gained two extra recorded grant operations. Restored the vendor filter; final suite passes.
2. At revision `a8d499a`, broke `bootstrap.ps1:39` (now line 42) by changing the GUI's `--user $User` to `--user root`. Command: `.build/uac-tools/pwsh -NoProfile -File scripts/release/test-bootstrap.ps1`. **Exit 1**, named assertion `GUI identity, direct binary dispatch or exit propagation failed`. Restored normal user; harness passes.
3. Broke `bootstrap.ps1:23-25` by replacing native quoting with `return ($Arguments -join ' ')`. Original host-PowerShell argv oracle failed at its count assertion. After replacing that oracle with the raw CLR probe, repeated the same sabotage: **exit 1 at test-bootstrap.ps1:106**, native argument count 7 rather than 5. Restored quoting; corrected raw-argv oracle and all bootstrap cases pass.
4. Test-first RED controls also observed the missing helper/wrapper/session function, omitted helper embedding/self-check inventory, old top-level AppRun GUI path, stdout-contaminated exit result, terminating synthetic stderr, and silently swallowed live-node grant failure before their implementations were added. No deliberate mutation remains.

## GitHub Actions — VERIFIED by API and Windows job log

The release workflow's push filter names only `feature/release-builds`; editing `.github/workflows/release.yml` was outside lane ownership.
Its registered workflow ID was nevertheless active. POST `/repos/Paskooter/Jibo-DFU-Mod-Toolkit/actions/workflows/381823205/dispatches` with explicit `{"ref":"feature/windows-uac"}` returned **204**; read-back found the exact-head release run below. No workflow edit was needed.
Run/job enumerated counts were checked against API `total_count` values. Log redirects were followed without forwarding GitHub Authorization to the signed blob URL.

Release run: **38278897072**, completed **failure**, head `726dfc4f06a60937c2e0b6784af4230c80474f2c`.
URL: https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38278897072

| Job ID | Job | Final status / conclusion |
| --- | --- | --- |
| 114911053939 | linux-payload | completed / success |
| 114912011757 | build (ubuntu-22.04, Linux, x86_64) | completed / success |
| 114912011781 | build (windows-2022, Windows, x64) | completed / failure |
| 114912011869 | build (macos-15, macOS, arm64) | completed / success |
| 114912011977 | build (macos-15-intel, macOS, x86_64) | completed / success |
| 114913448229 | release (aggregate) | completed / skipped |

Push run: **38278854368**, completed **success**, same head.
URL: https://github.com/Paskooter/Jibo-DFU-Mod-Toolkit/actions/runs/38278854368

| Job ID | Job | Final status / conclusion |
| --- | --- | --- |
| 114910903043 | macos (macos-15-intel, x86_64) | completed / success |
| 114910903417 | macos (macos-15, arm64) | completed / success |

Windows log `.build/windows-job-114912011781.log` verifies that PyInstaller built `Jibo-DFU-Windows-x64.exe`, then executed both `--version` and `--self-check` successfully. Self-check returned:

- revision `726dfc4f06a60937c2e0b6784af4230c80474f2c`;
- embedded payload SHA-256 `0d9eec69fc286f64fcc79b7428606f56be03f8739c1310826f00041af5573ff5`;
- 1968 payload members and `wsl_setup_attempted: false`.

The next command, `powershell -NoProfile -File scripts/release/test-bootstrap.ps1`, failed at its original line 80: `Native argv roundtrip count failed`.
Windows had already passed script parsing/pure-helper checks and the two early setup-refusal child cases, but did **not** reach the new UAC-order/normal-user-GUI helper assertions or the six later full-bootstrap cases.
The failing oracle launched PowerShell `-File`, which adds another interpreter's argument behavior. Local-only commit `d862551` replaces it with a raw compiled CLR console argv consumer and labels empty output lines so redirects cannot discard them. It passes and was falsified locally; Windows execution of that replacement remains UNKNOWN.
API lists exactly 4 artifacts: `linux-stage`, Linux x86_64, macOS arm64, macOS x86_64. There is no uploaded Windows EXE or aggregate artifact because their upload/aggregate jobs did not run. No artifact checksum/download or full four-platform-set acceptance is claimed.

## INFERRED

- The count failure is likely the legacy Windows PowerShell `-File` argv boundary, rather than the Windows runtime bootstrap. The log proves where it failed but did not print actual Windows argv, so that exact transformation is not empirically isolated.
- Windows user selection and existing distro-root authority mean this introduces no new Linux-password trust boundary; this is the brief's stated WSL model. Microsoft's command documentation confirms explicit user selection: https://learn.microsoft.com/en-us/windows/wsl/basic-commands . No live Windows-root access was exercised here.
- Granting the invoking user access to the exact Jibo USB nodes should let unchanged libusb/dfu/ShofEL code operate without root GUI privileges. Synthetic selection tests and unchanged backend code support this, but real USB operation is not measured.

## UNKNOWN / root actions still required

1. Review the local-only harness correction, authorize/push it from root, and rerun the release workflow. This worker must not spend a second push. The Windows Add-Type/raw-argv branch and the later PowerShell 5.1 functional cases need actual execution.
2. Exercise real UAC consent/cancellation, distro ownership when a different Windows admin account is used, WSLg, real Jibo permissions, APX-to-DFU transitions and reattachment. No CI job attempts actual UAC, WSL setup or real hardware transfers.
3. Confirm real GUI backup ownership on a Windows/WSL host. The local wrapper's UID/HOME/file inheritance is verified with synthetic output, not a robot backup.
4. Fresh Ubuntu first-run account/password **creation** remains unchanged (`bootstrap.ps1:78-80,109-110`). Existing-distro launch/USB privilege operations no longer ask for Linux authentication. Source `run.ps1`/`run-gui.cmd` still use source `run.sh` and its Linux sudo behavior; they are not the EXE acceptance path.
5. There can be two Windows UAC prompts for an unbound device: the existing usbipd bind prompt and the new WSL USB-access prompt. Device permissions are not a robot-write approval; existing per-write reviews remain unchanged.

## Cross-lane / reusable observations

- Root/workflow ownership: the validation branch is not in the release push filter; registered workflow-dispatch API succeeds despite the workflow file being absent on main. This avoids an out-of-ownership workflow change, but should be documented if root later changes CI triggers.
- Root/skill ownership: use a raw compiled argv consumer, not another PowerShell `-File` script, as a native Windows quoting oracle; retain literal empty-argument markers in redirected output. Save this lesson in the frozen-release skill after root review. No global skill files were modified from this worktree-limited lane.
- Other lane code was not changed. Native launchers are pinned byte-for-byte to the base by a new regression test, and both macOS portability jobs passed.

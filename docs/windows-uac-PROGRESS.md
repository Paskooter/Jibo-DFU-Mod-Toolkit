# WIN-UAC progress

## Claim and scope
The Windows release executable can authorize only host USB setup via Windows UAC / `wsl.exe --user root`, then launch the frozen GUI as the distro's non-root default user without sudo or a Linux password. Linux/macOS launch semantics remain unchanged. No sudoers/password storage.

## VERIFIED — discovery / baseline
- Worktree `/home/shell/work/dfu-win-uac`, branch `feature/windows-uac`, base `03a25aa58c6fd15e71dc39212c217fee57c1e29d`; clean at discovery (2026-10-12 16:09 UTC; two-hour deadline 18:09 UTC).
- Actual EXE route: `windows_launcher.py:37-38` -> `bootstrap.ps1:114` -> release `AppRun:22-23` -> `sudo --preserve-env=... jibo-dfu`. It does NOT run `run.sh`; the suspected `sudo -v` belongs to the separate source launcher.
- Baseline: `python3 -m unittest discover -s tests -p test_run_sh.py -v`: 10 pass, 0 fail, 0 skip.
- Baseline release pytest command blocked: active Python has no pytest. `gh` and `pwsh` are not on PATH; alternatives being discovered.

## VERIFIED — ownership blocker
- `.github/workflows/release.yml:8` only triggers branch pushes for `feature/release-builds`, not our permitted `feature/windows-uac`.
- That workflow is outside lane ownership; it will NOT be edited. Investigate existing workflow-dispatch API availability before concluding whether the authorized branch push can run CI. Root must resolve any workflow ownership change.

## INFERRED — implementation seam
Use Windows-side root-only USB permission preparation/watch for the invoking UID/GID, narrowly restricted to Jibo USB VID:PID 0955:7740/701a. Bypass shared AppRun only in the EXE bootstrap by launching the same frozen binary directly as the normal user. Package/build tools are already bundled, so EXE needs no apt/Tk install. Keep source run.sh and other native launcher files unchanged.

## VERIFIED — test-first implementation work
- `gh` and `pwsh` are not on PATH. Local tools were installed only under ignored `.build/`: pytest in `uac-venv`, PowerShell 7.6.6 in `uac-tools`.
- Baseline `scripts/release/test-bootstrap.ps1` on PowerShell 7 exposed a pre-existing invalid backtick-u string in `run.ps1:76`. Replaced only that manual-instruction string with a literal format string; USB algorithms unchanged.
- Test-first additions observed missing USB helper, missing non-root wrapper/session liveness helper, omitted EXE helper embedding, omitted EXE self-check helper inventory, old full EXE AppRun route, and stdout-contaminated exit return, then implemented fixes.
- Current release tests: 22 pass, 0 fail, 0 skip (latest count to be re-run at final revision). PowerShell harness exercises 8 full bootstrap scenarios using synthetic WSL/UAC boundaries plus pure helper checks.

## VERIFIED — Windows implementation
- Root grants/watch are restricted to VID:PID 0955:7740/701a; node ownership is invoking UID:GID, mode 0600. No udev/sudoers changes or root-owned backups.
- EXE refuses root-default distributions, failed USB handoff, UAC cancellation/admin failure, and immediate watcher failure before GUI.
- GUI calls the frozen binary via a normal-user wrapper and preserves its native exit status even with stdout. Session cleanup/lifetime checks stop the root watcher.
- Five native launcher/adaptor files are pinned byte-for-byte against the base in a regression test. No GUI/TUI/backend files were edited.

## VERIFIED — regression execution
- `TMPDIR=/dev/shm .build/uac-py311/bin/python -m pytest -q -rs`: 367 pass, 0 fail, 18 skip, 47 passing subtests (Python 3.11.15). Skips: 17 opt-in Tk/display window tests and 1 missing `gi` integration.
- `PATH="$PWD/.build/uac-py311/bin:$PATH" TMPDIR=/dev/shm python3 -m unittest discover -s tests -v`: 273 pass, 0 fail/error/skip.
- PowerShell 7.6.6: parse/helpers, real host-process argv roundtrip (spaces, quotes, trailing slash, empty argument), and 8 mocked full-bootstrap scenarios pass.
- First full pytest run on disk hit a 180-second timeout after 38 tests. Isolated combined-edit test passed in 0.15s on tmpfs; both complete suites then passed. No backend/test changes were needed. Initial unittest attempt with the active Python lacked pytest and had 3 import errors; isolated pytest-enabled Python 3.11 venv resolved them.
- GitHub API lists release workflow ID 381823205 as active, but the workflow file on main returns HTTP 404. Branch push will trigger macOS portability; release workflow remains blocked by its branch filter/default-branch absence unless dispatch API succeeds.

## VERIFIED — final hardening / falsification
- Final full pytest: 368 pass, 0 fail, 18 skip, 47 passing subtests; isolated release tests now include the live-node permission-failure control.
- Synthetic GUI stderr now passes through without terminating or contaminating the numeric exit status; ErrorActionPreference is restored.
- Live-node chown/chmod failure returns nonzero; vanished hotplug nodes remain retryable.
- Falsification 1: removed `vendor == 0955` from USB helper line 11; `test_windows_usb_permission_helper_only_grants_jibo_nodes` failed on unauthorized synthetic VID 1234 node /001/005. Restored.
- Falsification 2: changed `Invoke-WslGui --user $User` to `--user root`; PowerShell's named GUI-identity assertion failed. Restored.
- Falsification 3: replaced native quoting with `$Arguments -join ' '`; real host argv roundtrip failed on count. Restored.
- After all restorations: full pytest, PowerShell harness, Bash syntax checks and `git diff --check` pass. Hardware and Windows PowerShell 5.1 remain untested locally.

## VERIFIED — permitted remote write / CI discovery
- Exactly one push: `git push origin feature/windows-uac:feature/windows-uac`; `git ls-remote` read-back matches `726dfc4f06a60937c2e0b6784af4230c80474f2c`.
- Push started macOS portability run 38278854368.
- Release branch filter could not be edited within ownership. The existing registered workflow-dispatch API **did** work despite main lacking the workflow file: POST `/actions/workflows/381823205/dispatches` with explicit ref `feature/windows-uac` returned 204. Read-back found release run **38278897072**, head SHA exactly matching the pushed revision. Thus no workflow ownership edit is needed for this validation run.
- Both runs are being monitored through GitHub API, no token printed, no tags/releases/main writes. Current release job linux-payload is in progress; Windows is not started yet.

## VERIFIED — CI Windows failure and local harness correction
- Windows job 114912011781 built the actual EXE, and its `--version` and `--self-check` passed. Self-check reported revision 726dfc4, 1968 payload files, no WSL setup attempted.
- The subsequent PowerShell 5.1 test failed at `test-bootstrap.ps1:80`: `Native argv roundtrip count failed`. Job logs saved under ignored `.build/windows-job-114912011781.log`.
- The roundtrip's child was another PowerShell `-File` invocation, not a raw native argv consumer. Replaced that oracle with a compiled C# console argv probe; Windows uses Add-Type, local Linux uses installed Mono. Prefix encoded argument lines so redirect handling cannot discard the empty argument's otherwise blank line.
- Corrected harness passes locally on PowerShell 7.6.6 / Mono, preserving all five values, then all 8 mocked top-level bootstrap scenarios pass. **Windows execution of this correction is UNKNOWN.** Runtime application code was not changed after the pushed revision.
- The brief allows only one push and it has been spent. Do not push again; commit this harness correction locally for root to review and rerun Windows CI. Initial count failure's exact Windows `-File` transformation is INFERRED, not isolated on a live Windows host.
- Corrected raw-argv oracle was falsified too: unquoted argument join produced 7 native arguments instead of the expected 5 and failed at `test-bootstrap.ps1:106`; quoting was restored and the harness passed.
- Synthetic ownership execution through the unchanged normal-user wrapper created a file in a temporary HOME with UID:GID 1000:1000 matching the caller, propagated exit 41 and removed the session. No real backup/hardware was used.
- Final API read: release run 38278897072 **completed failure**: linux-payload, Linux AppImage, macOS arm64 and macOS x86_64 succeeded; Windows failed; aggregate release job skipped. Separate macOS portability run 38278854368 completed success for both architectures. Four Actions artifacts exist (Linux stage/Linux/Mac arm64/Mac x86_64); Windows and aggregate artifacts do not exist.

## UNKNOWN
Real interactive UAC, WSLg, Linux device permissions, APX->DFU transitions and real hardware acceptance cannot be exercised on this Linux host. CI native Windows capability remains to be established.

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

## UNKNOWN
Real interactive UAC, WSLg, Linux device permissions, APX->DFU transitions and real hardware acceptance cannot be exercised on this Linux host. CI native Windows capability remains to be established.

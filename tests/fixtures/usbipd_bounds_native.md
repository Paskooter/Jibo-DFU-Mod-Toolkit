# Offline native controls (NOT executed on the Linux host)

Use Windows 11, **Windows PowerShell 5.1 / .NET Framework**, a private scratch
folder and the final exact helper bytes. Run each phase as a foreground child
under the separately reviewed harness's **newly created Job** with its own outer
20-second deadline. The harness must assign containment before releasing input,
retain the process/Job handles, terminate the Job on every result and verify
ActiveProcesses=0. Do not run removal mutants without this enclosing Job. Do not
kill anything based on the observation PID files below; those are not authority.
No actual WSL/USB/WinGet/service/elevation/clipboard calls are required or allowed.

Compile once per scratch folder using PS5.1 (no download/dependency install):

```powershell
Add-Type -TypeDefinition ([IO.File]::ReadAllText($FixtureSource)) `
  -OutputAssembly (Join-Path $Scratch 'usbipd.exe') -OutputType ConsoleApplication
$env:USBIPD_BOUNDS_DIRECTORY=$Scratch
$env:USBIPD_BOUNDS_MODE='healthy'
. $ExactHelper
$r=Test-UsbipdExecutable (Join-Path $Scratch 'usbipd.exe') -TimeoutSeconds 2
if (-not $r) { throw 'healthy PE refused' }
if (-not $script:UsbipdLastSupervision.ReadersReleased -or
    -not $script:UsbipdLastSupervision.ContainmentStopped) { throw 'cleanup incomplete' }
```

Compile under a path with spaces and non-ASCII characters too. Only `--version`
and fixture-private `--hold` are accepted. For **each phase** reset the entry/PID
observation files before launching the actual test child. Record helper/test/PE
hashes, host/version, exact argv, elapsed, expected/actual result and cleanup.

## Required actual-native phase matrix

- `healthy`: true; exactly one `version` entry. No raw version output leaks.
- `broken`: false; one entry; no state/attach/installer entry.
- `hang`: false; actual PE parent starts another PE and waits. Record both actual
  created IDs, open **observation-only retained Process handles** while the test
  is running, then assert both HasExited after API return. Assert ReadersReleased
  and ContainmentStopped. Observe before the outer test Job is cleaned up.
- `pipe`: false; parent exits zero after valid version but child inherits pipes.
  First independently prove pipe inheritance: with product cleanup deliberately
  withheld in a private mutant, its readers must NOT reach EOF and the retained
  child observation handle must remain live. Reject fixture preflight if the
  native runtime did not actually inherit handles. Then fixed helper must stop
  child, release readers and return false before the probe envelope expires.
- `noisy-stderr`: valid version with 5000 stderr bytes must be false and complete
  early. Removal of the `4096` check must turn this phase into an assertion failure
  (accepted=true); raw stderr must never enter logs.
- `blocked-output`: writes forever; reject early, stop exact parent, release
  readers; no raw `fixture-secret` output in the result/log.

`Test-UsbipdExecutable -TimeoutSeconds 2` has an explicit **4-second total bound**:
2s version allowance + 2s fixed isolation startup/cleanup allowance. Resolver calls
instead share **one 15-second total** (including all isolation startup, inventory,
filesystem, candidate starts, version work and cleanup). A 300ms cleanup reserve
is inside that envelope. Do not assert that the direct API's argument excludes
its documented startup allowance; test both bounds and actual probe entry.

## Read-boundary injection, without real registry/PATH writes

Define only `Get-Command`, `Get-ItemProperty`, or `Test-Path` functions before
calling the real resolver. Their definitions and referenced `$global:` primitive
(string/bool/int/null) values are snapshotted into the disposable worker. Use
`$env:` for fixture paths and file sentinels for independent entry observations.
No unrelated functions, objects, closures or global state are transferred.
Aliases/functions from PATH lookup must NOT count: assert `CommandType` is exactly
`Application` in the Get-Command seam. Do not replace the resolver/version API.

1. First `Get-Command usbipd.exe` yields the real healthy PE, while a later registry
   seam would sleep 16200ms. Assert resolved path, version-entry count=1, early
   return and **zero later registry entries**. Original helper must fail.
2. Both command names unavailable; first registry seam records entry then sleeps
   16200ms. Assert refusal <=15.3s, zero version entries and complete containment.
3. First command supplies PE; Test-Path records entry then sleeps 16200ms. Assert
   refusal <=15.3s, zero native version entries and complete containment.
4. Independently supply registry APPLICATIONFOLDER (both views), ProgramW6432,
   ProgramFiles and persisted machine/user PATH. Supply fake uninstall strings
   and invalid relative/UNC directories as poison **data**, never commands. Assert
   exact source precedence, no user/process/machine PATH change and real version.
   `source-program32` tests the **ProgramFiles fallback**, not ProgramFiles(x86):
   on the native 64-bit runner, child startup normalizes ProgramFiles to
   ProgramW6432. The harness preserves its deliberately distinct fallback root
   in USBIPD_BOUNDS_PROGRAMFILES and restores it at the first injected read
   boundary after worker startup. Parent/worker roots and actual filesystem
   candidates are retained in the hashed receipts. The portable measured-state
   replay in test_windows_usbipd_programfiles.py must fail on the old harness;
   its real-resolver removal control deletes only the product ProgramFiles
   candidate and requires the unchanged phase predicate to reject readiness.
5. Missing/relative/UNC/quote preferred paths: null, no inventory entry and no
   fallback. Healthy alternate path must exist as a control.
6. Broken first PE (delayed 1s), healthy second: exact later path, both entries,
   same remaining budget. Set the private `$script:UsbipdBudgetSeconds=3` after
   dot-sourcing for the deterministic short-budget control. A second PE delayed
   2s must be cut off under the same 3s total. Reset budget after each child.

Use independent file sentinels for forbidden boundaries; fixture exceptions may
be caught by product and cannot establish non-entry. The separately repaired
native caller harness still must exercise actual USB-only/monitor handoff under
safe fake boundaries. These instructions and compiled source are native-ready
interfaces, **not native GREEN evidence**, nor an owner-machine diagnosis.

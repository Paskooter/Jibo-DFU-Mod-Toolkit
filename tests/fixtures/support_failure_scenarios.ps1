# Run Native ONLY in a disposable Windows test session, bounded by a parent.
# Example: powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -File
# tests/fixtures/support_failure_scenarios.ps1 -Mode Native -Scratch <owned-dir>
# No system clipboard access: lexical C# Clipboard stores synthetic text in RAM.
param(
    [ValidateSet('Native','Portable')][string]$Mode = 'Native',
    [Parameter(Mandatory=$true)][string]$Scratch,
    [string]$ConsoleSource = (Join-Path $PSScriptRoot '../../scripts/release/console.ps1'),
    [string]$ControllerBinary = ''
)
$ErrorActionPreference='Stop'
if ($Mode -eq 'Native' -and ($env:OS -ne 'Windows_NT' -or $PSVersionTable.PSVersion.Major -ne 5)) {
    throw 'Native proof requires Windows PowerShell 5.1 in a disposable session.'
}
if (Test-Path -LiteralPath $Scratch) { throw 'Use a fresh owned scratch directory.' }
New-Item -ItemType Directory -Path $Scratch | Out-Null
. $ConsoleSource
$env:OS='Windows_NT'; $env:JIBO_DFU_SETUP_LOG=''
$global:Forbidden=0; $global:MetadataReads=0
function Get-ItemProperty([string]$Path) {
    if ($Path -ne 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion') { throw 'unexpected metadata boundary' }
    $global:MetadataReads++
    return [pscustomobject]@{InstallationType='Client';ProductName='Windows 10 Pro';CurrentBuild='26200';UBR=9445;DisplayVersion='25H2'}
}
# This helper never loads bootstrap.ps1. Count forbidden entry as well as throw:
# a production catch must not turn a forbidden call into a passing refusal.
function Invoke-ToolkitNative { $global:Forbidden++; throw 'native setup forbidden' }
function Invoke-ToolkitElevated { $global:Forbidden++; throw 'elevation forbidden' }
function Start-ToolkitProcess { $global:Forbidden++; throw 'process setup forbidden' }
function Start-Process { $global:Forbidden++; throw 'process launch forbidden' }
function Set-Clipboard { $global:Forbidden++; throw 'system clipboard forbidden' }
function Get-ToolkitDialogOwner { throw 'fixture fallback error' }
if ($Mode -eq 'Portable') {
    Add-Type -Path (Join-Path $PSScriptRoot 'support_failure_proxy.cs')
    [ToolkitSetupStatus]::Connect($ControllerBinary)
} else {
    # Inject only a safe post-Join wrapper fault. No product hook or UI changes.
    $join='dialogThread.Join(); // An explicit user decision, never a command timeout.'
    if (-not $script:ToolkitSetupUiSource.Contains($join)) { throw 'post-Join seam missing' }
    $ui=$script:ToolkitSetupUiSource.Replace($join, $join + "`n" + '        if (SupportFailureController.ThrowAfterDialog) throw new InvalidOperationException("synthetic wrapper error");')
    $fixture=(Get-Content -Raw (Join-Path $PSScriptRoot 'support_clipboard_faults.cs')) + (Get-Content -Raw (Join-Path $PSScriptRoot 'support_failure_controller.cs'))
    Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition ($ui + $script:ToolkitQuestionModelSource + $fixture)
}
$script:RealStart=${function:Start-ToolkitSetup}; $script:RealStop=${function:Stop-ToolkitSetup}
$script:Starts=0; $script:Stops=0
function Start-ToolkitSetup { $script:Starts++; & $script:RealStart }
function Stop-ToolkitSetup { $script:Stops++; & $script:RealStop }
try {
    Initialize-ToolkitLog -Root $Scratch -Build ('f'*40)
    if ($global:MetadataReads -ne 1 -or $script:ToolkitWindowsVersion -ne 'Windows 11 Pro 25H2 (build 26200.9445)') { throw 'native formatter seam failed' }
    Start-ToolkitSetup
    Write-ToolkitLog 'setup-end' @{result='failed';error='Synthetic failure'}
    $index=0
    foreach ($modeName in @('external','thread','invalid-operation','success','external')) {
        if ($Mode -eq 'Portable') {
            [ToolkitSetupStatus]::Mode=$modeName
            [ToolkitSetupStatus]::ThrowAfterDialog=($index -eq 4)
        } else {
            [SupportFailureController]::ThrowAfterDialog=($index -eq 4)
            [SupportFailureController]::Exercise($modeName)
        }
        $caught=$false
        try { Show-ToolkitFailure 'Synthetic failure' } catch {
            if ($_.Exception.Message -notmatch 'fixture fallback error') { throw }
            $caught=$true
        }
        if ($Mode -eq 'Native' -and [SupportFailureController]::DriverError) { throw [SupportFailureController]::DriverError }
        if ($caught -ne ($index -eq 4)) { throw 'wrapper error control not exercised' }
        $rows=@(Get-Content $script:ToolkitLogPath | ForEach-Object {$_ | ConvertFrom-Json})
        $events=@($rows | Where-Object event -eq 'clipboard-copy-failed')
        $expected=if ($index -lt 3) {$index+1} elseif ($index -eq 3) {3} else {4}
        if ($events.Count -ne $expected) { throw ('WRAPPER-PERSISTENCE: safe failure did not reach log after dialog: '+$events.Count) }
        if ($modeName -ne 'success') {
            $event=$events[-1]
            $category=if ($modeName -eq 'external') {'external'} elseif ($modeName -eq 'thread') {'thread-state'} else {'invalid-operation'}
            if ($event.category -ne $category -or $event.apartment -ne 'STA' -or $event.managed_thread_id -le 0 -or $event.summary_length -le 0 -or $event.hresult -notmatch '^0x[0-9A-F]{8}$') { throw 'safe provenance lost across wrapper' }
            $fields=@($event.PSObject.Properties.Name | Sort-Object)
            $want=@('utc','event','at_utc','exception_type','category','hresult','apartment','managed_thread_id','summary_length') | Sort-Object
            if (Compare-Object $fields $want) { throw 'unexpected persisted field' }
            $details=Get-ToolkitSupportDetails 'Synthetic failure'
            if (-not $details.Contains('clipboard-copy-failed') -or -not $details.Contains($event.hresult)) { throw 'next support read cannot expose failure' }
        }
        if (@([ToolkitSetupStatus]::DrainClipboardFailures()).Count -ne 0) { throw 'orphaned queue-only failure' }
        $index++
    }
    if ($global:Forbidden -ne 0) { throw 'installer/device/clipboard boundary entered' }
    if ($script:Starts -ne 6 -or $script:Stops -ne 5) { throw 'real wrapper stop/start chain missing' }
    if ($Mode -eq 'Portable' -and [ToolkitSetupStatus]::Trace -notmatch 'OK START;OK STOP;OK START;OK DETAILS STA button=1 manual-fallback=ok;OK DRAIN;') { throw 'portable controller chain missing' }
    if ($Mode -eq 'Native' -and [SupportFailureController]::DialogCount -ne 5) { throw 'native dialog clicks missing' }
    if ([IO.File]::ReadAllText($script:ToolkitLogPath) -match 'fixture-private') { throw 'private exception or summary leaked' }
    $receipt=[ordered]@{mode=$Mode;windows=[Environment]::OSVersion.VersionString;powershell=$PSVersionTable.PSVersion.ToString();clr=[Environment]::Version.ToString();clipboard='synthetic-memory-only';cases=5;events=4;forbidden_calls=$global:Forbidden;starts=$script:Starts;stops=$script:Stops;source_sha256=(Get-FileHash -Algorithm SHA256 $ConsoleSource).Hash;result='ok'}
    $receipt | ConvertTo-Json | Set-Content (Join-Path $Scratch 'receipt.json')
    Write-Output 'PASS WRAPPER-PERSISTENCE cases=5 events=4 forbidden=0'
} finally {
    if ($Mode -eq 'Portable') { [ToolkitSetupStatus]::Close() } else { Stop-ToolkitSetup }
}

# Parse and exercise the bootstrap without real WSL, USB, installs or UAC.
$ErrorActionPreference = 'Stop'
$hostBinary = (Get-Process -Id $PID).Path
$path = Join-Path $PSScriptRoot 'bootstrap.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$script:answer = 'n'
function Read-Host { param($Prompt); return $script:answer }
if (Confirm-Setup 'Test') { throw 'Decline was not respected' }
$script:answer = 'yes'
if (-not (Confirm-Setup 'Test')) { throw 'Explicit approval was not accepted' }
$script:received = @()
function wsl.exe { $script:received = $args; $global:LASTEXITCODE=0; return "Ubuntu`0" }
$result = Native -Arguments @('--distribution', 'Ubuntu with spaces', '--exec', 'true')
if ($result -ne 'Ubuntu' -or $script:received.Count -ne 4 -or $script:received[1] -ne 'Ubuntu with spaces') { throw 'Native argument or NUL handling failed' }
function wsl.exe { $global:LASTEXITCODE=1; return 'not installed' }
$failed = $false
try { Native -Arguments @('--version') | Out-Null } catch { $failed = $true }
if (-not $failed) { throw 'Native failure was swallowed' }
# Diagnose old glibc/musl distributions without running a real WSL process.
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.35' }
if (-not (Test-CompatibleDistribution 'fixture')) { throw 'Supported distro refused' }
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.31' }
if (Test-CompatibleDistribution 'fixture') { throw 'Old glibc accepted' }
function wsl.exe { $global:LASTEXITCODE=0; return 'musl' }
if (Test-CompatibleDistribution 'fixture') { throw 'musl distro accepted' }
# Original USB helper is shipped byte-for-byte, rather than a second USB algorithm.
$usbPath = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'run.ps1'
$usbErrors=$null
[System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$usbErrors) | Out-Null
if ($usbErrors.Count) { throw ($usbErrors | Out-String) }
# Exercise the entire missing-WSLg refusal path in a child process. A mock admin
# function leaves a sentinel if any installation is attempted after declining.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    [IO.File]::WriteAllText((Join-Path $fixture 'payload.tar.gz'), 'not read on refusal')
    [IO.File]::WriteAllText((Join-Path $fixture 'run.ps1'), 'not run on refusal')
    $child = Join-Path $fixture 'refusal.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
$global:BootstrapTestCase = $Case
$global:BootstrapTestSentinel = Join-Path $Payload 'unexpected-admin'
function wsl.exe {
    if ($global:BootstrapTestCase -eq 'no-distro' -and $args[0] -eq '--version') {
        $global:LASTEXITCODE=0; return 'WSLg 1.0'
    }
    $global:LASTEXITCODE=1; return 'No installed distributions or GUI support'
}
function Read-Host { param($Prompt); return 'n' }
function Start-Process { [IO.File]::WriteAllText($global:BootstrapTestSentinel, 'failed'); throw 'Unexpected admin action' }
& $Bootstrap -PayloadRoot $Payload
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('no-gui', 'no-distro')) {
        $messages = @(& $hostBinary -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
        $expected = if ($case -eq 'no-distro') { 'Distribution setup declined' } else { 'No setup changes were made' }
        if ($LASTEXITCODE -ne 1 -or (Test-Path (Join-Path $fixture 'unexpected-admin')) -or (($messages -join ' ') -notmatch $expected)) {
            throw ('Full bootstrap refusal path failed (' + $case + '): ' + ($messages -join ' '))
        }
    }
} finally { Remove-Item -Recurse -Force $fixture }
# Real native-process argv roundtrip, no admin/WSL. This runs on the host's
# parser (Windows PowerShell 5.1 in Actions; PowerShell 7 locally on Linux).
$argvFixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-argv-' + [guid]::NewGuid())
New-Item -ItemType Directory $argvFixture | Out-Null
try {
    $argvChild = Join-Path $argvFixture 'argv.ps1'
    $argvOutput = Join-Path $argvFixture 'argv.json'
    [IO.File]::WriteAllText($argvChild, 'ConvertTo-Json -InputObject @($args) -Compress')
    $values = @('Ubuntu with spaces', '/path with spaces/helper.sh', 'embedded"quote', 'C:\trailing\', '')
    $argumentLine = ConvertTo-NativeArgumentLine (@('-NoProfile', '-File', $argvChild) + $values)
    $process = Start-Process -FilePath $hostBinary -ArgumentList $argumentLine -RedirectStandardOutput $argvOutput -Wait -PassThru
    $actual = @(Get-Content $argvOutput -Raw | ConvertFrom-Json)
    if ($process.ExitCode -ne 0 -or $actual.Count -ne $values.Count) { throw 'Native argv roundtrip count failed' }
    for ($i=0; $i -lt $values.Count; $i++) {
        if ($actual[$i] -cne $values[$i]) { throw ('Native argv roundtrip failed at index ' + $i) }
    }
} finally { Remove-Item -Recurse -Force $argvFixture }
# Windows privileges: UAC comes before any root watcher; GUI uses an explicit non-root user.
$script:calls = @()
function Native { param([string[]]$Arguments); $script:calls += ,$Arguments; return '' }
function Invoke-ApprovedAdmin { param($File, $Arguments); $script:calls += ,@('UAC', $File, $Arguments) }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru)
    $script:calls += ,@('WATCH', $FilePath, $ArgumentList)
    return [pscustomobject]@{ HasExited=$false; Id=4242 }
}
$watch = Start-WslUsbAccess -Name 'Ubuntu with spaces' -User 'fixture' -Uid '1000' -Gid '1001' -Helper '/mnt/c/path with spaces/usb.sh' -Session '/home/fixture/.usb-session'
if ($script:calls.Count -ne 3 -or $script:calls[0][0] -ne 'UAC') { throw 'Root watcher started before UAC consent' }
$adminArgs = $script:calls[0][2]
$watchArgs = $script:calls[2][2]
if ($adminArgs -notmatch '"--user" "root"' -or $watchArgs -notmatch '"--user" "root"' -or $adminArgs -notmatch '"Ubuntu with spaces"' -or $adminArgs -notmatch '"/mnt/c/path with spaces/usb.sh"') { throw 'Root WSL identity/argument preservation failed' }
if (($script:calls[1] -join '|') -ne '--distribution|Ubuntu with spaces|--user|fixture|--exec|touch|--|/home/fixture/.usb-session') { throw 'Session marker was not created by the normal user' }
$script:calls = @()
function Invoke-ApprovedAdmin { param($File, $Arguments); throw 'UAC declined' }
$failed = $false
try { Start-WslUsbAccess -Name 'fixture' -User 'fixture' -Uid '1000' -Gid '1001' -Helper '/usb.sh' -Session '/session' | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:calls.Count) { throw 'UAC cancellation did not stop before root watcher/session setup' }
$script:calls = @()
function wsl.exe { $script:calls += ,$args; $global:LASTEXITCODE=41; return 'Synthetic GUI stdout' }
$code = Invoke-WslGui -Name 'Ubuntu with spaces' -User 'fixture' -Target '/home/fixture/path with spaces' -Session '/session'
if ($code -ne 41 -or ($script:calls[0] -join '|') -ne '--distribution|Ubuntu with spaces|--user|fixture|--exec|bash|/home/fixture/path with spaces/wsl-user-launch.sh|/home/fixture/path with spaces/jibo-dfu|/session') { throw 'GUI identity, direct binary dispatch or exit propagation failed' }
# Exercise the actual top-level bootstrap, not just helpers. Every external
# command is a synthetic boundary, with an invocation trace and controlled exits.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-flow-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    foreach ($file in @('payload.tar.gz', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh')) {
        [IO.File]::WriteAllText((Join-Path $fixture $file), 'synthetic; never executed')
    }
    [IO.File]::WriteAllText((Join-Path $fixture 'revision.txt'), ('a' * 40))
    $child = Join-Path $fixture 'flow.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
$global:UacCase = $Case
$global:UacTrace = Join-Path $Payload ($Case + '.jsonl')
function Record($Kind, $Values) { [IO.File]::AppendAllText($global:UacTrace, (([pscustomobject]@{Kind=$Kind; Values=@($Values)} | ConvertTo-Json -Compress) + "`n")) }
function wsl.exe {
    Record 'wsl' $args
    $global:LASTEXITCODE = 0
    if ($args[0] -eq '--version') { return 'WSLg 1.0' }
    if ($args[0] -eq '--list') { return '* Ubuntu with spaces Running 2' }
    if ($args -contains 'getconf') { return 'glibc 2.35' }
    if ($args -contains 'uname') { return 'x86_64' }
    if ($args -contains 'id') {
        if ($args[-1] -eq '-u') { if ($global:UacCase -eq 'root-user') { return '0' }; return '1000' }
        if ($args[-1] -eq '-g') { return '1001' }
        return 'fixture'
    }
    if ($args -contains 'printenv') { return '/home/fixture/path with spaces' }
    if ($args -contains 'wslpath') { return ('/mnt/c/fixture/' + (Split-Path $args[-1] -Leaf)) }
    if ($args -contains 'bash' -and ($args -join ' ') -match '/wsl-user-launch.sh|/AppRun') { $global:LASTEXITCODE = 41 }
}
function powershell.exe {
    Record 'usb' $args
    $global:LASTEXITCODE = if ($global:UacCase -eq 'usb-declined') { 3 } else { 0 }
}
function Get-Command { param($Name, $ErrorAction); return [pscustomobject]@{Source='fixture-usbipd'} }
function Read-Host { param($Prompt); return 'n' }
function Start-Process {
    param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru, [switch]$NoNewWindow)
    Record 'start' @($FilePath, $ArgumentList, $Verb)
    if ($Verb -eq 'RunAs') {
        if ($global:UacCase -eq 'uac-declined') { throw 'Synthetic UAC cancellation' }
        $code = if ($global:UacCase -eq 'admin-failed') { 5 } else { 0 }
        return [pscustomobject]@{ExitCode=$code}
    }
    $process = [pscustomobject]@{Id=4242; HasExited=($global:UacCase -eq 'watcher-failed' -and $FilePath -match 'wsl.exe$'); ExitCode=7}
    $process | Add-Member ScriptMethod WaitForExit { param($Timeout); if ($Timeout) { return $this.HasExited }; $this.HasExited=$true }
    return $process
}
function Stop-Process { param($Id); Record 'stop' @($Id) }
& $Bootstrap -PayloadRoot $Payload
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('success', 'root-user', 'uac-declined', 'admin-failed', 'usb-declined', 'watcher-failed')) {
        $messages = @(& $hostBinary -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
        $code = $LASTEXITCODE
        $events = @(Get-Content (Join-Path $fixture ($case + '.jsonl')) | ForEach-Object { $_ | ConvertFrom-Json })
        $guiCalls = @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'bash' -and ($_.Values -join ' ') -match '/wsl-user-launch.sh|/AppRun' })
        if ($case -eq 'success') {
            if ($code -ne 41 -or $guiCalls.Count -ne 1) { throw ('Full bootstrap GUI exit failed: ' + ($messages -join ' ') + ' TRACE: ' + ($events | ConvertTo-Json -Compress -Depth 5)) }
            $gui = $guiCalls[0].Values
            if (($gui -join '|') -notmatch '\|--user\|fixture\|' -or ($gui -join ' ') -match '/AppRun') { throw 'Full EXE route still requests sudo/root GUI' }
            $admin = @($events | Where-Object { $_.Kind -eq 'start' -and $_.Values[-1] -eq 'RunAs' })
            if ($admin.Count -ne 1 -or $admin[0].Values[1] -notmatch '"--user" "root"') { throw 'Full EXE route did not authorize root USB setup via UAC' }
            if (-not @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'rm' }).Count) { throw 'Full bootstrap did not clean the permission session' }
        } else {
            if ($code -ne 1 -or $guiCalls.Count) { throw ('Full bootstrap failed to refuse ' + $case + ': ' + ($messages -join ' ')) }
            if ($case -eq 'root-user' -and @($events | Where-Object { $_.Kind -eq 'start' -or $_.Values -contains 'tar' }).Count) { throw 'Root default user was rejected too late' }
        }
    }
} finally { Remove-Item -Recurse -Force $fixture }
Write-Output 'PASS: PowerShell parse/helpers plus 8 full bootstrap scenarios (2 setup refusals, GUI exit, root-user refusal, UAC cancellation, admin failure, USB refusal, watcher failure); all WSL/USB/admin boundaries mocked.'

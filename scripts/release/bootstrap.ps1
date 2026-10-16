[CmdletBinding()]
param([string]$PayloadRoot = $PSScriptRoot, [string]$Distro, [switch]$SelfCheck, [switch]$ShowConsole)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$launcherExitCode = 1

function Confirm-Setup([string]$Question) {
    Update-ToolkitSetup 'Waiting for your answer' $Question
    Write-ToolkitLog 'question-start' @{ step=$script:ToolkitStep }
    try {
        $answer = Show-ToolkitQuestion $Question
        Write-ToolkitLog 'question-end' @{ step=$script:ToolkitStep; approved=[bool]$answer }
        return $answer
    } finally { Update-ToolkitSetup $script:ToolkitStep $script:ToolkitPolicy.Hint }
}
function Invoke-ApprovedAdmin([string]$File, [string]$Arguments) {
    $p = Invoke-ToolkitElevated -File $File -Arguments $Arguments -TimeoutSeconds 900 -AllowWait
    if ($p.ExitCode -ne 0) { throw "Setup exited with code $($p.ExitCode). Restart this app after any requested reboot." }
}
function Native([string[]]$Arguments, [double]$TimeoutSeconds = 0) {
    $policy = if ($script:ToolkitPolicy) { $script:ToolkitPolicy } else { Get-ToolkitStepPolicy 'Starting Linux' }
    if ($TimeoutSeconds -le 0) { $TimeoutSeconds = $policy.Seconds }
    $result = Invoke-ToolkitNative -File "$env:SystemRoot\System32\wsl.exe" -Arguments $Arguments -TimeoutSeconds $TimeoutSeconds -AllowWait:$policy.AllowWait
    if ($result.ExitCode -ne 0) {
        $error = New-Object Exception "WSL failed: $($result.StartError) $($result.Output -join ' ')"
        $error.Data['ToolkitExitCode'] = $result.ExitCode
        if ($result.TimedOut) { $error.Data['ToolkitStall'] = $true }
        throw $error
    }
    return (($result.Output | ForEach-Object { $_.ToString() -replace "`0", '' }) -join "`n").Trim()
}
function ConvertTo-NativeArgumentLine([string[]]$Arguments) {
    # Start-Process joins arrays on Windows. Quote for the native argv parser,
    # not PowerShell; preserve spaces, embedded quotes and trailing backslashes.
    return (($Arguments | ForEach-Object {
        '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
}
function Test-WslDistributionName([string]$Name) {
    # WSL's own rule for registered names. wsl.exe reads its options with a plain
    # whitespace split, so names outside this set could not be passed safely.
    return ($Name -cmatch '^[A-Za-z0-9._-]{1,256}$')
}
function Get-WslRootArgumentLine([string]$Name, [string[]]$Command) {
    # wsl.exe parses options before --exec itself, splitting on spaces/tabs with no
    # quote handling: a quoted "--distribution" is not an option, and the whole line
    # then runs through the distro's shell. Options stay bare; only the command after
    # --exec is quoted, because wsl.exe passes that part through CommandLineToArgvW.
    if (-not (Test-WslDistributionName $Name)) { throw 'Invalid WSL distribution name.' }
    return ('--distribution {0} --user root --exec {1}' -f $Name, (ConvertTo-NativeArgumentLine $Command))
}
function Start-WslUsbAccess([string]$Name, [string]$User, [string]$Uid, [string]$Gid, [string]$Helper, [string]$Session) {
    if ($Uid -notmatch '^[1-9][0-9]*$' -or $Gid -notmatch '^[0-9]+$') { throw 'USB access requires a non-root Linux user.' }
    if (-not (Test-WslDistributionName $Name)) { throw 'Invalid WSL distribution name.' }
    # Root inside the user's own WSL distribution is available to wsl.exe without
    # Windows elevation or a Linux password. An elevated wsl.exe would also be a
    # different session that cannot see this user's distributions on some systems.
    Write-Host 'Setting up Jibo USB access inside WSL (no password needed). The GUI will remain your normal Linux user.'
    Native -Arguments @('--distribution', $Name, '--user', 'root', '--exec', 'bash', $Helper, $Uid, $Gid, 'once') | Out-Null
    Native -Arguments @('--distribution', $Name, '--user', $User, '--exec', 'touch', '--', $Session) | Out-Null
    $wsl = "$env:SystemRoot\System32\wsl.exe"
    $line = Get-WslRootArgumentLine -Name $Name -Command @('bash', $Helper, $Uid, $Gid, $Session)
    $process = Start-ToolkitProcess -FilePath $wsl -ArgumentList $line
    # Cache the handle before waiting: Windows PowerShell can otherwise report
    # a null ExitCode for -NoNewWindow processes (PowerShell/PowerShell #5421).
    $null = $process.Handle
    return $process
}
function Install-PrivateDistribution([string]$Root) {
    $name = 'Jibo-DFU-Toolkit'
    $url = 'https://releases.ubuntu.com/jammy/ubuntu-22.04.5-wsl-amd64.wsl'
    $sha256 = '4499c4fe257f2fc83145b429ce211a0a43fd590e70d6261ede616210947d9f8f'
    $bytes = 360684292
    $provision = Join-Path $Root 'wsl-provision.sh'
    if (-not (Test-Path $provision)) { throw 'Embedded Linux setup helper is missing.' }
    if (-not $env:LOCALAPPDATA) { throw 'Windows local application-data directory is unavailable.' }
    $base = Join-Path $env:LOCALAPPDATA $name
    $install = Join-Path $base 'wsl'
    # Never overwrite a previous Linux filesystem, including an unregistered one.
    if (Test-Path $install) { throw "Private Linux storage already exists at $install. Move it aside only if you no longer need its Linux files, then reopen the toolkit." }
    New-Item -ItemType Directory -Force -Path $base | Out-Null
    $download = Join-Path $base ('ubuntu-' + [guid]::NewGuid().ToString('N') + '.wsl')
    $imported = $false
    try {
        Set-ToolkitStep 'Downloading Ubuntu'
        Write-Host 'Downloading the official Ubuntu 22.04 image for the private toolkit Linux...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Receive-ToolkitDownload -Uri $url -OutFile $download -ExpectedBytes $bytes
        if ((Get-Item -LiteralPath $download).Length -ne $bytes) { throw 'Ubuntu image size mismatch. Nothing was registered.' }
        Set-ToolkitStep 'Checking the Ubuntu image'
        if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ine $sha256) { throw 'Ubuntu image checksum mismatch. Nothing was registered.' }
        Set-ToolkitStep "Installing the toolkit's Linux"
        Write-Host 'Verified Ubuntu image. Setting up the private Linux (no account or password prompts)...'
        # Microsoft's --import deliberately sets NO_OOBE. It works with this gzip
        # tar image and avoids the newer --install --from-file first-run wizard.
        # Registration belongs to this Windows user; never elevate wsl.exe here.
        Native -Arguments @('--import', $name, $install, $download, '--version', '2') | Out-Host
        $imported = $true
        $linuxProvision = Native -Arguments @('--distribution', $name, '--user', 'root', '--exec', 'wslpath', '-u', '--', $provision)
        Set-ToolkitStep 'Preparing Linux'
        Native -Arguments @('--distribution', $name, '--user', 'root', '--exec', 'bash', $linuxProvision) | Out-Host
        Native -Arguments @('--terminate', $name) | Out-Null
        Set-ToolkitStep 'Starting Linux'
        if ((Native -Arguments @('--distribution', $name, '--exec', 'id', '-un')) -cne 'jibo') { throw 'Private Linux did not start as the toolkit user.' }
        if (-not (Get-DistributionRuntime $name -RequireUbuntu)) { throw 'Private Linux compatibility check failed.' }
        Write-Host 'Private Linux is ready. Future launches need no setup download.'
        return $name
    } catch {
        $failureCode = if ($_.Exception.Data['ToolkitExitCode']) { [int]$_.Exception.Data['ToolkitExitCode'] } else { 1 }
        Complete-ToolkitStep -Result failed -ExitCode $failureCode -ErrorText $_.Exception.Message
        # Only a distro successfully imported by THIS attempt may be removed.
        # Never unregister an existing distro or one whose import failed/conflicted.
        if ($imported) {
            try { Native -Arguments @('--unregister', $name) -TimeoutSeconds 30 | Out-Null }
            catch { Write-Warning 'Could not remove the failed private Linux. Run: wsl --unregister Jibo-DFU-Toolkit' }
        }
        throw
    } finally {
        if (Test-Path $download) { Remove-Item -LiteralPath $download -Force }
    }
}
function Invoke-WslGui([string]$Name, [string]$User, [string]$Target, [string]$Session, $Permissions, $Monitor, [switch]$ShowConsole, [double]$ReadyTimeoutSeconds = 120) {
    # Supervise readiness and USB support without ever allocating a console.
    if (-not (Test-WslDistributionName $Name) -or $User -notmatch '^[^\s"]+$') { throw 'Invalid WSL GUI identity.' }
    $run = Join-Path ([IO.Path]::GetTempPath()) ('jibo-dfu-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $run | Out-Null
    $setupStopped = $false
    $supportFailed = $false
    try {
        $readyFile = Join-Path $run 'ready'
        $linuxReady = Native -Arguments @('--distribution', $Name, '--user', $User, '--exec', 'wslpath', '-u', '--', $readyFile)
        $command = ConvertTo-NativeArgumentLine @('bash', "$Target/wsl-user-launch.sh", "$Target/jibo-dfu", $Session, $linuxReady)
        $line = '--distribution {0} --user {1} --exec {2}' -f $Name, $User, $command
        $gui = Start-ToolkitProcess -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList $line
        $null = $gui.Handle # Retain ExitCode when using WaitForExit instead of -Wait.
        $readyTimer = [Diagnostics.Stopwatch]::StartNew()
        while (-not $gui.WaitForExit(100)) {
            if (-not $supportFailed -and (($Permissions -and $Permissions.HasExited) -or ($Monitor -and $Monitor.HasExited))) {
                # The wrapper removes its session at normal exit; allow wsl.exe
                # to report that exit before diagnosing a dead USB watcher.
                if ($gui.WaitForExit(200)) { break }
                $supportFailed = $true
                Show-ToolkitFailure 'USB support stopped unexpectedly. Close the GUI when safe, then reopen the toolkit.'
            }
            if (-not $setupStopped -and (Test-Path -LiteralPath $readyFile)) {
                Complete-ToolkitStep
                Write-ToolkitLog 'gui-ready' @{ step='Opening the toolkit' }
                Stop-ToolkitSetup
                $setupStopped = $true
            }
            if (-not $setupStopped -and (Test-ToolkitStall -ElapsedSeconds $readyTimer.Elapsed.TotalSeconds -LimitSeconds $ReadyTimeoutSeconds)) {
                $message = "Opening the toolkit stalled: its window did not appear within $ReadyTimeoutSeconds seconds. Close other WSL work, run wsl --shutdown, then reopen."
                Write-ToolkitLog 'stall' @{ step='Opening the toolkit'; error=$message }
                if (Show-ToolkitSupportDialog $message -AllowWait) { Write-ToolkitLog 'keep-waiting' @{step='Opening the toolkit'}; $readyTimer.Restart() }
                else { $error = New-Object TimeoutException $message; $error.Data['ToolkitStall'] = $true; throw $error }
            }
        }
        # A helper can fail in the same polling interval as a normal GUI close.
        # Do not let that race turn a failed USB session into a successful exit.
        if (($Permissions -and $Permissions.HasExited -and $Permissions.ExitCode -ne 0) -or ($Monitor -and $Monitor.HasExited)) { $supportFailed = $true }
        if ($null -eq $gui.ExitCode) { throw 'WSL did not report the GUI exit code.' }
        if ($gui.ExitCode -ne 0) { return $gui.ExitCode }
        if ($supportFailed) { return 1 }
        return 0
    } catch {
        Complete-ToolkitStep -Result failed -ExitCode 1 -ErrorText $_.Exception.Message
        throw
    } finally {
        if (-not $setupStopped -and $gui -and -not $gui.WaitForExit(0)) { Stop-ToolkitProcess $gui }
        Remove-Item -LiteralPath $run -Recurse -Force
    }
}
function Test-CompatibleDistribution([string]$Name) {
    try {
        $libc = Native -Arguments @('--distribution', $Name, '--exec', 'getconf', 'GNU_LIBC_VERSION')
        if ($libc -notmatch '^glibc\s+(\d+\.\d+)$') { return $false }
        return ([version]$Matches[1] -ge [version]'2.35')
    } catch { if ($_.Exception.Data['ToolkitStall']) { throw }; return $false }
}
function Get-DistributionRuntime([string]$Name, [switch]$RequireUbuntu) {
    # No shell interpolation: read distro metadata as data, not executable code.
    if (-not (Test-WslDistributionName $Name)) { return $null }
    try {
        Set-ToolkitStep 'Starting Linux'
        if (-not (Test-CompatibleDistribution $Name)) { return $null }
        if ((Native -Arguments @('--distribution', $Name, '--exec', 'uname', '-m')) -ne 'x86_64') { return $null }
        $uid = Native -Arguments @('--distribution', $Name, '--exec', 'id', '-u')
        if ($uid -notmatch '^[1-9][0-9]*$') { return $null }
        Native -Arguments @('--distribution', $Name, '--exec', 'test', '-d', '/mnt/wslg') | Out-Null
        $release = Native -Arguments @('--distribution', $Name, '--exec', 'cat', '/etc/os-release')
        $os = ''; $version = [version]'0.0'
        foreach ($line in ($release -split "`n")) {
            if ($line -match '^ID=["'']?([a-z0-9_-]+)["'']?$') { $os = $Matches[1] }
            if ($line -match '^VERSION_ID=["'']?([0-9]+\.[0-9]+)["'']?$') { $version = [version]$Matches[1] }
        }
        if ($RequireUbuntu -and ($os -cne 'ubuntu' -or $version -lt [version]'22.04')) { return $null }
        return [pscustomobject]@{ Name=$Name; OsVersion=$version }
    } catch { if ($_.Exception.Data['ToolkitStall']) { throw }; return $null }
}
function Select-ToolkitDistribution($Rows, [string]$Requested, [string]$Root) {
    if ($Requested) {
        $row = @($Rows | Where-Object { $_.Name -ceq $Requested -and $_.Version -eq 2 })
        if ($row.Count -ne 1) { throw 'Requested distribution is not installed as WSL 2.' }
        if (-not (Get-DistributionRuntime $Requested)) { throw 'Requested distribution is incompatible: x86_64, glibc 2.35+, WSLg and a non-root default user are required.' }
        return $Requested
    }
    # Never even probe a distro belonging to another product. Names are deliberately
    # narrower than WSL's legal-name rule; only an explicit -Distro overrides this.
    $candidates = @()
    foreach ($row in $Rows) {
        if ($row.Version -ne 2 -or ($row.Name -cne 'Jibo-DFU-Toolkit' -and $row.Name -cnotmatch '^Ubuntu(-[0-9]{2}\.[0-9]{2})?$')) { continue }
        $runtime = Get-DistributionRuntime $row.Name -RequireUbuntu
        if ($runtime) {
            if ($row.Name -ceq 'Jibo-DFU-Toolkit') { return $row.Name }
            $candidates += [pscustomobject]@{ Name=$row.Name; OsVersion=$runtime.OsVersion; Default=$row.Default }
        }
    }
    $ordered = @($candidates | Sort-Object @{Expression='Default'; Descending=$true}, @{Expression='OsVersion'; Descending=$true}, Name)
    if ($ordered.Count) { return $ordered[0].Name }
    if (@($Rows | Where-Object { $_.Name -ceq 'Jibo-DFU-Toolkit' }).Count) {
        throw 'The private Linux is not ready. To remove it and its Linux files, run: wsl --unregister Jibo-DFU-Toolkit. Then reopen the toolkit to set it up again.'
    }
    if (-not (Confirm-Setup 'Set up a private Linux for the toolkit? One-time download of about 345 MB. No Linux account or password needed.')) { throw 'Distribution setup declined. No download or import was attempted.' }
    return (Install-PrivateDistribution -Root $Root)
}
try {
    . (Join-Path $PSScriptRoot 'console.ps1')
    Initialize-ToolkitLog
    Set-ToolkitStep 'Checking embedded files'
    $revision = (Get-Content (Join-Path $PayloadRoot 'revision.txt') -Raw).Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw 'Invalid embedded revision.' }
    $script:ToolkitBuild = $revision
    Write-ToolkitLog 'build' @{ build=$revision }
    $archive = Join-Path $PayloadRoot 'payload.tar.gz'
    $usbScript = Join-Path $PayloadRoot 'run.ps1'
    if (-not (Test-Path $archive) -or -not (Test-Path $usbScript)) { throw 'Embedded payload is incomplete.' }
    if ($SelfCheck) { Complete-ToolkitStep; Write-ToolkitLog 'setup-end' @{result='ok'; exit_code=0}; Write-Output 'Bootstrap scripts and Linux payload are present; WSL setup not attempted.'; exit 0 }
    Start-ToolkitSetup
    Set-ToolkitStep 'Checking Windows and WSL'
    Write-Host 'Jibo DFU Toolkit: Windows uses WSL 2 + WSLg and usbipd-win. Setup requires internet only if these are missing.'
    $ready = $false
    try {
        $version = Native -Arguments @('--version')
        $script:ToolkitWslVersion = Protect-ToolkitText $version
        Write-ToolkitLog 'wsl-version' @{ version=$script:ToolkitWslVersion }
        $ready = ($version -match 'WSLg')
    } catch {
        $script:ToolkitWslVersion = 'unavailable (see command failure in log)'
        if ($_.Exception.Data['ToolkitStall']) { throw }
        $ready = $false
    }
    if (-not $ready) {
        if (-not (Confirm-Setup 'Install/update WSL 2 and WSLg? Windows will ask for administrator approval.')) { throw 'WSL setup declined. No setup changes were made.' }
        Set-ToolkitStep 'Installing Windows components'
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--install --no-distribution'
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--update'
        throw 'WSL setup requested. Reboot if Windows requests it, then run this download again.'
    }
    $rows = @()
    Set-ToolkitStep 'Finding Linux'
    try { $listing = Native -Arguments @('--list', '--verbose') }
    catch { if ($_.Exception.Data['ToolkitStall']) { throw }; $listing = '' }
    foreach ($line in ($listing -split "`n")) {
        if ($line -match '^\s*(?<default>\*)?\s*(?<name>.+?)\s+\S+\s+(?<version>[12])\s*$') {
            $rows += [pscustomobject]@{Name=$Matches['name'].Trim(); Version=[int]$Matches['version']; Default=($Matches['default'] -eq '*')}
        }
    }
    $Distro = Select-ToolkitDistribution -Rows $rows -Requested $Distro -Root $PayloadRoot
    Write-Host "Using Linux: $Distro."
    $usb = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if (-not $usb) { $usb = Get-Command usbipd -ErrorAction SilentlyContinue }
    if (-not $usb) {
        if (-not (Confirm-Setup 'Install usbipd-win using the interactive Windows installer?')) { throw 'USB setup declined.' }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'Install Microsoft App Installer (winget) or usbipd-win from https://github.com/dorssel/usbipd-win/releases, then reopen this app.' }
        Set-ToolkitStep 'Installing Windows components'
        Invoke-ApprovedAdmin $winget.Source 'install --interactive --exact --id dorssel.usbipd-win'
        throw 'usbipd-win setup finished. Reopen this app so Windows refreshes PATH.'
    }
    $linuxUser = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-un')
    $linuxUid = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-u')
    $linuxGid = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-g')
    if ($linuxUid -notmatch '^[1-9][0-9]*$' -or $linuxGid -notmatch '^[0-9]+$' -or $linuxUser -match '["\r\n]' -or -not $linuxUser) {
        throw 'Choose a WSL distribution with a non-root default user. The GUI will not run as root.'
    }
    $linuxHome = Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'printenv', 'HOME')
    if (-not $linuxHome.StartsWith('/') -or $linuxHome -match '[\r\n]') { throw 'Invalid WSL home directory.' }
    Set-ToolkitStep 'Unpacking the toolkit'
    $target = "$linuxHome/.local/share/jibo-dfu/$revision"
    $linuxArchive = Native -Arguments @('--distribution', $Distro, '--exec', 'wslpath', '-u', '--', $archive)
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'mkdir', '-p', '--', $target) | Out-Null
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'tar', '-xzf', $linuxArchive, '-C', $target) | Out-Null
    foreach ($helper in @('wsl-usb-permissions.sh', 'wsl-user-launch.sh')) {
        $source = Join-Path $PayloadRoot $helper
        if (-not (Test-Path $source)) { throw 'Embedded Windows USB helper is missing.' }
        $linuxSource = Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'wslpath', '-u', '--', $source)
        Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'cp', '--', $linuxSource, "$target/$helper") | Out-Null
    }
    # The receipt is for diagnostics; Native still throws with the full output on failure.
    Set-ToolkitStep 'Checking the toolkit'
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', "$target/jibo-dfu", '--self-check') | Out-Null
    Write-Host 'Toolkit self-check passed.'
    $session = "$target/.usb-$([guid]::NewGuid().ToString('N'))"
    $permissions = $null
    $monitor = $null
    try {
        # Existing validated bind/attach/reattach logic retains usbipd's UAC prompt.
        Set-ToolkitStep 'Connecting USB'
        $consoleHelper = Join-Path $PSScriptRoot 'console.ps1'
        $usbResult = Invoke-ToolkitNative -File 'powershell.exe' -Arguments @('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $usbScript, '-UsbOnly', '-Distro', $Distro, '-ConsoleHelper', $consoleHelper) -TimeoutSeconds 180 -AllowWait
        if ($usbResult.ExitCode -ne 0) {
            $error = New-Object Exception "USB handoff failed or administrator approval was declined. No GUI was started. $($usbResult.StartError) $($usbResult.Output -join ' ')"
            $error.Data['ToolkitExitCode'] = $usbResult.ExitCode
            if ($usbResult.TimedOut) { $error.Data['ToolkitStall'] = $true }
            throw $error
        }
        $permissions = Start-WslUsbAccess -Name $Distro -User $linuxUser -Uid $linuxUid -Gid $linuxGid -Helper "$target/wsl-usb-permissions.sh" -Session $session
        if ($permissions.WaitForExit(200)) { throw 'The WSL USB permission watcher failed to start.' }
        $consoleHelper = Join-Path $PSScriptRoot 'console.ps1'
        $monitorArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -UsbOnly -MonitorUsb -Distro "{1}" -ConsoleHelper "{2}"' -f $usbScript, $Distro, $consoleHelper
        $monitor = Start-ToolkitProcess -FilePath powershell.exe -ArgumentList $monitorArgs
        Set-ToolkitStep 'Opening the toolkit'
        $code = Invoke-WslGui -Name $Distro -User $linuxUser -Target $target -Session $session -Permissions $permissions -Monitor $monitor -ShowConsole:$ShowConsole -ReadyTimeoutSeconds $script:ToolkitPolicy.Seconds
        if ($code -ne 0) { $launcherExitCode = $code; Complete-ToolkitStep -Result failed -ExitCode $code -ErrorText "GUI or USB support exited with code $code." }
    } catch {
        $failureCode = if ($_.Exception.Data['ToolkitExitCode']) { [int]$_.Exception.Data['ToolkitExitCode'] } else { 1 }
        Complete-ToolkitStep -Result failed -ExitCode $failureCode -ErrorText $_.Exception.Message
        throw
    } finally {
        # Removing the user-owned session makes the root watcher exit itself.
        Set-ToolkitStep 'Cleaning up USB support'
        $cleanupError = $null
        try { Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'rm', '-f', '--', $session) -TimeoutSeconds 30 | Out-Null }
        catch { $cleanupError = $_ }
        if ($permissions -and -not $permissions.HasExited) {
            if (-not $permissions.WaitForExit(2000)) { Stop-Process -Id $permissions.Id; $null = $permissions.WaitForExit(5000) }
        }
        if ($monitor -and -not $monitor.HasExited) { Stop-Process -Id $monitor.Id; $null = $monitor.WaitForExit(5000) }
        if ($cleanupError -and -not $script:ToolkitFailedStep) { throw $cleanupError }
        if ($cleanupError) {
            Write-ToolkitLog 'cleanup-failed' @{ error=$cleanupError.Exception.Message }
            Complete-ToolkitStep -Result failed -ExitCode 1 -ErrorText $cleanupError.Exception.Message
        } else { Complete-ToolkitStep }
    }
    if ($code -ne 0) { throw "GUI or USB support exited with code $code." }
    Complete-ToolkitStep
    Write-ToolkitLog 'setup-end' @{ result='ok'; exit_code=0 }
    exit $code
} catch {
    $failure = $_.Exception
    $failureCode = if ($failure.Data['ToolkitExitCode']) { [int]$failure.Data['ToolkitExitCode'] } else { $launcherExitCode }
    Complete-ToolkitStep -Result failed -ExitCode $failureCode -ErrorText $failure.Message
    Write-ToolkitLog 'setup-end' @{ result='failed'; exit_code=$failureCode; step=$script:ToolkitFailedStep; error=$failure.Message; build=$script:ToolkitBuild; windows=$script:ToolkitWindowsVersion; wsl=$script:ToolkitWslVersion }
    if (Get-Command Show-ToolkitFailure -ErrorAction SilentlyContinue) { Show-ToolkitFailure $failure.Message }
    else { Write-Host "Launcher: $($failure.Message)" -ForegroundColor Red }
    exit $launcherExitCode
} finally {
    Stop-ToolkitSetup
}

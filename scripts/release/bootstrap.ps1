[CmdletBinding()]
param([string]$PayloadRoot = $PSScriptRoot, [string]$Distro, [switch]$SelfCheck)
$ErrorActionPreference = 'Stop'

function Confirm-Setup([string]$Question) {
    return ((Read-Host "$Question [y/N]") -match '^(?i:y|yes)$')
}
function Invoke-ApprovedAdmin([string]$File, [string]$Arguments) {
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -Verb RunAs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Setup exited with code $($p.ExitCode). Restart this app after any requested reboot." }
}
function Native([string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lines = @(& wsl.exe @Arguments 2>&1); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "WSL failed: $($lines -join ' ')" }
    return (($lines | ForEach-Object { $_.ToString() -replace "`0", '' }) -join "`n").Trim()
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
    return (Start-Process -FilePath $wsl -ArgumentList $line -NoNewWindow -PassThru)
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
        Write-Host 'Downloading the official Ubuntu 22.04 image for the private toolkit Linux...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $download -UseBasicParsing
        if ((Get-Item -LiteralPath $download).Length -ne $bytes) { throw 'Ubuntu image size mismatch. Nothing was registered.' }
        if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ine $sha256) { throw 'Ubuntu image checksum mismatch. Nothing was registered.' }
        Write-Host 'Verified Ubuntu image. Setting up the private Linux (no account or password prompts)...'
        # Microsoft's --import deliberately sets NO_OOBE. It works with this gzip
        # tar image and avoids the newer --install --from-file first-run wizard.
        # Registration belongs to this Windows user; never elevate wsl.exe here.
        Native -Arguments @('--import', $name, $install, $download, '--version', '2') | Out-Host
        $imported = $true
        $linuxProvision = Native -Arguments @('--distribution', $name, '--user', 'root', '--exec', 'wslpath', '-u', '--', $provision)
        Native -Arguments @('--distribution', $name, '--user', 'root', '--exec', 'bash', $linuxProvision) | Out-Host
        Native -Arguments @('--terminate', $name) | Out-Null
        if ((Native -Arguments @('--distribution', $name, '--exec', 'id', '-un')) -cne 'jibo') { throw 'Private Linux did not start as the toolkit user.' }
        if (-not (Get-DistributionRuntime $name -RequireUbuntu)) { throw 'Private Linux compatibility check failed.' }
        Write-Host 'Private Linux is ready. Future launches need no setup download.'
        return $name
    } catch {
        # Only a distro successfully imported by THIS attempt may be removed.
        # Never unregister an existing distro or one whose import failed/conflicted.
        if ($imported) {
            try { Native -Arguments @('--unregister', $name) | Out-Null }
            catch { Write-Warning 'Could not remove the failed private Linux. Run: wsl --unregister Jibo-DFU-Toolkit' }
        }
        throw
    } finally {
        if (Test-Path $download) { Remove-Item -LiteralPath $download -Force }
    }
}
function Invoke-WslGui([string]$Name, [string]$User, [string]$Target, [string]$Session) {
    # Never enter AppRun's Linux sudo/polkit branch from the Windows EXE.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & wsl.exe --distribution $Name --user $User --exec bash "$Target/wsl-user-launch.sh" "$Target/jibo-dfu" $Session 2>&1 | Out-Host
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
    return $code
}
function Test-CompatibleDistribution([string]$Name) {
    try {
        $libc = Native -Arguments @('--distribution', $Name, '--exec', 'getconf', 'GNU_LIBC_VERSION')
        if ($libc -notmatch '^glibc\s+(\d+\.\d+)$') { return $false }
        return ([version]$Matches[1] -ge [version]'2.35')
    } catch { return $false }
}
function Get-DistributionRuntime([string]$Name, [switch]$RequireUbuntu) {
    # No shell interpolation: read distro metadata as data, not executable code.
    if (-not (Test-WslDistributionName $Name)) { return $null }
    try {
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
    } catch { return $null }
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
    $archive = Join-Path $PayloadRoot 'payload.tar.gz'
    $usbScript = Join-Path $PayloadRoot 'run.ps1'
    if (-not (Test-Path $archive) -or -not (Test-Path $usbScript)) { throw 'Embedded payload is incomplete.' }
    if ($SelfCheck) { Write-Output 'Bootstrap scripts and Linux payload are present; WSL setup not attempted.'; exit 0 }
    Write-Host 'Jibo DFU Toolkit: Windows uses WSL 2 + WSLg and usbipd-win. Setup requires internet only if these are missing.'
    $ready = $false
    try { $version = Native -Arguments @('--version'); $ready = ($version -match 'WSLg') } catch { $ready = $false }
    if (-not $ready) {
        if (-not (Confirm-Setup 'Install/update WSL 2 and WSLg? Windows will ask for administrator approval.')) { throw 'WSL setup declined. No setup changes were made.' }
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--install --no-distribution'
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--update'
        throw 'WSL setup requested. Reboot if Windows requests it, then run this download again.'
    }
    $rows = @()
    try { $listing = Native -Arguments @('--list', '--verbose') }
    catch { $listing = '' }
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
    $revision = (Get-Content (Join-Path $PayloadRoot 'revision.txt') -Raw).Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw 'Invalid embedded revision.' }
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
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', "$target/jibo-dfu", '--self-check') | Out-Null
    Write-Host 'Toolkit self-check passed.'
    $session = "$target/.usb-$([guid]::NewGuid().ToString('N'))"
    $permissions = $null
    $monitor = $null
    try {
        # Existing validated bind/attach/reattach logic retains usbipd's UAC prompt.
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $usbScript -UsbOnly -Distro $Distro
        if ($LASTEXITCODE -ne 0) { throw 'USB handoff failed or administrator approval was declined. No GUI was started.' }
        $permissions = Start-WslUsbAccess -Name $Distro -User $linuxUser -Uid $linuxUid -Gid $linuxGid -Helper "$target/wsl-usb-permissions.sh" -Session $session
        if ($permissions.WaitForExit(200)) { throw 'The WSL USB permission watcher failed to start.' }
        $monitorArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -UsbOnly -MonitorUsb -Distro "{1}"' -f $usbScript, $Distro
        $monitor = Start-Process powershell.exe -ArgumentList $monitorArgs -NoNewWindow -PassThru
        $code = Invoke-WslGui -Name $Distro -User $linuxUser -Target $target -Session $session
    } finally {
        # Removing the user-owned session makes the root watcher exit itself.
        try { Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'rm', '-f', '--', $session) | Out-Null }
        catch { Write-Warning 'Could not remove the USB session; the watcher also checks the GUI process lifetime.' }
        if ($permissions -and -not $permissions.HasExited) {
            if (-not $permissions.WaitForExit(2000)) { Stop-Process -Id $permissions.Id; $permissions.WaitForExit() }
        }
        if ($monitor -and -not $monitor.HasExited) { Stop-Process -Id $monitor.Id; $monitor.WaitForExit() }
    }
    exit $code
} catch {
    Write-Host "Launcher: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}

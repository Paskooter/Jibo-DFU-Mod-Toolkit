"""First-open display checks: no real WSL, USB, compositor changes or network."""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / 'scripts/release/wsl-user-launch.sh'


@pytest.mark.parametrize('log,expected', [
    ('Command line: weston\nRDP backend: use_gfxredir = 0\n', 'copy'),
    ('Command line: weston\nRDP backend: use_gfxredir = 1\n', 'shared'),
    ('Command line: old\nRDP backend: use_gfxredir = 1\nCommand line: new\n', 'starting'),
    ('Command line: old\nRDP backend: use_gfxredir = 0\nCommand line: new\nRDP backend: use_gfxredir = 1\n', 'shared'),
    ('Command line: weston\r\nRDP backend: use_gfxredir = 0\r\n', 'copy'),
    ('unrecognized log\n', 'starting'),
    (None, 'missing'),
])
def test_display_mode_reads_only_the_current_weston_session(tmp_path, log, expected):
    path = tmp_path / 'weston.log'
    if log is not None:
        path.write_bytes(log.encode())
    result = subprocess.run(['bash', str(WRAPPER), 'display-mode', str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


EIO = ('rdp_allocate_shared_memory: Failed to open "/mnt/shared_memory/{1fca67eb-6440-4488-96b2-4ef24a36d96c}" '
       'with error: Input/output error\n')
START = 'Command line: /usr/bin/weston --backend=rdp-backend.so --xwayland --log=/mnt/wslg/weston.log\n'


@pytest.mark.parametrize('log,expected', [
    # The current session's shared-memory probe hit EIO (openvmm#4274).
    (START + EIO + 'RDP backend: use_gfxredir = 0\n', 'mode=copy sessions=1 eio=1'),
    # Copy mode for another reason (no EIO line): reported, but not as EIO.
    (START + 'RDP backend: use_gfxredir = 0\n', 'mode=copy sessions=1 eio=0'),
    (START + 'RDP backend: use_gfxredir = 1\n', 'mode=shared sessions=1 eio=0'),
    # After a restart: a new session is counted and the old EIO no longer applies.
    (START + EIO + 'RDP backend: use_gfxredir = 0\n' + START + 'RDP backend: use_gfxredir = 1\n', 'mode=shared sessions=2 eio=0'),
    # Restarted weston that has not finished starting yet.
    (START + EIO + 'RDP backend: use_gfxredir = 0\n' + START, 'mode=starting sessions=2 eio=0'),
    # Restarted weston that is still broken.
    (START + 'RDP backend: use_gfxredir = 1\n' + START + EIO + 'RDP backend: use_gfxredir = 0\n', 'mode=copy sessions=2 eio=1'),
    ((START + EIO + 'RDP backend: use_gfxredir = 0\n').replace('\n', '\r\n'), 'mode=copy sessions=1 eio=1'),
    ('unrecognized log\n', 'mode=starting sessions=0 eio=0'),
    ('', 'mode=starting sessions=0 eio=0'),
    (None, 'mode=missing sessions=0 eio=0'),
])
def test_display_detail_counts_sessions_and_the_shared_memory_eio(tmp_path, log, expected):
    path = tmp_path / 'weston.log'
    if log is not None:
        path.write_bytes(log.encode())
    result = subprocess.run(['bash', str(WRAPPER), 'display-detail', str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected + '\n'


DISPLAY_SCENARIOS = ['display-repair-ok', 'display-repair-still-copy', 'display-repair-command-failed', 'display-repair-hang',
                     'display-repair-never-back', 'display-detail-broken', 'display-copy-user-distro']


def test_display_scenarios_are_all_in_the_bootstrap_inventory():
    harness = (ROOT / 'scripts/release/test-bootstrap.ps1').read_text()
    inventory = re.search(r"foreach \(\$case in @\(([^)]*)\) \| Where-Object", harness).group(1)
    names = re.findall(r"'([^']+)'", inventory)
    assert [n for n in names if n.startswith('display-')] == DISPLAY_SCENARIOS
    assert len(names) == len(set(names)) == 62


@pytest.mark.parametrize('scenario', DISPLAY_SCENARIOS)
def test_bootstrap_display_fix_scenarios(tmp_path, scenario):
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    result = subprocess.run([pwsh, '-NoProfile', '-File', str(ROOT / 'scripts/release/test-bootstrap.ps1'), '-Scenario', scenario],
                            capture_output=True, text=True, timeout=150, env={**os.environ, 'TMPDIR': str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS scenario: ' + scenario in result.stdout and 'Bootstrap scenarios: 1 passed; 0 failed' in result.stdout


def test_display_helpers_stay_read_only():
    # The repair runs from Windows in the system distro; this helper only reads.
    text = WRAPPER.read_text()
    head = text.split('[[ ( $# == 2 || $# == 3 )')[0]
    code = '\n'.join(line for line in head.splitlines() if not line.lstrip().startswith('#'))
    for forbidden in ('pkill', 'kill ', 'umount', '--shutdown', '--terminate', 'wsl.exe', 'rm ', '>'):
        assert forbidden not in code, forbidden


def run_gui_probe(tmp_path, body):
    pwsh = os.environ.get('PWSH')
    if not pwsh:
        pytest.skip('requires PowerShell')
    script = tmp_path / 'probe.ps1'
    script.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + str(ROOT / 'scripts/release/bootstrap.ps1') + r"""',[ref]$tokens,[ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text }
$script:started=0; $script:calls=@(); $script:steps=@(); $script:sessions=5; $script:mode='copy'
function Native { param($Arguments); return '/tmp/ready' }
function Write-ToolkitLog { param($Event,$Data) }
function Complete-ToolkitStep { param($Result,$ExitCode,$ErrorText) }
function Set-ToolkitStep { param($Name); $script:steps += $Name }
function Start-ToolkitProcess { $script:started++; throw 'GUI_START' }
""" + body)
    result = subprocess.run([pwsh, '-NoProfile', '-File', str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


REPAIR = "--distribution|Jibo-DFU-Toolkit|--system|--user|root|--exec|pkill|-x|weston"


@pytest.mark.parametrize('outcome', ['shared', 'copy', 'failed', 'never'])
def test_private_copy_display_is_repaired_once_then_opens_or_refuses(tmp_path, outcome):
    out = run_gui_probe(tmp_path, r"""
function Invoke-ToolkitNative {
    param($File,$Arguments,$TimeoutSeconds)
    $script:calls += ,@($Arguments)
    if ($Arguments -contains 'display-mode') { return [pscustomobject]@{ExitCode=0;Output=@($script:mode)} }
    if ($Arguments -contains 'display-detail') { return [pscustomobject]@{ExitCode=0;Output=@("mode=$($script:mode) sessions=$($script:sessions) eio=1")} }
    if ($Arguments -contains '--system') {
        if ($TimeoutSeconds -le 0 -or $TimeoutSeconds -gt 15) { throw 'UNBOUNDED_REPAIR' }
        switch ('""" + outcome + r"""') { 'shared' { $script:sessions++; $script:mode='shared' }; 'copy' { $script:sessions++ }; 'failed' { return [pscustomobject]@{ExitCode=1;Output=@('no')} } }
        return [pscustomobject]@{ExitCode=0;Output=@()}
    }
    throw ('UNEXPECTED ' + ($Arguments -join ' '))
}
$message=''
try { Invoke-WslGui -Name Jibo-DFU-Toolkit -User jibo -Target /tmp/toolkit -Session /tmp/session -ReadyTimeoutSeconds 0.1 -DisplayRepairWaitSeconds 1 }
catch { $message=$_.Exception.Message }
$repairs=@($script:calls | Where-Object { $_ -contains '--system' })
"repairs=$($repairs.Count)"
"argv=$(($repairs | ForEach-Object { $_ -join '|' }) -join ';')"
"started=$script:started"
"steps=$($script:steps -join ',')"
"forbidden=$(@($script:calls | Where-Object { $_ -contains '--shutdown' -or $_ -contains '--terminate' }).Count)"
"message=$message"
""")
    lines = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    assert lines['repairs'] == '1' and lines['argv'] == REPAIR and lines['forbidden'] == '0'
    if outcome == 'shared':
        assert lines['started'] == '1' and lines['message'] == 'GUI_START'
        assert lines['steps'] == 'Fixing a Windows display problem,Opening the toolkit'
    else:
        assert lines['started'] == '0'
        assert lines['steps'] == 'Fixing a Windows display problem'
        assert lines['message'].startswith('Windows display is not ready (WARN:COPY MODE), and the automatic fix didn')
        assert 'wsl --shutdown' in lines['message'] and 'restart Windows' not in lines['message']


def test_display_repair_refuses_any_other_distribution(tmp_path):
    out = run_gui_probe(tmp_path, r"""
function Invoke-ToolkitNative { param($File,$Arguments,$TimeoutSeconds); $script:calls += ,@($Arguments); return [pscustomobject]@{ExitCode=1;Output=@('no')} }
foreach ($name in @('Ubuntu-22.04','jibo-dfu-toolkit','Jibo-DFU-Toolkit-2','docker-desktop')) {
    try { Repair-ToolkitDisplay -Name $name -User jibo -Target /tmp/t -WaitSeconds 1 | Out-Null; 'ACCEPTED ' + $name }
    catch { if ($_.Exception.Message -ceq 'Display repair is only allowed for the toolkit''s private Linux.') { 'refused ' + $name } else { 'OTHER ' + $_.Exception.Message } }
}
"calls=$($script:calls.Count)"
# Positive control: the toolkit's own name reaches wsl.exe (here: detail read fails, no repair).
"own=$(Repair-ToolkitDisplay -Name Jibo-DFU-Toolkit -User jibo -Target /tmp/t -WaitSeconds 1) calls=$($script:calls.Count)"
""")
    assert 'ACCEPTED' not in out and 'OTHER' not in out and out.count('refused ') == 4
    assert 'calls=0\n' in out and 'own=False calls=1' in out


def test_copy_display_is_refused_before_starting_the_gui(tmp_path):
    pwsh = os.environ.get('PWSH')
    if not pwsh:
        pytest.skip('requires PowerShell')
    script = tmp_path / 'probe.ps1'
    script.write_text(r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + str(ROOT / 'scripts/release/bootstrap.ps1') + r"""',[ref]$tokens,[ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text }
$script:started=0
function Native { param($Arguments); return '/tmp/ready' }
function Invoke-ToolkitNative { param($File,$Arguments,$TimeoutSeconds); return [pscustomobject]@{ExitCode=0;Output=@('copy')} }
function Write-ToolkitLog { param($Event,$Data) }
function Complete-ToolkitStep { param($Result,$ExitCode,$ErrorText) }
function Start-ToolkitProcess { $script:started++; throw 'FORBIDDEN_GUI_START' }
$refused=$false
try { Invoke-WslGui -Name Ubuntu -User jibo -Target /tmp/toolkit -Session /tmp/session -ReadyTimeoutSeconds 0.1 }
catch { if ($_.Exception.Message -notmatch '^Windows display is not ready') { throw }; $refused=$true }
if (-not $refused -or $script:started -ne 0) { throw 'Copy-mode display admitted a GUI' }
'PASS copy display refused before GUI'
""")
    result = subprocess.run([pwsh, '-NoProfile', '-File', str(script)], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS copy display refused before GUI' in result.stdout

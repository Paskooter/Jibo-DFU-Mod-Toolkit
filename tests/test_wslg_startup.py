"""First-open display checks: no real WSL, USB, compositor changes or network."""
import os
from pathlib import Path
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

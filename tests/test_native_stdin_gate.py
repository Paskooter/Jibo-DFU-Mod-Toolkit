"""Byte-level native harness gate controls; not native Windows acceptance."""
import base64
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')


@pytest.mark.parametrize('script,function,variable,expected', [
    ('test-usbipd-native.ps1', 'Invoke-OwnedPhase', 'process', b'outer-job:fixture\n'),
    ('test-support-native.ps1', 'Invoke-SupportChild', 'child', b'fixture\n'),
])
def test_gate_sender_is_exact_with_framework_utf8_preamble(tmp_path, script, function, variable, expected):
    """Run the actual sender with a BOM-producing .NET Framework-style writer.

    Console.In does not strip a UTF-8 BOM. A StreamWriter.WriteLine gate can
    therefore corrupt an ordinal nonce even when the process is contained.
    """
    if not PWSH:
        pytest.skip('PowerShell required for byte-level sender control')
    source = ROOT / 'scripts/release' / script
    driver = tmp_path / 'gate.ps1'
    driver.write_text(r'''
param($Source,$Function,$Variable)
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$null,[ref]$null)
$f=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $Function},$true))[0]
# Only the sender statements, verbatim; no process/job/fixture is executed.
$sender=@($f.Extent.Text -split '\r?\n' | Where-Object {
    $_ -match '^\s*\$gateBytes\s*=' -or
    $_ -match '^\s*\$(process|child)\.StandardInput\.(WriteLine|BaseStream\.(Write|Flush))\('
}) -join "`n"
if (-not $sender) { throw 'Gate sender statements absent' }
$stream=New-Object IO.MemoryStream
$writer=New-Object IO.StreamWriter -ArgumentList @($stream,(New-Object Text.UTF8Encoding($true)),1024,$true)
$RunId='fixture'; $Nonce='fixture'
Set-Variable -Name $Variable -Value ([pscustomobject]@{StandardInput=$writer})
& ([scriptblock]::Create($sender))
[Convert]::ToBase64String($stream.ToArray())
$writer.Dispose(); $stream.Dispose()
''')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver), str(source), function, variable],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = base64.b64decode(result.stdout.strip())
    assert actual == expected, f'Gate bytes contain an encoding preamble or altered nonce: {actual!r}'

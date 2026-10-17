"""Binding-order and harmless Check controls; NOT native Windows acceptance."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(not PWSH, reason='already installed PowerShell required; no downloads')
RUNNERS = [('support', 'SourceRoot'), ('usbipd', 'ToolkitRoot')]
INPUTS = {
    'support': ['scripts/release/console.ps1', 'tests/test_windows_support_diagnostics.py',
                'tests/fixtures/support_clipboard_faults.cs', 'tests/fixtures/support_failure_controller.cs',
                'tests/fixtures/support_failure_proxy.cs', 'tests/fixtures/support_failure_scenarios.ps1',
                'scripts/release/windows_launcher.py', '.github/workflows/release.yml'],
    'usbipd': ['usbipd.ps1', 'run.ps1', 'scripts/release/bootstrap.ps1',
               'scripts/release/console.ps1', 'scripts/release/test-bootstrap.ps1'],
}


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def invoke(tmp_path, *args):
    assert PWSH is not None
    return subprocess.run([PWSH, '-NoProfile', '-NonInteractive', *args], cwd=tmp_path,
                          text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize('name,parameter', RUNNERS)
def test_root_parameter_binds_without_script_context(tmp_path, name, parameter):
    runner = ROOT / f'scripts/release/test-{name}-native.ps1'
    # Evaluate the ACTUAL param block without a file context. This deterministically
    # catches the early Split-Path failure; PSCore Check alone masks the PS5.1 RED.
    result = invoke(tmp_path, '-Command', """
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile(TARGET,[ref]$null,[ref]$null)
$probe=[scriptblock]::Create($ast.ParamBlock.Extent.Text + '\n' +
    'if ($PSScriptRoot) { throw "Probe unexpectedly has script context" }; "BOUND:" + $PARAMETER')
& $probe -Mode Check
""".replace('TARGET', quote(runner)).replace('PARAMETER', parameter))
    assert result.returncode == 0, 'ROOT-PARAMETER-BINDING: ' + result.stdout + result.stderr
    assert result.stdout.strip() == 'BOUND:'


@pytest.mark.parametrize('name,parameter', RUNNERS)
@pytest.mark.parametrize('relative', [False, True], ids=['absolute-script', 'relative-script'])
@pytest.mark.parametrize('explicit', [False, True], ids=['omitted-root', 'explicit-root'])
def test_check_resolves_root_after_binding_from_changed_cwd(tmp_path, name, parameter, relative, explicit):
    candidate = ROOT
    if explicit:
        candidate = tmp_path / 'explicit source with spaces'
        for path in INPUTS[name]:
            target = candidate / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        # Distinct harmless bytes prove the caller's root is used, not repaired.
        with (candidate / INPUTS[name][0]).open('a') as stream:
            stream.write('\n# Explicit root fingerprint control.\n')
    runner = ROOT / f'scripts/release/test-{name}-native.ps1'
    script = os.path.relpath(runner, tmp_path) if relative else str(runner)
    args = ['-File', script, '-Mode', 'Check']
    if explicit:
        args += ['-' + parameter, str(candidate)]
    result = invoke(tmp_path, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    sources = json.JSONDecoder().raw_decode(result.stdout.lstrip())[0]
    for path in INPUTS[name]:
        assert sources[path] == hashlib.sha256((candidate / path).read_bytes()).hexdigest()
    assert not (tmp_path / f'{name}-native-receipts').exists()


@pytest.mark.parametrize('name,parameter', RUNNERS)
@pytest.mark.parametrize('bad', ['empty', 'whitespace', 'missing', 'file'])
def test_explicit_malformed_root_is_not_replaced(tmp_path, name, parameter, bad):
    file = tmp_path / 'not a directory'
    file.write_text('owned non-root control')
    value = {'empty': '', 'whitespace': ' ', 'missing': str(tmp_path / 'missing'), 'file': str(file)}[bad]
    runner = ROOT / f'scripts/release/test-{name}-native.ps1'
    result = invoke(tmp_path, '-Command', '& ' + quote(runner) + ' -Mode Check -' + parameter + ' ' + quote(value))
    assert result.returncode == 1, 'MALFORMED-ROOT-WAS-REPAIRED: ' + result.stdout + result.stderr
    if bad == 'empty':
        assert 'empty string' in result.stderr
    else:
        reason = 'Required integrated input absent:' if name == 'support' else 'Required integrated candidate source absent:'
        assert reason in result.stderr
    assert not (tmp_path / f'{name}-native-receipts').exists()


@pytest.mark.parametrize('explicit', [False, True], ids=['omitted-root', 'explicit-root'])
def test_support_dot_source_keeps_default_and_explicit_root(tmp_path, explicit):
    runner = ROOT / 'scripts/release/test-support-native.ps1'
    expected = tmp_path / 'explicit dot source root' if explicit else ROOT
    command = '. ' + quote(runner)
    if explicit:
        command += ' -SourceRoot ' + quote(expected)
    result = invoke(tmp_path, '-Command', command + '; "ROOT:" + $SourceRoot')
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'ROOT:' + str(expected)

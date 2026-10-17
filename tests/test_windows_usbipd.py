"""Safe portable OS-boundary checks. No real WSL, installers, USB or network.

Bootstrap's existing test-bootstrap.ps1 traces exact child arguments separately;
these execute run.ps1's actual USB-only and monitor entries in real processes.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')
PORTABLE = pytest.mark.skipif(not PWSH or os.name == 'nt', reason='portable Linux shell OS fixtures')


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(tmp_path, text, env=None, timeout=20):
    assert PWSH is not None
    script = tmp_path / 'probe.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n" + text)
    return subprocess.run([PWSH, '-NoProfile', '-File', str(script)],
                          env={**os.environ, 'TMPDIR': str(tmp_path), **(env or {})},
                          capture_output=True, text=True, timeout=timeout)


def fake_usb(tmp_path):
    folder = tmp_path / 'Program Files' / 'usbipd-win'
    folder.mkdir(parents=True)
    binary = folder / 'usbipd.exe'
    binary.write_text('''#!/bin/sh
printf '%s\t%s\n' "$0" "$*" >> "$FIXTURE_TRACE"
case "$1" in
 --version)
  case "$FIXTURE_MODE" in
   broken) printf 'token=fixture-secret\n'; exit 17;;
   malformed) printf 'not a usbipd version\n'; exit 0;;
   hang) exec /bin/sleep 30;;
  esac
  printf '5.3.0+fixture\n';;
 state)
  [ "$#" = 1 ] || exit 90
  printf '%s\n' '{"Devices":[{"BusId":"1-2","InstanceId":"USB\\\\VID_0955&PID_7740\\\\FIXTURE","Description":"Synthetic Jibo","PersistedGuid":"fixture","ClientIPAddress":null}]}' ;;
 attach)
  [ "$*" = 'attach --wsl Ubuntu-22.04 --busid 1-2' ] || exit 91;;
 *) exit 92;;
esac
''')
    binary.chmod(0o755)
    return binary


@PORTABLE
@pytest.mark.parametrize('mode', ['usb-only', 'monitor', 'source-monitor', 'manual'])
def test_real_run_entries_use_exact_resolved_executable(tmp_path, mode):
    binary = fake_usb(tmp_path)
    trace = tmp_path / 'native.log'
    fake_wsl = tmp_path / 'wsl.exe'
    fake_wsl.write_text('''#!/bin/sh
case "$*" in
 '--list --verbose') printf 'NAME STATE VERSION\n* Ubuntu-22.04 Stopped 2\n';;
 *wslpath*) printf '/fixture/repo\n';;
esac
''')
    fake_wsl.chmod(0o755)
    helper = tmp_path / 'console.ps1'
    helper.write_text('''function Invoke-ToolkitNative {
 param($File,[string[]]$Arguments)
 if ($File -cne $env:FIXTURE_USB -and $File -cne $env:FIXTURE_WSL) { throw 'Forbidden external executable' }
 $output = @(& $File @Arguments)
 return [pscustomobject]@{Output=$output;ExitCode=$LASTEXITCODE;StartError=$null}
}
''')
    # Source's legacy capture also invokes only these exact shell fixtures.
    params = "-Distro Ubuntu-22.04 -UsbipdPath " + psquote(binary)
    if mode in ('usb-only', 'monitor'):
        params += ' -UsbOnly -ConsoleHelper ' + psquote(helper)
    if mode == 'monitor':
        params += ' -MonitorUsb'
    if mode == 'manual':
        params += ' -ManualUsb'
    result = run_ps(tmp_path, '''
function Get-Command { param($Name,$ErrorAction,$CommandType)
 if ($Name -eq 'wsl.exe') { return [pscustomobject]@{Source=$env:FIXTURE_WSL} }
 throw 'Unexpected PATH lookup: explicit handoff must not switch executable'
}
$global:ticks=0
function Start-Sleep { param($Milliseconds); $global:ticks++; if ($global:ticks -ge 2) { exit 0 } }
function Start-Process { param($FilePath,$ArgumentList,[switch]$NoNewWindow,[switch]$PassThru)
 [IO.File]::WriteAllText($env:FIXTURE_MONITOR,$ArgumentList)
 return [pscustomobject]@{HasExited=$true}
}
& RUN PARAMS
exit $LASTEXITCODE
'''.replace('RUN', psquote(ROOT / 'run.ps1')).replace('PARAMS', params),
        {'FIXTURE_USB': str(binary), 'FIXTURE_WSL': str(fake_wsl),
         'FIXTURE_TRACE': str(trace), 'FIXTURE_MONITOR': str(tmp_path / 'monitor.txt')})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line.split('\t', 1) for line in trace.read_text().splitlines()]
    assert all(path == str(binary) for path, _ in calls)
    assert calls[0][1] == '--version'
    assert sum(args == 'state' for _, args in calls) == (2 if mode == 'monitor' else 1)
    assert sum(args.startswith('attach ') for _, args in calls) == (0 if mode == 'manual' else 2 if mode == 'monitor' else 1)
    if mode == 'source-monitor':
        assert f'-UsbipdPath "{binary}"' in (tmp_path / 'monitor.txt').read_text()
    if mode == 'manual':
        assert not (tmp_path / 'monitor.txt').exists()


@PORTABLE
@pytest.mark.parametrize('mode', ['missing', 'broken', 'malformed', 'directory', 'relative', 'injection', 'unc'])
def test_invalid_explicit_handoff_never_reaches_usb_state(tmp_path, mode):
    binary = fake_usb(tmp_path)
    trace = tmp_path / 'native.log'
    path = {'missing': str(binary) + '.missing', 'directory': str(binary.parent),
            'relative': 'usbipd.exe', 'injection': str(binary) + '";throw 1;#',
            'unc': '\\\\fixture.invalid\\usbipd.exe'}.get(mode, str(binary))
    text = '''
function Get-Command { param($Name,$ErrorAction,$CommandType); if ($Name -eq 'wsl.exe') { return [pscustomobject]@{Source='fixture-wsl'} }; throw 'Fallback after bad explicit path' }
function Invoke-ToolkitNative { param($File,[string[]]$Arguments); [IO.File]::AppendAllText($env:FIXTURE_BOUNDARY,($File + '|' + ($Arguments -join ' ') + "`n")); if ($File -ne 'fixture-wsl' -or ($Arguments -join ' ') -ne '--list --verbose') { throw 'Forbidden external operation' }; return [pscustomobject]@{ExitCode=0;Output=@('NAME STATE VERSION','* Ubuntu-22.04 Stopped 2')} }
& RUN -UsbOnly -Distro Ubuntu-22.04 -ConsoleHelper HELPER -UsbipdPath PATH
exit $LASTEXITCODE
'''
    helper = tmp_path / 'console.ps1'
    helper.write_text('')
    result = run_ps(tmp_path, text.replace('RUN', psquote(ROOT/'run.ps1')).replace('HELPER', psquote(helper)).replace('PATH', psquote(path)),
                    {'FIXTURE_MODE': mode, 'FIXTURE_TRACE': str(trace), 'FIXTURE_BOUNDARY': str(tmp_path/'boundary.log')})
    assert result.returncode == 2, result.stdout + result.stderr
    assert not trace.exists() or all(line.endswith('\t--version') for line in trace.read_text().splitlines())
    assert 'fixture-secret' not in result.stdout + result.stderr
    assert (tmp_path/'boundary.log').read_text().splitlines() == ['fixture-wsl|--list --verbose']


@PORTABLE
@pytest.mark.parametrize('source', ['path-exe', 'path-command', 'registry', 'persisted-machine', 'persisted-user', 'normal-location'])
def test_shared_resolver_checks_real_cli_not_inventory(tmp_path, source):
    binary = fake_usb(tmp_path)
    text = '''
. HELPER
function Get-Command { param($Name,$CommandType,$ErrorAction)
 if ($CommandType -ne 'Application') { throw 'Aliases/functions must not count as installed binaries' }
 if (($env:FIXTURE_SOURCE -eq 'path-exe' -and $Name -eq 'usbipd.exe') -or ($env:FIXTURE_SOURCE -eq 'path-command' -and $Name -eq 'usbipd')) { return [pscustomobject]@{Source=$env:FIXTURE_USB} }
 return $null
}
function Get-ItemProperty { param($LiteralPath,$Name,$ErrorAction)
 if ($Name -eq 'APPLICATIONFOLDER' -and $env:FIXTURE_SOURCE -eq 'registry') { return [pscustomobject]@{APPLICATIONFOLDER=(Split-Path $env:FIXTURE_USB);UninstallString='DO NOT EXECUTE'} }
 if ($Name -eq 'Path' -and (($env:FIXTURE_SOURCE -eq 'persisted-machine' -and $LiteralPath -like 'HKLM:*') -or ($env:FIXTURE_SOURCE -eq 'persisted-user' -and $LiteralPath -like 'HKCU:*'))) { return [pscustomobject]@{Path=('relative;\\\\fixture.invalid\\bad;"' + (Split-Path $env:FIXTURE_USB) + '"')} }
 return $null
}
$before = $env:PATH
$resolved = Resolve-UsbipdExecutable
if ($resolved -cne $env:FIXTURE_USB -or $env:PATH -cne $before) { throw 'Resolution failed or PATH mutated' }
'''.replace('HELPER', psquote(ROOT/'usbipd.ps1'))
    env = {'FIXTURE_SOURCE': source, 'FIXTURE_USB': str(binary), 'FIXTURE_TRACE': str(tmp_path/'native.log'),
           'ProgramFiles': str(binary.parent.parent) if source == 'normal-location' else str(tmp_path/'absent'),
           'ProgramW6432': str(tmp_path/'absent')}
    result = run_ps(tmp_path, text, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path/'native.log').read_text().splitlines() == [str(binary) + '\t--version']


@PORTABLE
def test_bounded_version_probe_kills_hanging_cli_without_output_leak(tmp_path):
    binary = fake_usb(tmp_path)
    started = time.monotonic()
    result = run_ps(tmp_path, '. ' + psquote(ROOT/'usbipd.ps1') + '\nif (Test-UsbipdExecutable -Path ' + psquote(binary) + ' -TimeoutSeconds 0.2) { throw "Hang accepted" }',
                    {'FIXTURE_MODE': 'hang', 'FIXTURE_TRACE': str(tmp_path/'native.log')}, timeout=8)
    assert result.returncode == 0, result.stdout + result.stderr
    assert time.monotonic() - started < 5
    assert (tmp_path/'native.log').read_text().endswith('\t--version\n')


@PORTABLE
@pytest.mark.parametrize('fresh_shell', [True, False], ids=['fresh-path-true', 'older-exe-stale-path'])
def test_fresh_shell_and_older_inherited_environment_both_find_installed_cli(tmp_path, fresh_shell):
    binary = fake_usb(tmp_path)
    inherited = os.environ.get('PATH', '')
    result = run_ps(tmp_path, '''
. HELPER
function Get-ItemProperty { param($LiteralPath,$Name,$ErrorAction); return $null }
$onPath = [bool](Get-Command usbipd.exe -CommandType Application -ErrorAction SilentlyContinue)
if ($onPath -ne ($env:FIXTURE_FRESH -eq 'yes')) { throw 'PATH fixture did not represent fresh/older process' }
$before=$env:PATH
if ((Resolve-UsbipdExecutable) -cne $env:FIXTURE_USB -or $before -cne $env:PATH) { throw 'Installed CLI not resolved without PATH edit' }
'''.replace('HELPER', psquote(ROOT/'usbipd.ps1')),
        {'PATH': str(binary.parent) + os.pathsep + inherited if fresh_shell else inherited,
         'FIXTURE_FRESH': 'yes' if fresh_shell else 'no', 'FIXTURE_USB': str(binary),
         'FIXTURE_TRACE': str(tmp_path/'native.log'),
         'ProgramFiles': str(binary.parent.parent), 'ProgramW6432': str(binary.parent.parent)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path/'native.log').read_text().splitlines() == [str(binary) + '\t--version']


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_packaging_helper_closure_and_missing_helper_control(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT/'scripts/release'))
    build = load_module('usbipd_build', ROOT/'scripts/release/build.py')
    # Inspect real packaging argv without invoking downloads, PyInstaller or a PE.
    (tmp_path/'payload.tar.gz').write_bytes(b'fixture')
    calls = []
    monkeypatch.setattr(build, 'download', lambda *a: None)
    monkeypatch.setattr(build, 'run', lambda *a, **kw: calls.append(a))
    class StopAtNativeBuild(Exception):
        pass
    def stop_at_native(*a):
        raise StopAtNativeBuild
    monkeypatch.setattr(build, 'verify_windows_executable', stop_at_native)
    with pytest.raises(StopAtNativeBuild):
        build.windows(tmp_path)
    sources = [str(calls[0][i+1]).split(';')[0] for i, value in enumerate(calls[0]) if value == '--add-data']
    assert str(ROOT/'usbipd.ps1') in sources
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    for path in sources:
        path = Path(path)
        if path.is_file():
            shutil.copy2(path, bundle/path.name)
    with tarfile.open(bundle/'payload.tar.gz', 'w:gz') as archive:
        for name in ('jibo-dfu','AppRun','_internal/runtime/licenses/LICENSE','_internal/runtime/licenses/THIRD-PARTY-NOTICES.md'):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
    (bundle/'payload.sha256').write_text(hashlib.sha256((bundle/'payload.tar.gz').read_bytes()).hexdigest())
    (bundle/'revision.txt').write_text('a'*40)
    launcher = load_module('usbipd_launcher', ROOT/'scripts/release/windows_launcher.py')
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    assert launcher._main(['--self-check']) == 0
    assert (bundle/'usbipd.ps1').read_bytes() == (ROOT/'usbipd.ps1').read_bytes()
    (bundle/'usbipd.ps1').unlink()
    with pytest.raises(RuntimeError, match='Missing embedded Windows helper: usbipd.ps1'):
        launcher._main(['--self-check'])

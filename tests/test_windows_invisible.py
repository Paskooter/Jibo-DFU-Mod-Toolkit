"""Windowless Windows release contract, without real WSL or installers."""
import ast
import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
from unittest.mock import patch

import pytest
from test_windows_console import windows_launcher

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_gui_title_identifies_build_without_a_console(tmp_path, monkeypatch):
    import jibo_gui as gui
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'release-manifest.json').write_text(json.dumps({'revision': 'b' * 40}))
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert gui.release_title() == 'Jibo DFU Toolkit (build bbbbbbb)'
    import inspect
    assert 'root.title(release_title())' in inspect.getsource(gui.ToolkitWindow.__init__)


def test_default_bootstrap_is_created_without_a_console(tmp_path, monkeypatch):
    module = windows_launcher(tmp_path, monkeypatch, [])
    calls = []
    monkeypatch.setattr(subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0)
    assert module.main() == 0
    argv, options = calls[0]
    assert options['creationflags'] & 0x08000000
    assert argv[argv.index('-WindowStyle') + 1] == 'Hidden'
    assert '-NonInteractive' in argv


def test_only_show_console_allocates_a_console(tmp_path, monkeypatch):
    module = windows_launcher(tmp_path, monkeypatch, ['-ShowConsole'])
    attached = []
    monkeypatch.setattr(module, 'show_console', lambda: attached.append(True))
    calls = []
    monkeypatch.setattr(subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0)
    assert module.main() == 0
    assert attached == [True]
    assert calls[0][1].get('creationflags', 0) == 0
    assert '-NonInteractive' in calls[0][0]


def test_launcher_error_uses_dialog_never_input(tmp_path, monkeypatch):
    module = windows_launcher(tmp_path, monkeypatch, ['--bogus'])
    dialogs = []
    monkeypatch.setattr(module, 'show_error', lambda message: dialogs.append(message))
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('invisible input wait'))
    assert module.run() == 1
    assert len(dialogs) == 1 and 'Usage:' in dialogs[0]


def test_gui_subsystem_diagnostics_write_to_explicit_output_file(tmp_path, monkeypatch):
    output = tmp_path / 'version.txt'
    module = windows_launcher(tmp_path, monkeypatch, ['--version', '--output', str(output)])
    assert module.main() == 0
    assert output.read_text().splitlines() == [
        'Jibo DFU Toolkit (build aaaaaaa)',
        'Jibo DFU Mod Toolkit Windows x64 WSL bootstrapper']


def test_diagnostic_failure_is_captured_without_a_modal_dialog(tmp_path, monkeypatch):
    output = tmp_path / 'self-check.txt'
    module = windows_launcher(tmp_path, monkeypatch, ['--self-check', '--output', str(output)])
    (tmp_path / 'payload/payload.sha256').write_text('0' * 64)
    monkeypatch.setattr(module, 'show_error', lambda _: pytest.fail('CI diagnostics opened a modal dialog'))
    assert module.run() == 1
    assert 'checksum mismatch' in output.read_text()


def test_windows_build_requires_gui_subsystem_and_diagnostic_receipts():
    source = (ROOT / 'scripts/release/build.py').read_text()
    assert "'--noconsole'" in source
    assert 'verify_windows_executable' in source
    spec = importlib.util.spec_from_file_location('invisible_build', ROOT / 'scripts/release/build.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules):
        sys.path.insert(0, str(ROOT / 'scripts/release'))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
    pe = bytearray(512)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3c, 128)
    pe[128:132] = b'PE\x00\x00'
    struct.pack_into('<H', pe, 128 + 24 + 68, 3)
    path = ROOT / 'unused.exe'
    with patch.object(Path, 'read_bytes', return_value=bytes(pe)):
        with pytest.raises(RuntimeError, match='WINDOWS_GUI'):
            module.verify_windows_executable(path)


def test_default_scripts_have_no_console_wait_or_hide_minimize():
    bootstrap = (ROOT / 'scripts/release/bootstrap.ps1').read_text()
    launcher = (ROOT / 'scripts/release/windows_launcher.py').read_text()
    assert 'Read-Host' not in bootstrap
    assert 'input(' not in launcher
    assert 'Set-ToolkitConsole' not in bootstrap
    assert '-Verb RunAs -WindowStyle Hidden' in bootstrap
    assert '-Verb RunAs -WindowStyle Hidden' in (ROOT / 'run.ps1').read_text()
    assert 'Start-ToolkitProcess' in bootstrap
    assert bootstrap.count('Start-Process') == 2  # RunAs plus the argument-format comment only.
    helper = (ROOT / 'scripts/release/console.ps1').read_text()
    assert helper.count('$info.CreateNoWindow = $true') == 2
    assert 'Start-ToolkitSetup' in bootstrap
    assert 'Stop-ToolkitSetup' in bootstrap

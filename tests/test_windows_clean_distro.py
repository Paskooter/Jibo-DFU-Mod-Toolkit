"""Windows release-only contract; no WSL, network or hardware is used."""
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def launcher():
    spec = importlib.util.spec_from_file_location('clean_windows_launcher', ROOT / 'scripts/release/windows_launcher.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('args', [[], ['--version'], ['--self-check'], ['-Distro', 'NVIDIA-Workbench']])
def test_first_line_identifies_build_before_actions(tmp_path, monkeypatch, capsys, args):
    archive = tmp_path / 'payload.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for name in ('jibo-dfu', 'AppRun', '_internal/runtime/licenses/LICENSE',
                     '_internal/runtime/licenses/THIRD-PARTY-NOTICES.md'):
            entry = tarfile.TarInfo(name)
            entry.size = 1
            output.addfile(entry, io.BytesIO(b'x'))
    (tmp_path / 'payload.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    (tmp_path / 'revision.txt').write_text('a' * 40)
    for name in ('bootstrap.ps1', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh',
                 'wsl-provision.sh', 'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md'):
        (tmp_path / name).write_text('fixture')
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'argv', ['toolkit'] + args)
    calls = []
    def call(values):
        assert capsys.readouterr().out.splitlines()[0] == 'Jibo DFU Toolkit (build aaaaaaa)'
        calls.append(values)
        return 41
    monkeypatch.setattr(subprocess, 'call', call)
    result = launcher().main()
    if args in (['--version'], ['--self-check']):
        assert result == 0
        assert capsys.readouterr().out.splitlines()[0] == 'Jibo DFU Toolkit (build aaaaaaa)'
        assert not calls
    else:
        assert result == 41
        assert calls[0][-len(args):] == args if args else calls[0][-2] == '-PayloadRoot'


def test_provisioning_is_embedded_for_windows_only():
    build = (ROOT / 'scripts/release/build.py').read_text()
    assert "'scripts/release/wsl-provision.sh'" in build.split('def windows(', 1)[1]
    assert 'wsl-provision.sh' in (ROOT / 'scripts/release/windows_launcher.py').read_text()
    script = ROOT / 'scripts/release/wsl-provision.sh'
    assert script.is_file()
    assert subprocess.run(['bash', '-n', str(script)]).returncode == 0

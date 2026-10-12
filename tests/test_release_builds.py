"""Release-only adapters must leave the source launchers and safety flows alone."""
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / 'scripts/release' / (name + '.py')
    assert path.is_file(), 'missing release adapter: ' + name
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_native_pipeline_dispatch_only_accepts_embedded_helper(tmp_path):
    module = load_script('native_launcher')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    helper = runtime / 'jibo_dfu_pipeline.py'
    helper.write_text('')
    assert module.is_pipeline_request([str(helper), '--help'], runtime)
    assert not module.is_pipeline_request(['/tmp/jibo_dfu_pipeline.py'], runtime)
    assert not module.is_pipeline_request(['gui'], runtime)


def test_payload_manifest_detects_corruption(tmp_path):
    module = load_script('manifest')
    (tmp_path / 'tools').mkdir()
    tool = tmp_path / 'tools/dfu-util'
    tool.write_bytes(b'original')
    module.write_manifest(tmp_path, 'test-revision')
    assert module.verify_manifest(tmp_path)['revision'] == 'test-revision'
    tool.write_bytes(b'tampered')
    import pytest
    with pytest.raises(ValueError, match='dfu-util'):
        module.verify_manifest(tmp_path)


def test_release_workflow_is_draft_only_and_has_four_native_targets():
    path = ROOT / '.github/workflows/release.yml'
    assert path.is_file(), 'missing release workflow'
    text = path.read_text()
    for required in ('workflow_dispatch:', "'v*'", 'windows-2022', 'ubuntu-22.04',
                     'macos-15-intel', 'macos-15', '--draft', 'SHA256SUMS'):
        assert required in text
    assert '--latest' not in text


def test_windows_bootstrap_reuses_existing_usb_monitor_without_cloning():
    path = ROOT / 'scripts/release/bootstrap.ps1'
    assert path.is_file(), 'missing Windows bootstrap'
    text = path.read_text()
    for required in ('run.ps1', '-UsbOnly -MonitorUsb', 'RunAs', 'Read-Host',
                     '--version', '/mnt/wslg', 'payload.tar.gz'):
        assert required in text
    assert 'git clone' not in text
    assert 'run.sh' not in text
    import re
    assert not re.search(r'(?i)\$home\s*=', text), 'PowerShell HOME is read-only'


def test_ext4_build_never_installs_host_services():
    module = load_script('build')
    assert hasattr(module, 'configure_flags'), 'missing isolated dependency configuration'
    flags = module.configure_flags('e2fsprogs-1.47.1.tar.gz', Path('/tmp/prefix'))
    assert '--without-udev-rules-dir' in flags
    assert '--without-systemd-unit-dir' in flags
    assert '--without-crond-dir' in flags


def test_native_version_needs_no_desktop_or_tools():
    path = ROOT / 'scripts/release/native_launcher.py'
    assert path.is_file(), 'missing native launcher'
    result = subprocess.run([sys.executable, str(path), '--version'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Jibo DFU Mod Toolkit' in result.stdout

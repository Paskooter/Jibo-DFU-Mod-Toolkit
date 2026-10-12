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


def test_manifest_covers_symlinked_pyinstaller_resource_directories(tmp_path):
    module = load_script('manifest')
    real = tmp_path / 'resources'
    real.mkdir()
    (real / 'helper').write_bytes(b'original')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'tools').symlink_to(real, target_is_directory=True)
    module.write_manifest(runtime, 'revision')
    receipt = module.verify_manifest(runtime)
    assert 'tools/helper' in receipt['files']


def test_manifest_can_explicitly_exclude_macho_relocation_outputs(tmp_path):
    module = load_script('manifest')
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/helper').write_bytes(b'before Mach-O relocation')
    (tmp_path / 'source.py').write_text('unchanged source')
    module.write_manifest(tmp_path, 'revision', exclude=('tools/helper',))
    (tmp_path / 'tools/helper').write_bytes(b'after Mach-O relocation')
    receipt = module.verify_manifest(tmp_path)
    assert receipt['excluded_native_files'] == ['tools/helper']
    assert 'source.py' in receipt['files']
    (tmp_path / 'source.py').write_text('corrupt')
    import pytest
    with pytest.raises(ValueError, match='source.py'):
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


def test_release_entry_build_omits_unrelated_arm_examples(tmp_path):
    module = load_script('build')
    assert hasattr(module, 'entry_build_env'), 'missing minimal entry build environment'
    makefile = tmp_path / 'Makefile'
    makefile.write_text('BIN_FILES=emmc_server.bin reset_example.bin\nall:\n\t@printf "%s\\n" "$(BIN_FILES)"\n')
    result = subprocess.run(['make', '-f', str(makefile)], env=module.entry_build_env(tmp_path),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'intermezzo.bin dfu_stage2.bin'


def test_macho_audit_rejects_build_machine_libraries():
    module = load_script('build')
    assert hasattr(module, 'external_macho_dependencies'), 'missing Mach-O dependency audit'
    output = 'tool:\n\t@loader_path/libusb.dylib (compatibility version 1.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n\t/opt/homebrew/opt/libusb/lib/libusb.dylib (compatibility version 1.0.0)\n'
    assert module.external_macho_dependencies(output) == ['/opt/homebrew/opt/libusb/lib/libusb.dylib']


def test_installed_formula_source_pin_is_preserved():
    module = load_script('build')
    assert hasattr(module, 'formula_source_pin'), 'missing installed-formula source lookup'
    text = 'url "https://example.invalid/component-1.2.tar.xz"\nsha256 "' + 'a' * 64 + '"\n'
    assert module.formula_source_pin(text) == ('https://example.invalid/component-1.2.tar.xz', 'a' * 64)
    import pytest
    with pytest.raises(ValueError, match='source pin'):
        module.formula_source_pin('no url or hash')


def test_windows_payload_checksum_refuses_corruption(tmp_path, monkeypatch):
    module = load_script('windows_launcher')
    (tmp_path / 'payload.tar.gz').write_bytes(b'corrupt')
    (tmp_path / 'payload.sha256').write_text('0' * 64)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'argv', ['bootstrap', '--self-check'])
    import pytest
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        module.main()


def test_apprun_diagnostics_do_not_request_administrator(tmp_path):
    import shutil
    shutil.copy2(ROOT / 'scripts/release/AppRun', tmp_path / 'AppRun')
    native = tmp_path / 'jibo-dfu'
    native.write_text('#!/bin/sh\nprintf "native:%s\\n" "$1"\n')
    native.chmod(0o755)
    for flag in ('--version', '--self-check', '--gui-smoke'):
        result = subprocess.run(['bash', str(tmp_path / 'AppRun'), flag], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'native:' + flag
        assert 'administrator' not in result.stdout


def test_download_rejects_wrong_source_hash(tmp_path):
    module = load_script('build')
    original = tmp_path / 'source'
    original.write_bytes(b'source fixture')
    import pytest
    with pytest.raises(RuntimeError, match='hash mismatch'):
        module.download(original.as_uri(), tmp_path / 'download', '0' * 64)
    assert not (tmp_path / 'download').exists()


def test_native_version_needs_no_desktop_or_tools():
    path = ROOT / 'scripts/release/native_launcher.py'
    assert path.is_file(), 'missing native launcher'
    result = subprocess.run([sys.executable, str(path), '--version'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Jibo DFU Mod Toolkit' in result.stdout

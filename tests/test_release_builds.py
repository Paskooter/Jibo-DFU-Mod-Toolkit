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
    windows_step = text.split('name: Build Windows one-file bootstrapper', 1)[1].split('- name:', 1)[0]
    assert windows_step.count('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }') == 6, 'native failures must not be masked by later PowerShell success'
    commands = [line.strip() for line in windows_step.splitlines()]
    for command in ('python -m pip install ', 'python scripts/release/build.py ', 'python -c ',
                    'powershell -NoProfile -File scripts/release/test-bootstrap.ps1',
                    'powershell -NoProfile -File scripts/release/test-console.ps1',
                    'powershell -NoProfile -File scripts/release/test-window-probe.ps1'):
        position = next(index for index, line in enumerate(commands) if line.startswith(command))
        assert commands[position + 1] == 'if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }', command


def test_windows_bootstrap_reuses_existing_usb_monitor_without_cloning():
    path = ROOT / 'scripts/release/bootstrap.ps1'
    assert path.is_file(), 'missing Windows bootstrap'
    text = path.read_text()
    for required in ('run.ps1', '-UsbOnly -MonitorUsb', 'Invoke-ToolkitElevated', 'Show-ToolkitQuestion',
                     '--version', '/mnt/wslg', 'payload.tar.gz'):
        assert required in text
    assert 'git clone' not in text
    assert 'run.sh' not in text
    import re
    assert not re.search(r'(?i)\$home\s*=', text), 'PowerShell HOME is read-only'
    assert 'Test-CompatibleDistribution' in text, 'old/musl distributions must be diagnosed before payload launch'
    assert 'GNU_LIBC_VERSION' in text


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


def test_source_notices_handle_raw_and_zip_downloads(tmp_path):
    module = load_script('build')
    assert hasattr(module, 'source_notice_contents'), 'missing source-format handling'
    raw = tmp_path / 'cacert.pem'
    raw.write_text('public certificate source fixture')
    assert module.source_notice_contents(raw) == []
    import zipfile
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('component/LICENSE', 'terms fixture')
        stream.writestr('component/source.c', 'source fixture')
    assert module.source_notice_contents(archive) == [('component/LICENSE', b'terms fixture')]


def test_notice_paths_do_not_create_fake_mac_bundles():
    module = load_script('build')
    assert hasattr(module, 'notice_path'), 'missing data-only notice path mapping'
    original = Path('Frameworks/Python.framework/Versions/3.12/LICENSE.txt')
    assert module.notice_path(original) == Path('Frameworks/Python_framework/Versions/3.12/LICENSE.txt')
    assert module.notice_path(Path('docs/example.app/LICENSE')) == Path('docs/example_app/LICENSE')
    assert module.notice_path(Path('COPYING')) == Path('COPYING')


def test_shared_payload_notices_copy_toolkit_license_and_notice(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = load_script('build')
    licenses = tmp_path / 'licenses'
    licenses.mkdir()
    entry = tmp_path / 'entry'
    entry.mkdir()
    (entry / 'source.c').write_text('synthetic source')
    package = tmp_path / 'pyinstaller'
    package.mkdir()
    (package / 'COPYING.txt').write_text('synthetic PyInstaller terms')
    monkeypatch.setitem(sys.modules, 'PyInstaller',
                        SimpleNamespace(__file__=str(package / '__init__.py'), __version__='fixture'))
    tk = tmp_path / 'tcltk'
    tk.mkdir()
    (tk / 'license.terms').write_text('synthetic Tcl/Tk terms')
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='darwin',
                        version_info=sys.version_info, version=sys.version))
    monkeypatch.setattr(module, 'mac_dependency_notices', lambda *args: None)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: str(tk) + '\n')
    monkeypatch.setattr(module.subprocess, 'run',
                        lambda *args, **kwargs: kwargs['stdout'].write(b'synthetic source archive'))
    monkeypatch.setattr(module, 'download',
                        lambda url, destination, *args: destination.write_bytes(b'synthetic dependency terms'))
    module.notices(licenses, entry, 'fixture-revision')
    # This shared directory is frozen into AppImage, both DMGs and the EXE payload.
    for name in ('LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md'):
        assert (licenses / name).is_file(), 'missing shared payload notice: ' + name
        assert (licenses / name).read_bytes() == (ROOT / name).read_bytes()


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


def test_windows_usb_permission_helper_only_grants_jibo_nodes(tmp_path):
    script = ROOT / 'scripts/release/wsl-usb-permissions.sh'
    assert script.is_file(), 'missing password-free WSL USB permission helper'
    sysfs = tmp_path / 'sys'
    devfs = tmp_path / 'dev'
    sysfs.mkdir()
    devfs.mkdir()
    for name, vendor, product, bus, device in (
        ('apx', '0955', '7740', '1', '2'),
        ('dfu', '0955', '701a', '1', '3'),
        ('other-product', '0955', '9999', '1', '4'),
        ('other-vendor', '1234', '701a', '1', '5'),
        ('invalid-bus', '0955', '701a', '../../etc', '6'),
    ):
        entry = sysfs / name
        entry.mkdir()
        for field, value in (('idVendor', vendor), ('idProduct', product),
                             ('busnum', bus), ('devnum', device)):
            (entry / field).write_text(value + '\n')
    (devfs / '001').mkdir()
    for number in ('002', '003', '004', '005'):
        (devfs / '001' / number).write_text('synthetic USB node')
    # Model the character-device boundary; chmod/chown are recorded, not executed.
    probe = r'''
source "$1"
test() { if [[ $1 == -c ]]; then [[ -f $2 ]]; else builtin test "$@"; fi; }
chown() { printf 'chown'; printf ' <%s>' "$@"; printf '\n'; }
chmod() { printf 'chmod'; printf ' <%s>' "$@"; printf '\n'; }
grant_jibo_usb "$2" "$3" 1000 1001
'''
    result = subprocess.run(['bash', '-c', probe, 'probe', str(script), str(sysfs), str(devfs)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f'chown <1000:1001> <--no-dereference> <--> <{devfs}/001/002>',
        f'chmod <0600> <--> <{devfs}/001/002>',
        f'chown <1000:1001> <--no-dereference> <--> <{devfs}/001/003>',
        f'chmod <0600> <--> <{devfs}/001/003>',
    ]
    # Hotplug adds DFU and removes APX; the same grant pass must rediscover nodes.
    (devfs / '001/002').unlink()
    result = subprocess.run(['bash', '-c', probe, 'probe', str(script), str(sysfs), str(devfs)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '002' not in result.stdout and '003' in result.stdout
    # Never follow a user/device-path symlink to a different host file.
    (devfs / '001/003').unlink()
    (devfs / '001/003').symlink_to(devfs / '001/004')
    result = subprocess.run(['bash', '-c', probe, 'probe', str(script), str(sysfs), str(devfs)],
                            capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == '', result.stderr


def test_windows_user_wrapper_keeps_uid_home_and_cleans_session(tmp_path):
    import os
    import pytest
    if os.geteuid() == 0:
        pytest.skip('requires the non-root invoking user')
    wrapper = ROOT / 'scripts/release/wsl-user-launch.sh'
    assert wrapper.is_file(), 'missing non-root WSL GUI wrapper'
    native = tmp_path / 'jibo-dfu'
    native.write_text('#!/bin/bash\nprintf "uid:%s home:%s session:%s\\n" "$EUID" "$HOME" "$(<"$SESSION")"\nexit 41\n')
    native.chmod(0o755)
    session = tmp_path / 'session'
    home = tmp_path / 'normal-home'
    env = os.environ.copy()
    env.update(HOME=str(home), SESSION=str(session))
    result = subprocess.run(['bash', str(wrapper), str(native), str(session)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 41, result.stderr
    assert f'uid:{os.geteuid()} home:{home} session:' in result.stdout
    assert not session.exists(), 'GUI exit must remove the watcher session'


def test_windows_usb_session_checks_process_uid_and_liveness(tmp_path):
    import os
    helper = ROOT / 'scripts/release/wsl-usb-permissions.sh'
    session = tmp_path / 'session'
    probe = r'''
source "$1"
printf '%s\n' "$$" > "$2"
usb_session_alive "$2" "$3" || exit 11
usb_session_alive "$2" 999999 && exit 12
printf '0\n' > "$2"
usb_session_alive "$2" "$3" && exit 13
printf 'invalid\n' > "$2"
usb_session_alive "$2" "$3" && exit 14
rm "$2"
usb_session_alive "$2" "$3" && exit 15
exit 0
'''
    result = subprocess.run(['bash', '-c', probe, 'probe', str(helper), str(session), str(os.geteuid())],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_windows_build_embeds_only_windows_privilege_helpers(tmp_path, monkeypatch):
    module = load_script('build')
    (tmp_path / 'payload.tar.gz').write_bytes(b'synthetic payload')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'download', lambda *args: None)
    calls = []
    from release_fakes import windows_build_run
    monkeypatch.setattr(module, 'run', windows_build_run(calls, tmp_path))
    module.windows(tmp_path)
    build_args = list(map(str, calls[0]))
    embedded = [build_args[i + 1] for i, arg in enumerate(build_args) if arg == '--add-data']
    for helper in ('wsl-usb-permissions.sh', 'wsl-user-launch.sh'):
        assert str(tmp_path / 'scripts/release' / helper) + ';.' in embedded
    # GPL-3.0-only: the GPL text and the copyright notice must be embedded data
    # of every single-file artifact, not only inside the Linux payload tarball.
    for name in ('LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md'):
        assert str(tmp_path / name) + ';.' in embedded, name
        assert (ROOT / name).is_file(), name
    # These helpers are EXE-only data, never part of shared Linux AppRun/native logic.
    assert 'wsl-usb-permissions.sh' not in (ROOT / 'scripts/release/AppRun').read_text()
    assert 'wsl-user-launch.sh' not in (ROOT / 'scripts/release/native_launcher.py').read_text()


def test_windows_selfcheck_requires_embedded_license_and_notice(tmp_path, monkeypatch, capsys):
    import hashlib
    import io
    import tarfile
    import pytest
    module = load_script('windows_launcher')
    archive = tmp_path / 'payload.tar.gz'
    with tarfile.open(archive, 'w:gz') as target:
        for name in ('jibo-dfu', 'AppRun', '_internal/runtime/licenses/THIRD-PARTY-NOTICES.md',
                     '_internal/runtime/licenses/LICENSE'):
            info = tarfile.TarInfo(name)
            info.size = 7
            target.addfile(info, io.BytesIO(b'fixture'))
    (tmp_path / 'payload.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    (tmp_path / 'revision.txt').write_text('a' * 40)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'argv', ['bootstrap', '--self-check'])
    for name in ('bootstrap.ps1', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh', 'wsl-provision.sh',
                 'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md', 'console.ps1', 'usbipd.ps1'):
        with pytest.raises(RuntimeError, match='Missing embedded Windows helper: ' + name):
            module.main()
        (tmp_path / name).write_text('synthetic helper')
    assert module.main() == 0
    assert 'wsl_setup_attempted' in capsys.readouterr().out


def test_native_release_has_self_contained_production_flash_verification(monkeypatch):
    import pytest
    import importlib.util
    spec = importlib.util.spec_from_file_location('flash_native_launcher', ROOT / 'scripts/release/native_launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.flash_trust_check()
    assert result['key_id'] == 'phoenix-flash-2026-10'
    assert result['ed25519'] is True
    import jibo_flash_contract as contract
    monkeypatch.setattr(contract, '_python_verifier', lambda: None)
    with pytest.raises(RuntimeError, match='Ed25519'):
        module.flash_trust_check()


def test_non_windows_launchers_are_byte_identical_to_release_builds_base():
    import hashlib
    # Source launchers are pinned to public main 5d8b136; release adapters to
    # 03a25aa. run.sh now has WSL no-sudo diagnostics (covered by test_run_sh);
    # Mac and frozen Linux adapters must remain byte-identical.
    expected = {
        'run-macos.sh': '859eeabc991687f18290d1e918499e75ff4cfb2e76bc755e4d72302dff220071',
        'run-gui.sh': 'cdf548d795acf939184ffe51d0b689cde4133958b006cd083fbf28b469cc5fde',
        'scripts/release/AppRun': '9e9f7f214a2224b0b1c06b698e75cd88a6348fac4bf2e0de2c9d95ce262f10f8',
    }
    for path, digest in expected.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


def test_windows_usb_grant_failure_is_not_reported_as_success(tmp_path):
    script = ROOT / 'scripts/release/wsl-usb-permissions.sh'
    entry = tmp_path / 'sys/apx'
    entry.mkdir(parents=True)
    for field, value in (('idVendor', '0955'), ('idProduct', '7740'), ('busnum', '1'), ('devnum', '2')):
        (entry / field).write_text(value + '\n')
    node = tmp_path / 'dev/001/002'
    node.parent.mkdir(parents=True)
    node.write_text('synthetic USB node')
    probe = r'''
source "$1"
test() { if [[ $1 == -c ]]; then [[ -f $2 ]]; else builtin test "$@"; fi; }
chown() { return 5; }
grant_jibo_usb "$2" "$3" 1000 1001
'''
    result = subprocess.run(['bash', '-c', probe, 'probe', str(script), str(tmp_path / 'sys'), str(tmp_path / 'dev')],
                            capture_output=True, text=True)
    assert result.returncode != 0, 'live-node permission failure was silently swallowed'
    assert 'Could not grant Jibo USB access' in result.stderr


def test_native_version_needs_no_desktop_or_tools():
    path = ROOT / 'scripts/release/native_launcher.py'
    assert path.is_file(), 'missing native launcher'
    result = subprocess.run([sys.executable, str(path), '--version'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Jibo DFU Mod Toolkit' in result.stdout

#!/usr/bin/env python3
"""Build native release bundles on CI only. Never invoked on an end user's host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from manifest import write_manifest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    'libusb-1.0.27.tar.bz2': ('https://github.com/libusb/libusb/releases/download/v1.0.27/libusb-1.0.27.tar.bz2',
                            'ffaa41d741a8a3bee244ac8e54a72ea05bf2879663c098c82fc5757853441575'),
    'dfu-util-0.11.tar.gz': ('https://dfu-util.sourceforge.net/releases/dfu-util-0.11.tar.gz',
                          'b4b53ba21a82ef7e3d4c47df2952adf5fa494f499b6b0b57c58c5d04ae8ff19e'),
    'e2fsprogs-1.47.1.tar.gz': ('https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v1.47.1/e2fsprogs-1.47.1.tar.gz',
                              '0d2e0bf80935c3392b73a60dbff82d8a6ef7ea88b806c2eea964b6837d3fd6c2'),
}
APPIMAGETOOL = ('https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage',
                'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0')
TOOLS = ('dfu-util', 'debugfs', 'e2fsck', 'resize2fs', 'dumpe2fs', 'mke2fs')


def run(*args, cwd=None, env=None):
    print('+', *map(str, args), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def download(url, destination, expected=None):
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if expected and actual != expected:
        raise RuntimeError('Download hash mismatch: ' + url)
    destination.write_bytes(data)
    return actual


def unpack(archive, directory):
    # Only SHA256-pinned build-time upstream archives, never end-user archives.
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = (directory / member.name).resolve()
            if directory.resolve() not in path.parents:
                raise RuntimeError('Unsafe source archive path: ' + member.name)
        source.extractall(directory)


def configure_flags(name, prefix):
    flags = ['--prefix=' + str(prefix), '--disable-nls']
    if name.startswith('libusb'):
        flags = ['--prefix=' + str(prefix), '--disable-static']
        if sys.platform != 'darwin':
            flags += ['--disable-udev']
    elif name.startswith('e2fsprogs'):
        flags += ['--disable-fuse2fs', '--without-udev-rules-dir',
                  '--without-systemd-unit-dir', '--without-crond-dir']
    return flags


def dependencies(work, licenses):
    prefix = work / 'prefix'
    prefix.mkdir()
    env = os.environ.copy()
    env.update(PKG_CONFIG_PATH=str(prefix / 'lib/pkgconfig'),
               CPPFLAGS='-I' + str(prefix / 'include'), LDFLAGS='-L' + str(prefix / 'lib'))
    for name, (url, sha) in SOURCES.items():
        archive = licenses / name
        download(url, archive, sha)
        unpack(archive, work)
        source = work / name.split('.tar.')[0]
        flags = configure_flags(name, prefix)
        run(source / 'configure', *flags, cwd=source, env=env)
        run('make', '-j4', cwd=source, env=env)
        run('make', 'install', cwd=source, env=env)
        for license_name in ('COPYING', 'COPYING.LIB', 'NOTICE', 'LICENSE'):
            file = source / license_name
            if file.is_file():
                shutil.copy2(file, licenses / (source.name + '-' + license_name))
    return prefix


def formula_source_pin(text):
    url = re.search(r'^\s*url\s+"(https://[^"#]+)"', text, re.MULTILINE)
    sha = re.search(r'^\s*sha256\s+"([0-9a-f]{64})"', text, re.MULTILINE)
    if not url or not sha:
        raise ValueError('Installed formula has no literal stable source pin')
    return url.group(1), sha.group(1)


def mac_dependency_notices(licenses):
    # A superset of runtime dependencies is deliberate: include all installed
    # interpreter dependency notices, and corresponding sources for GPL/LGPL libs.
    names = set()
    for formula_name in ('python@3.12', 'python-tk@3.12'):
        names.update(subprocess.check_output(['brew', 'deps', '--formula', formula_name],
                                            text=True).splitlines())
    inventory = {}
    for name in sorted(names):
        keg = Path(subprocess.check_output(['brew', '--prefix', name], text=True).strip()).resolve()
        target = licenses / 'Homebrew-dependencies' / name
        target.mkdir(parents=True)
        formula = next((keg / '.brew').glob('*.rb'))
        text = formula.read_text()
        shutil.copy2(formula, target)
        receipt = keg / 'INSTALL_RECEIPT.json'
        if receipt.is_file():
            shutil.copy2(receipt, target)
        copied = []
        for file in keg.rglob('*'):
            if file.is_file() and any(file.name.lower().startswith(k) for k in
                                     ('license', 'licence', 'copying', 'copyright', 'notice')):
                relative = file.relative_to(keg)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)
                copied.append(relative.as_posix())
        copyleft = bool(re.search(r'\bL?GPL-', text))
        item = {'keg_version': keg.name, 'notices': copied, 'source_included': False}
        if copyleft or not copied:
            url, sha = formula_source_pin(text)
            archive = target / ('corresponding-source-' + Path(url.split('?', 1)[0]).name)
            download(url, archive, sha)
            item.update(source_included=True, source_url=url, source_sha256=sha)
            # Archives themselves contain all original attribution/license texts.
            with tarfile.open(archive) as source:
                for member in source.getmembers():
                    basename = Path(member.name).name.lower()
                    if member.isfile() and any(basename.startswith(k) for k in
                                              ('license', 'licence', 'copying', 'copyright', 'notice')):
                        extracted = source.extractfile(member)
                        if extracted is not None:
                            destination = target / 'source-notices' / member.name
                            if target.resolve() not in destination.resolve().parents:
                                raise RuntimeError('Unsafe source notice path: ' + member.name)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_bytes(extracted.read())
        inventory[name] = item
    (licenses / 'Homebrew-dependencies.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print('Bundled interpreter dependency notices:', json.dumps(inventory, indent=2))


def notices(licenses, entry, revision):
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.md', 'vendor/jibo-ram-dfu-v1-source.tar.gz'):
        shutil.copy2(ROOT / name, licenses / Path(name).name)
    shutil.copytree(ROOT / 'firmware', licenses / 'firmware')
    for name in ('build_loader.py', 'build_file_level_entry.py'):
        shutil.copy2(ROOT / 'scripts' / name, licenses / name)
    shutil.copy2(ROOT / 'patches/shofel2-dfu-entry.patch', licenses)
    shutil.copy2(ROOT / 'docs/loader-build.md', licenses)
    # Include the actual patched ShofEL source and protocol header used to compile.
    with tarfile.open(licenses / 'ShofEL2-corresponding-source.tar.gz', 'w:gz') as archive:
        for file in sorted(entry.rglob('*')):
            if file.is_file() and '.git' not in file.parts and file.suffix not in ('.o', '.d') and file.name not in ('shofel2_t124', 'intermezzo.bin', 'dfu_stage2.bin'):
                archive.add(file, arcname='ShofEL2/' + file.relative_to(entry).as_posix())
    # Preserve component file headers as well as any top-level license files.
    for file in entry.iterdir():
        if file.is_file() and any(x in file.name.upper() for x in ('COPYING', 'LICENSE', 'NOTICE')):
            shutil.copy2(file, licenses / ('ShofEL2-' + file.name))
    with (licenses / 'toolkit-source.tar.gz').open('wb') as stream:
        subprocess.run(['git', 'archive', '--format=tar.gz', revision], cwd=ROOT, stdout=stream, check=True)
    import PyInstaller
    package = Path(PyInstaller.__file__).parent
    candidates = [package / 'COPYING.txt', package.parent / 'pyinstaller' / 'COPYING.txt']
    found = next((p for p in candidates if p.is_file()), None)
    if found:
        shutil.copy2(found, licenses / 'PyInstaller-COPYING.txt')
    else:
        download('https://raw.githubusercontent.com/pyinstaller/pyinstaller/v6.16.0/COPYING.txt', licenses / 'PyInstaller-COPYING.txt')
    version = '.'.join(map(str, sys.version_info[:3]))
    download('https://raw.githubusercontent.com/python/cpython/v' + version + '/LICENSE', licenses / 'Python-LICENSE.txt')
    if sys.platform == 'darwin':
        mac_dependency_notices(licenses)
        prefix = Path(subprocess.check_output(['brew', '--prefix', 'tcl-tk'], text=True).strip())
        terms = [p for p in prefix.rglob('*') if p.is_file() and p.name.lower() in ('license.terms', 'license', 'license.txt', 'copying')]
        if not terms:
            # Homebrew uses tcl-tk@8 for python-tk@3.12 on some runner revisions.
            prefix = Path(subprocess.check_output(['brew', '--prefix', 'tcl-tk@8'], text=True).strip())
            terms = [p for p in prefix.rglob('*') if p.is_file() and p.name.lower() in ('license.terms', 'license', 'license.txt', 'copying')]
        for i, path in enumerate(terms):
            shutil.copy2(path, licenses / ('TclTk-' + str(i) + '-' + path.name))
        if not terms:
            raise RuntimeError('Tcl/Tk license texts are missing')
    else:
        # Copyright notices for all potentially collected X11/Tk/Python system libs.
        target = licenses / 'system-library-notices'
        target.mkdir()
        for path in Path('/usr/share/doc').glob('*/copyright'):
            if path.is_file():
                shutil.copy2(path, target / (path.parent.name + '.txt'))
        for package_name in ('tcl8.6', 'tk8.6'):
            if not (target / (package_name + '.txt')).is_file():
                raise RuntimeError('Missing Tcl/Tk license: ' + package_name)
    download('https://raw.githubusercontent.com/AppImage/type2-runtime/main/LICENSE', licenses / 'AppImage-runtime-LICENSE.txt')
    (licenses / 'BUILD-SOURCES.json').write_text(json.dumps({'revision': revision, 'sources': SOURCES,
        'python': sys.version, 'pyinstaller': PyInstaller.__version__}, indent=2) + '\n')


def entry_build_env(prefix):
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = str(prefix / 'lib')
    env['LIBRARY_PATH'] = str(prefix / 'lib')
    env['CPATH'] = str(prefix / 'include/libusb-1.0')
    if sys.platform != 'darwin':
        # Build only the two payloads actually distributed, not unrelated examples
        # that need newer C dialects than Ubuntu's ARM GCC. Source launcher unchanged.
        env['MAKEFLAGS'] = 'BIN_FILES=intermezzo.bin\\ dfu_stage2.bin'
    return env


def external_macho_dependencies(output):
    dependencies = []
    for line in output.splitlines()[1:]:
        name = line.strip().split(' (', 1)[0]
        if name and not name.startswith(('@', '/usr/lib/', '/System/Library/')):
            dependencies.append(name)
    return dependencies


def audit_macho(root):
    seen = set()
    for directory, _, names in os.walk(root, followlinks=True):
        for name in names:
            path = Path(directory) / name
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            with path.open('rb') as stream:
                magic = stream.read(4)
            if magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
                continue
            output = subprocess.check_output(['otool', '-L', str(path)], text=True)
            external = external_macho_dependencies(output)
            if external:
                raise RuntimeError('Unbundled Mach-O dependency in ' + str(path) + ': ' + str(external))
    print('PASS: Mach-O dependencies contain only bundle-relative or macOS system references')


def native(architecture):
    work = ROOT / '.build/release'
    work.mkdir(parents=True)
    runtime = work / 'runtime'
    runtime.mkdir()
    licenses = runtime / 'licenses'
    licenses.mkdir()
    prefix = dependencies(work, licenses)
    env = entry_build_env(prefix)
    entry = work / 'entry'
    args = [sys.executable, ROOT / 'scripts/build_file_level_entry.py', '--out', entry]
    if sys.platform == 'darwin':
        args += ['--libusb-prefix', prefix]
    run(*args, env=env)
    tools = runtime / 'tools'
    tools.mkdir()
    for name in TOOLS:
        path = next((prefix / d / name for d in ('bin', 'sbin') if (prefix / d / name).is_file()), None)
        if path is None:
            raise RuntimeError('Missing prebuilt tool: ' + name)
        shutil.copy2(path, tools / name)
    for name in ('shofel2_t124', 'intermezzo.bin', 'dfu_stage2.bin'):
        shutil.copy2(entry / name, tools / name)
    lib = runtime / 'lib'
    lib.mkdir()
    library_name = 'libusb-1.0.dylib' if sys.platform == 'darwin' else 'libusb-1.0.so.0'
    shutil.copy2(prefix / 'lib' / library_name, lib / library_name)
    for path in ROOT.glob('jibo*.py'):
        shutil.copy2(path, runtime / path.name)
    shutil.copytree(ROOT / 'assets', runtime / 'assets')
    shutil.copy2(ROOT / 'assets/loader.bin', runtime / 'loader.bin')
    shutil.copy2(ROOT / 'tests/gui_fakes.py', runtime / 'gui_fakes.py')
    shutil.copy2(ROOT / 'scripts/release/manifest.py', runtime / 'manifest.py')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    notices(licenses, entry, revision)
    # Mach-O relocation/ad-hoc preparation changes binary bytes during freezing.
    # Hash data before freezing, then NEVER edit the sealed .app afterward.
    excluded = (['tools/' + name for name in TOOLS + ('shofel2_t124',)] +
                ['lib/' + library_name]) if sys.platform == 'darwin' else []
    write_manifest(runtime, revision, exclude=excluded)
    name = 'Jibo DFU Mod Toolkit' if sys.platform == 'darwin' else 'jibo-dfu'
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', name,
            '--distpath', work / 'dist', '--workpath', work / 'freeze', '--specpath', work,
            '--paths', ROOT, '--paths', ROOT / 'tests',
            '--add-data', str(runtime) + ':runtime', '--hidden-import', 'tkinter',
            '--hidden-import', 'tkinter.ttk', '--hidden-import', 'tkinter.font',
            '--hidden-import', 'tkinter.filedialog', '--hidden-import', 'tkinter.messagebox',
            '--hidden-import', 'gui_fakes', '--hidden-import', 'manifest']
    for module in ROOT.glob('jibo*.py'):
        args += ['--hidden-import', module.stem]
    for name in TOOLS + ('shofel2_t124',):
        args += ['--add-binary', str(tools / name) + ':runtime/tools']
    args += ['--add-binary', str(lib / library_name) + ':runtime/lib']
    if sys.platform == 'darwin':
        args += ['--windowed', '--osx-bundle-identifier', 'io.jibo.dfu-toolkit']
    args += [ROOT / 'scripts/release/native_launcher.py']
    run(*args, env=env)
    # Prove tools do not accidentally execute from/link to the build prefix.
    prefix.rename(work / 'build-prefix-hidden')
    out = ROOT / 'release-out'
    out.mkdir(exist_ok=True)
    if sys.platform == 'darwin':
        app = work / 'dist/Jibo DFU Mod Toolkit.app'
        audit_macho(app)
        # Verification only: no identity signing/notarization step is added.
        run('codesign', '--verify', '--all-architectures', '--deep', '--strict', app)
        binary = app / 'Contents/MacOS/Jibo DFU Mod Toolkit'
        run(binary, '--self-check')
        run(binary, '--gui-smoke')
        image_root = work / 'dmg-root'
        image_root.mkdir()
        shutil.copytree(app, image_root / app.name, symlinks=True)
        (image_root / 'Applications').symlink_to('/Applications')
        shutil.copy2(ROOT / 'LICENSE', image_root)
        shutil.copy2(ROOT / 'THIRD-PARTY-NOTICES.md', image_root)
        dmg = out / ('Jibo-DFU-macOS-' + architecture + '.dmg')
        run('hdiutil', 'create', '-volname', 'Jibo DFU Toolkit', '-srcfolder', image_root, '-format', 'UDZO', dmg)
        with tarfile.open(out / ('Jibo-DFU-macOS-' + architecture + '-corresponding-source.tar.gz'), 'w:gz') as archive:
            archive.add(licenses, arcname='licenses')
    else:
        appdir = work / 'dist/jibo-dfu'
        write_manifest(appdir / '_internal/runtime', revision)
        shutil.copy2(ROOT / 'scripts/release/AppRun', appdir / 'AppRun')
        (appdir / 'AppRun').chmod(0o755)
        run(appdir / 'jibo-dfu', '--self-check')
        run('xvfb-run', '-a', appdir / 'jibo-dfu', '--gui-smoke')
        # The same verified onedir payload is embedded in the Windows bootstrapper.
        with tarfile.open(out / 'payload.tar.gz', 'w:gz') as archive:
            for path in sorted(appdir.iterdir()):
                archive.add(path, arcname=path.name)
        (out / 'revision.txt').write_text(revision + '\n')
        (appdir / 'jibo-dfu.desktop').write_text('[Desktop Entry]\nType=Application\nName=Jibo DFU Toolkit\nExec=AppRun\nIcon=jibo-dfu\nCategories=Utility;\nTerminal=false\n')
        (appdir / 'jibo-dfu.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" rx="24" fill="#1e2633"/><circle cx="64" cy="64" r="28" fill="#73d5ca"/></svg>')
        appimagetool = work / 'appimagetool.AppImage'
        download(*[APPIMAGETOOL[0], appimagetool, APPIMAGETOOL[1]])
        appimagetool.chmod(0o755)
        image = out / 'Jibo-DFU-Linux-x86_64.AppImage'
        tool_env = os.environ.copy()
        tool_env['ARCH'] = 'x86_64'
        run(appimagetool, '--appimage-extract-and-run', appdir, image, env=tool_env)
        run(image, '--appimage-extract-and-run', '--self-check')
        run('xvfb-run', '-a', image, '--appimage-extract-and-run', '--gui-smoke')
        with tarfile.open(out / 'Jibo-DFU-corresponding-source.tar.gz', 'w:gz') as archive:
            archive.add(licenses, arcname='licenses')
        shutil.copy2(ROOT / 'LICENSE', out)
        shutil.copy2(ROOT / 'THIRD-PARTY-NOTICES.md', out)
    print('Release files:', [(p.name, p.stat().st_size) for p in out.iterdir()])


def windows(payload):
    out = ROOT / 'release-out'
    out.mkdir(exist_ok=True)
    payload = Path(payload).resolve()
    sha = hashlib.sha256((payload / 'payload.tar.gz').read_bytes()).hexdigest()
    (payload / 'payload.sha256').write_text(sha + '\n')
    windows_license = payload / 'Windows-Python-LICENSE.txt'
    version = '.'.join(map(str, sys.version_info[:3]))
    download('https://raw.githubusercontent.com/python/cpython/v' + version + '/LICENSE', windows_license)
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
            '--name', 'Jibo-DFU-Windows-x64', '--distpath', out]
    for file in ('payload.tar.gz', 'payload.sha256', 'revision.txt', 'Windows-Python-LICENSE.txt'):
        args += ['--add-data', str(payload / file) + ';.']
    for file in ('scripts/release/bootstrap.ps1', 'run.ps1', 'LICENSE', 'THIRD-PARTY-NOTICES.md'):
        args += ['--add-data', str(ROOT / file) + ';.']
    run(*args, ROOT / 'scripts/release/windows_launcher.py')
    run(out / 'Jibo-DFU-Windows-x64.exe', '--version')
    run(out / 'Jibo-DFU-Windows-x64.exe', '--self-check')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--architecture', choices=('arm64', 'x86_64'), default='x86_64')
    parser.add_argument('--windows-payload', type=Path)
    args = parser.parse_args()
    if args.windows_payload:
        windows(args.windows_payload)
    else:
        native(args.architecture)

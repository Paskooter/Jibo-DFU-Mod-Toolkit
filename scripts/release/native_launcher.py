"""Frozen Python adapter. The existing toolkit sources remain unmodified."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


def is_pipeline_request(arguments, runtime):
    return bool(arguments and Path(arguments[0]).resolve() ==
                (Path(runtime) / 'jibo_dfu_pipeline.py').resolve())


def configure(runtime):
    # Real .py files are intentional: the GUI clones jibo_tui via get_source().
    sys.path.insert(0, str(runtime))
    tools = runtime / 'tools'
    os.environ['PATH'] = str(tools) + os.pathsep + os.environ.get('PATH', '')
    os.environ['JIBO_DFU_UTIL'] = str(tools / 'dfu-util')
    os.environ['JIBO_SHOFEL2'] = str(tools / 'shofel2_t124')
    os.environ['JIBO_LIBUSB'] = str(runtime / 'lib' / ('libusb-1.0.dylib' if sys.platform == 'darwin' else 'libusb-1.0.so.0'))


def flash_trust_check():
    import jibo_flash_contract as contract
    keys = contract.trusted_keys()
    verify = contract._python_verifier()
    if verify is None:
        raise RuntimeError('Bundled Ed25519 verifier is unavailable')
    public = bytes.fromhex('302a300506032b6570032100' +
                           '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c')
    import base64
    public = b'-----BEGIN PUBLIC KEY-----\n' + base64.b64encode(public) + b'\n-----END PUBLIC KEY-----\n'
    signature = bytes.fromhex('92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0'
                              '85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00')
    if not verify(public, b'\x72', signature) or verify(public, b'\x73', signature):
        raise RuntimeError('Bundled Ed25519 verification failed')
    if set(keys) != {'phoenix-flash-2026-10'}:
        raise RuntimeError('Unexpected hosted-image release trust')
    return {'key_id': 'phoenix-flash-2026-10', 'ed25519': True}


def rcm_help_check(runtime):
    # The GUI help photo must be the pinned bytes and decode with the bundled Tk.
    import hashlib
    import jibo_tui
    data = (runtime / jibo_tui.RCM_HELP_IMAGE).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != jibo_tui.RCM_HELP_IMAGE_SHA256:
        raise RuntimeError('RCM help photo does not match its pinned hash')
    return {'path': jibo_tui.RCM_HELP_IMAGE, 'sha256': digest, 'bytes': len(data)}


def hosted_transport_check():
    # Offline, with the exact context used by index/manifest/image requests.
    # This checks trust installation, not internet reachability or USB.
    import ssl
    import jibo_hosted_updates as hosted
    context = hosted._tls_context()
    count = context.cert_store_stats()['x509_ca']
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname or not count:
        raise RuntimeError('Hosted TLS trust self-check failed')
    return {'certificate_verification': True, 'hostname_verification': True,
            'ca_certificates': count}


def self_check(runtime):
    from manifest import verify_manifest
    receipt = verify_manifest(runtime)
    receipt['hosted_flash'] = flash_trust_check()
    receipt['hosted_transport'] = hosted_transport_check()
    import ctypes
    ctypes.CDLL(os.environ['JIBO_LIBUSB'])
    import tkinter
    receipt['tcl'] = tkinter.Tcl().eval('info patchlevel')
    tools = runtime / 'tools'
    probes = {'dfu-util': ('--version',), 'shofel2_t124': ('--dfu-stage-capability',),
              'debugfs': ('-V',), 'e2fsck': ('-V',), 'resize2fs': ('-V',),
              'dumpe2fs': ('-V',), 'mke2fs': ('-V',)}
    receipt['tools'] = {}
    for name, args in probes.items():
        result = subprocess.run([str(tools / name), *args], capture_output=True, text=True, timeout=15)
        output = (result.stdout + result.stderr).strip()
        # resize2fs has no version-only success mode; -V prints its version and usage.
        if result.returncode != 0 and not (name == 'resize2fs' and result.returncode == 1 and 'resize2fs' in output):
            raise RuntimeError(name + ' failed: ' + output)
        if name == 'shofel2_t124' and result.stdout.strip() != 'dfu-stage-launch=1':
            raise RuntimeError('ShofEL is missing DFU launch capability')
        receipt['tools'][name] = output
    # Source-based private flow loading must also work inside the frozen bundle.
    import jibo_gui_core
    jibo_gui_core.load_flows()
    pipeline = subprocess.run([sys.executable, str(runtime / 'jibo_dfu_pipeline.py'), '--help'],
                              capture_output=True, text=True, timeout=15)
    if pipeline.returncode or 'Pipelined DFU transfer' not in pipeline.stdout:
        raise RuntimeError('Embedded pipeline subprocess failed: ' + pipeline.stderr)
    receipt['pipeline_subprocess'] = True
    receipt['rcm_help_image'] = rcm_help_check(runtime)
    print(json.dumps({k: v for k, v in receipt.items() if k != 'files'}, indent=2))


def gui_smoke(runtime):
    # An explicit CI-only, read-only first-window probe; no USB access.
    import jibo_gui
    import jibo_gui_core
    import gui_fakes
    controller = jibo_gui_core.GuiController(gui_fakes.FakeApi('none'))
    root = jibo_gui.create_root()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(str(exc))
    window = jibo_gui.ToolkitWindow(root, controller)
    for _ in range(20):
        root.update()
    revision = json.loads((runtime / 'release-manifest.json').read_text())['revision']
    expected_title = 'Jibo DFU Toolkit (build ' + revision[:7] + ')'
    if root.title() != expected_title:
        raise RuntimeError('GUI build banner mismatch: ' + root.title())
    if not root.winfo_viewable() or errors:
        raise RuntimeError('First window did not render: ' + str(errors))
    # With no robot connected the RCM how-to is offered; it must open with its photo.
    window.rcm_help_link.invoke()
    for _ in range(10):
        root.update()
    photo = window.controls.get('rcm_photo')
    if window.page_name != 'rcm-help' or photo is None or not str(photo.cget('image')) or errors:
        raise RuntimeError('RCM help did not show its photo: ' + str(errors))
    window.controls['primary'].invoke()
    print(json.dumps({'first_window': True, 'rcm_help_photo': True, 'title': root.title(), 'width': root.winfo_width(), 'height': root.winfo_height(),
                      'tk': root.tk.call('info', 'patchlevel'), 'usb_access': False}))
    window.close()
    controller.shutdown()


def main():
    arguments = sys.argv[1:]
    if arguments == ['--version']:
        print('Jibo DFU Mod Toolkit (release bundle)')
        return 0
    runtime = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'runtime'
    configure(runtime)
    if arguments == ['--self-check']:
        self_check(runtime)
        return 0
    if arguments == ['--gui-smoke']:
        gui_smoke(runtime)
        return 0
    if is_pipeline_request(arguments, runtime):
        sys.argv = arguments
        runpy.run_path(str(runtime / 'jibo_dfu_pipeline.py'), run_name='__main__')
        return 0
    sys.argv = [str(runtime / 'jibo_dfu.py'), *(arguments or ['gui'])]
    runpy.run_path(str(runtime / 'jibo_dfu.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

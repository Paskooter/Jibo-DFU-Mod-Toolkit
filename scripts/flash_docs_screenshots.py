#!/usr/bin/env python3
"""Capture the two flash-choice screens with neutral fixtures and no USB.

Requires Tk, Pillow and ImageMagick. Run under a desktop or xvfb-run.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from scripts.docs_screenshots import DocsApi, clean_png
import jibo_flash_policy as policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/images/gui')
    parser.add_argument('--hosted', action='store_true', help='Also capture the unified Phoenix flash flow')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import jibo_gui as gui
    import jibo_gui_core as core
    api = DocsApi()
    errors = []
    with tempfile.TemporaryDirectory(prefix='flash-docs-') as temp:
        controller = core.GuiController(api, preferences_path=Path(temp) / 'preferences.json')
        root = gui.create_root()
        root.report_callback_exception = lambda *exc: errors.append(str(exc))
        window = gui.ToolkitWindow(root, controller)
        root.geometry('1080x720')

        def pump(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                assert not errors, errors
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError('Documentation capture timed out: ' + window.page_name)

        def texts(widget):
            if not widget.winfo_viewable():
                return ''
            values = [str(widget.cget('text'))] if 'text' in widget.keys() else []
            if widget.winfo_class() == 'Text':
                values.append(widget.get('1.0', 'end-1c'))
            values.extend(texts(child) for child in widget.winfo_children())
            return '\n'.join(values)

        def capture(name):
            root.update()
            visible = texts(root)
            assert not re.search(r'\bfake\b|\btest(?:ing)?\b|/tmp', visible, re.I), visible
            path = args.out / (name + '.png')
            subprocess.run(['import', '-window', str(root.winfo_id()), str(path)], check=True, timeout=10)
            clean_png(path)
            print(path)

        try:
            pump(lambda: not controller.busy)
            root.focus_force()
            window.open_action('flash-update')
            pump(lambda: window.prompt is not None)
            window.controls['primary'].invoke()
            pump(lambda: window.prompt is not None and window.prompt.title == 'Choose calibration handling')
            assert window.controls['selected']() == policy.KEEP
            assert tuple(window.controls['options']) == policy.POLICIES
            capture('flash-calibration')
            window.controls['choose'](policy.WITHOUT)
            window.controls['primary'].invoke()
            pump(lambda: window.prompt is not None and window.prompt.kind == 'confirm')
            assert window.prompt is not None
            assert window.prompt.title == policy.UNSAFE_TITLE
            assert window.prompt.initial == 'cancel'
            pump(lambda: not window.controls['primary'].instate(['disabled']))
            assert root.focus_get() == window.controls['cancel']
            capture('flash-without-calibration-confirm')
            window.controls['cancel'].invoke()
            pump(lambda: not controller.busy)
            assert not api.calls and not api.writes
            print('Verified: two choices, keep preselected, unsafe Cancel-default, zero backend writes/calls.')
        finally:
            controller.shutdown()
            pump(lambda: not controller.busy)
            window.close()
    if args.hosted:
        capture_hosted(args.out)
    return 0


def capture_hosted(out):
    """Render the real shared flow with separate, neutral documentation data."""
    from dataclasses import replace
    from unittest.mock import patch
    import threading
    from gui_fakes import FakeHosted, FakeHostedApi, sample_catalog
    import jibo_gui as gui
    import jibo_gui_core as core

    catalog = sample_catalog()
    catalog = replace(catalog, images=tuple(replace(image, version='2026.10.14' if image.is_latest else '2026.10.01')
                                           for image in catalog.images))
    hosted = FakeHosted(catalog=catalog)
    hosted.gate = threading.Event()
    api = FakeHostedApi(hosted, packages=['updates/jibo-13.0.0.tar.bz2'])
    api.BACKUP_ROOT = Path('~/Jibo-Backups')
    errors = []
    with tempfile.TemporaryDirectory(prefix='hosted-docs-') as temp:
        directory = Path(temp)
        controller = core.GuiController(api, preferences_path=directory / 'preferences.json')
        root = gui.create_root()
        root.report_callback_exception = lambda *exc: errors.append(str(exc))
        window = gui.ToolkitWindow(root, controller)
        root.geometry('1080x720')

        def pump(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                root.update()
                assert not errors, errors
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError('Hosted capture timed out: ' + window.page_name)

        def texts(widget):
            if not widget.winfo_viewable():
                return ''
            values = [str(widget.cget('text'))] if 'text' in widget.keys() else []
            if widget.winfo_class() == 'Text':
                values.append(widget.get('1.0', 'end-1c'))
            values.extend(texts(child) for child in widget.winfo_children())
            return '\n'.join(values)

        def geometry():
            root.update()
            for key in ('primary', 'cancel'):
                button = window.controls[key]
                assert button.winfo_height() >= button.winfo_reqheight()
                assert button.winfo_width() >= button.winfo_reqwidth()
                bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
                assert 0 < bottom <= root.winfo_height()
            row = next(iter(window.controls['options'].values()))
            assert row.label.winfo_height() >= row.label.winfo_reqheight()
            assert row.label.winfo_width() > 100

        def capture(name):
            root.update()
            visible = texts(root)
            assert not re.search(r'\bfake\b|\btest(?:ing)?\b|/tmp|Download a Phoenix image', visible, re.I), visible
            path = out / (name + '.png')
            subprocess.run(['import', '-window', str(root.winfo_id()), str(path)], check=True, timeout=10)
            clean_png(path)
            print(path)

        try:
            with patch.object(controller.flows, 'hosted_folders', return_value=(directory / 'updates', directory / 'cache')):
                pump(lambda: not controller.busy)
                root.focus_force()
                window.render_state()
                assert all(item.key != 'download-phoenix' for item in controller.snapshot().items)
                capture('hosted-home')
                window.open_action('flash-update')
                pump(lambda: window.prompt is not None)
                assert window.prompt.items[0].key == 'hosted:phx-2'
                assert window.prompt.items[0].tone == 'recommended'
                assert 'downloads from jibo.io when you flash' in window.prompt.items[0].label
                assert window.prompt.items[1].key == 'local:0'
                for size in ('920x640', '1080x720'):
                    root.geometry(size)
                    pump(lambda: root.winfo_width() == int(size.split('x')[0]))
                    geometry()
                print('Geometry checked at 920x640 and 1080x720')
                capture('hosted-chooser')
                window.answer('hosted:phx-2')
                pump(lambda: window.prompt is not None and window.prompt.title == 'Choose calibration handling')
                assert not hosted.started.is_set()
                window.answer('keep-calibration')
                pump(lambda: controller.download_active and hosted.started.is_set() and
                     window._cancel_shown and 'Downloading 25%' in window.progress_status.cget('text'))
                assert window.progress_cancel.winfo_viewable()
                assert window.progress_cancel.winfo_height() >= window.progress_cancel.winfo_reqheight()
                capture('hosted-progress')
                hosted.gate.set()
                pump(lambda: window.prompt is not None and window.prompt.kind == 'confirm')
                assert window.prompt.initial == 'cancel'
                assert policy.PHOENIX_LINE in window.prompt.detail
                window.answer('cancel')
                pump(lambda: not controller.busy)
                assert not api.writes
                print('Verified hosted flow: featured first, calibration before download, Cancel-default review')
                print('Interpreter:', sys.executable, 'Tk:', root.tk.call('package', 'provide', 'Tk'))
        finally:
            hosted.gate.set()
            controller.shutdown()
            pump(lambda: not controller.busy)
            window.close()


if __name__ == '__main__':
    raise SystemExit(main())

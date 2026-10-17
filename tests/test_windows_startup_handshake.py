"""Per-invocation Windows startup handshake, tested without USB or a display."""
from pathlib import Path
from types import SimpleNamespace

import jibo_gui


class Root:
    def __init__(self):
        self.caption = 'Jibo DFU Toolkit (build aaaaaaa)'
        self.jobs = []

    def title(self, value=None):
        if value is not None:
            self.caption = value
        return self.caption

    def after(self, ms, callback):
        self.jobs.append(callback)

    def after_idle(self, callback):
        self.jobs.append(callback)

    def bind(self, *args, **kwargs):
        pass

    def winfo_viewable(self):
        return True


def setup(tmp_path):
    root = Root()
    calls = []
    controller = SimpleNamespace(busy=False, start_action=lambda key: calls.append(key) or True)
    window = SimpleNamespace(close=lambda: calls.append('close'))
    ready = tmp_path / 'ready'
    jibo_gui.supervise_windows_startup(root, controller, window, str(ready))
    return root, controller, ready, calls


def test_mapped_linux_window_cannot_enable_operations_without_windows_ack(tmp_path):
    root, controller, ready, calls = setup(tmp_path)
    for callback in list(root.jobs):
        callback()
    assert ready.read_text() == 'ready\n'
    assert ' [startup ' + tmp_path.name + ']' in root.title()
    assert not controller.start_action('flash-update')
    assert calls == []
    Path(str(ready) + '.visible').touch()
    root.jobs[-1]()
    assert controller.start_action('flash-update')
    assert calls == ['flash-update']
    assert root.title() == 'Jibo DFU Toolkit (build aaaaaaa)'


def test_failed_startup_closes_idle_gui_without_a_hidden_question(tmp_path):
    root, controller, ready, calls = setup(tmp_path)
    Path(str(ready) + '.close').touch()
    root.jobs[-1]()
    assert calls == ['close']
    assert not controller.start_action('flash-update')


def test_startup_close_request_never_interrupts_a_busy_controller(tmp_path):
    root, controller, ready, calls = setup(tmp_path)
    controller.busy = True
    Path(str(ready) + '.close').touch()
    root.jobs[-1]()
    assert calls == []
    controller.busy = False
    root.jobs[-1]()
    assert calls == ['close']


def test_window_probe_fixture_is_in_native_ci():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
    assert 'powershell -NoProfile -File scripts/release/test-window-probe.ps1' in workflow


def test_real_tk_startup_closes_without_hidden_confirmation(tmp_path, monkeypatch):
    import gc
    import os
    import time
    import pytest
    if os.environ.get('JIBO_GUI_TEST_DISPLAY') != '1':
        pytest.skip('requires a display')
    from gui_fakes import FakeApi
    api = FakeApi('none')
    api.BACKUP_ROOT = tmp_path / 'backups'
    root = jibo_gui.create_root()
    controller = jibo_gui.core.GuiController(api, preferences_path=tmp_path / 'prefs.json')
    window = jibo_gui.ToolkitWindow(root, controller)
    ready = tmp_path / 'ready'
    def forbidden(*args, **kwargs):
        raise AssertionError('Startup close asked a hidden question')
    monkeypatch.setattr(jibo_gui.messagebox, 'askyesno', forbidden)
    monkeypatch.setattr(jibo_gui.messagebox, 'showwarning', forbidden)
    try:
        jibo_gui.supervise_windows_startup(root, controller, window, str(ready))
        end = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < end:
            root.update()
            time.sleep(0.01)
        assert ready.read_text() == 'ready\n'
        assert root.winfo_viewable()
        assert not controller.start_action('flash-update')
        Path(str(ready) + '.close').touch()
        while not window.closed and time.monotonic() < end:
            root.update()
            time.sleep(0.01)
        assert window.closed
        assert api.writes == []
    finally:
        if not window.closed:
            window.close()
        gc.collect()

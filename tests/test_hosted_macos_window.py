"""Actual result window and Copy details; fake USB and in-memory clipboard."""
import os
import ssl
import urllib.error
from unittest.mock import patch

import pytest
import jibo_gui_core as core
import jibo_hosted_updates as hosted
from gui_fakes import FakeHostedApi

pytestmark = pytest.mark.skipif(os.environ.get('JIBO_GUI_TEST_DISPLAY') != '1',
                                reason='opt-in real-window tests require Tk and a display')


@pytest.fixture
def failed_window(tmp_path, monkeypatch):
    import jibo_gui as gui
    api = FakeHostedApi()
    api.hosted, api.packages = hosted, []
    controller = core.GuiController(api, prompter=lambda _: None, runner=core.sync_runner,
                                    preferences_path=tmp_path / 'prefs.json')
    root = gui.create_root()
    window = gui.ToolkitWindow(root, controller)
    monkeypatch.setattr(controller.flows, 'hosted_folders', lambda: (tmp_path, tmp_path / 'cache'))
    def denied(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'untrusted'))
    monkeypatch.setattr(hosted, '_deadline_opener', lambda _: type('Opener', (), {'open': denied})())
    controller.request_refresh()
    with core.capture_output(controller):
        assert controller.start_action('flash-update')
    events = controller.drain()
    outcome = [event[1] for event in events if event[0] == 'finished'][-1]
    window.show_outcome(outcome)
    root.update()
    yield window, api
    if not window.closed:
        window.close()


def test_copy_details_contains_transport_cause_and_build(failed_window, monkeypatch):
    window, api = failed_window
    captured = []
    monkeypatch.setattr(window.root, 'clipboard_clear', lambda: None)
    monkeypatch.setattr(window.root, 'clipboard_append', captured.append)
    assert 'copy_details' in window.controls, 'result window lacks Copy details'
    button = window.controls['copy_details']
    assert button.winfo_ismapped()
    button.invoke()
    assert len(captured) == 1
    assert 'TLS certificate verification failed' in captured[0]
    assert window.root.title() in captured[0]
    assert 'Python:' in captured[0] and 'System:' in captured[0]
    assert 'Phoenix image list check failed:' in captured[0]
    assert not api.writes


def test_copy_details_failure_offers_selectable_text(failed_window, monkeypatch):
    import tkinter
    window, _ = failed_window
    def unavailable():
        raise tkinter.TclError('clipboard unavailable')
    monkeypatch.setattr(window.root, 'clipboard_clear', unavailable)
    assert 'copy_details' in window.controls, 'result window lacks Copy details'
    window.controls['copy_details'].invoke()
    window.root.update()
    assert 'Copy failed' in window.controls['copy_details'].cget('text')
    assert window.controls['support_details'].winfo_ismapped()
    assert 'TLS certificate verification failed' in window.controls['support_details'].get()

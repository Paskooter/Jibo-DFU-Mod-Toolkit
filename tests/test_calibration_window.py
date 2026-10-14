"""Real Tk calibration consent: default Share, direct local/escape cancellation."""
import os
import pytest
import jibo_gui_core as core
from test_gui_window import window
from jibo_calibration_flow import CONSENT_TITLE, CONSENT_BODY, SHARE_CHOICES

pytestmark = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1", reason="real Tk display required")

@pytest.fixture(autouse=True)
def sharing_available(monkeypatch):
    monkeypatch.setattr("jibo_calibration_client.CALIBRATION_SHARING_AVAILABLE", True)


def prompt(w):
    return core.Prompt("calibration-consent", CONSENT_TITLE, initial="share", detail=CONSENT_BODY + "\n\n• left camera lens\n• right camera lens\n• camera-to-camera alignment\n• motion sensor corrections\n• microphone levels", items=w.controller.flows._normalise_options(SHARE_CHOICES))

@pytest.mark.parametrize("answer", ["share", "local", "escape"])
def test_consent_default_and_direct_buttons(window, answer):
    w = window
    p = prompt(w)
    w.show_prompt(p); w.root.update()
    assert w.controls["primary"].cget("text") == "Share calibration (recommended)"
    assert w.controls["cancel"].cget("text") == "Keep local only"
    assert w.root.focus_get() is w.controls["primary"]
    assert not p.answered
    if answer == "escape":
        w.root.event_generate("<Escape>"); w.root.update()
        assert p.answered
        assert p.wait() is None
    else:
        w.controls["primary" if answer == "share" else "cancel"].invoke()
        assert p.wait() == answer

@pytest.mark.parametrize("size", [(1080, 720), (900, 640)])
def test_consent_buttons_and_scroll_body_have_space(window, size):
    w = window; w.root.geometry("{}x{}".format(*size))
    w.show_prompt(prompt(w)); w.root.update()
    for name in ("primary", "cancel"):
        widget = w.controls[name]
        assert widget.winfo_height() >= widget.winfo_reqheight()
        assert widget.winfo_rooty() + widget.winfo_height() <= w.root.winfo_rooty() + w.root.winfo_height()
    assert w.controls["disclosure"].get() == w.prompt.detail
    assert w.controls["disclosure"].winfo_height() > 100

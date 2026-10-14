"""Actual Tk widgets; run with JIBO_GUI_TEST_DISPLAY=1 under Xvfb or a desktop."""
import os
import time
from unittest.mock import patch

import pytest
import jibo_gui_core as core
from gui_fakes import FakeApi

pytestmark = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1",
                                reason="opt-in real-window tests require Tk and a display")


@pytest.fixture
def window(tmp_path):
    import jibo_gui as gui
    root = gui.create_root()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(exc)
    c = core.GuiController(FakeApi(), runner=core.sync_runner,
                           preferences_path=tmp_path / "phoenix.json")
    w = gui.ToolkitWindow(root, c)
    root.update()
    yield w
    w.controller._busy = False
    if not w.closed:
        w.close()
    assert not errors, errors


@pytest.mark.parametrize("state", ["none", "multiple", "rcm", "dfu"])
def test_sidebar_readiness_label_does_not_number_four_step_flow(window, state):
    w = window
    w.controller.api.state = state
    assert w.controller.request_refresh()
    w.render_state()
    w.root.update()
    snap = w.controller.snapshot()
    assert w.stepper.steps == ("Connect", "RCM/APX", "DFU", "Choose an action")
    assert w.status_step.cget("text") == "USB READINESS"
    assert w.status_title.cget("text") == snap.headline
    assert w.status_detail.cget("text") == snap.detail
    if state == "dfu":
        assert snap.step_index == 3
        assert w.controller.flows.status_lines(snap.readiness)[0] == (
            "STEP 3 / 3   DFU active; Jibo recovery loader ready")
        p = core.Prompt("confirm", "Confirm var write", detail="Exact review\nUSB port: 1-1")
        w.show_prompt(p)
        w.root.update()
        assert w.status_step.cget("text") == "USB READINESS"
        assert w.controls["review"].get() == p.detail
        w.controls["cancel"].invoke()
        assert p.wait() is None


def boot_prompt(w):
    return core.Prompt("select", "Boot screen network status", initial="install",
                       detail="Install/remove uses one full 500 MiB var transaction with mode and Wi-Fi, rollback backup and SHA-256 readback. No services partition is rewritten. Unknown SSM hashes safely keep the stock screen.",
                       prompt="This diagnostic uses only local network status, not the cloud.",
                       items=w.controller.flows._normalise_options((
                           ("install", "Show Wi-Fi status and IP on the boot screen"),
                           ("keep", "Leave the current boot-screen setting unchanged"),
                           ("remove", "Remove the boot-screen Wi-Fi/IP status"))))


def test_boot_network_checkbox_defaults_on_and_off_means_keep(window):
    w = window
    p = boot_prompt(w)
    w.show_prompt(p)
    w.root.update()
    assert w.controls["boot_enabled"].get() is True
    assert w.controls["checkbox"].winfo_ismapped()
    w.controls["checkbox"].invoke()
    w.controls["primary"].invoke()
    assert p.wait() == "keep"


def test_boot_network_checkbox_on_means_install(window):
    p = boot_prompt(window)
    window.show_prompt(p)
    window.controls["primary"].invoke()
    assert p.wait() == "install"


def test_boot_remove_remains_a_separate_explicit_choice(window):
    p = boot_prompt(window)
    window.show_prompt(p)
    window.controls["remove"].invoke()
    assert p.wait() == "remove"


def test_confirm_defaults_cancel_and_cannot_accept_carried_double_click(window):
    import jibo_gui as gui
    p = core.Prompt("confirm", "Confirm var write", detail="Exact review\nUSB port: 1-1",
                    items=window.controller.flows._normalise_options((("cancel", "Cancel"), ("confirm", "Confirm"))),
                    initial="cancel")
    window.show_prompt(p)
    window.root.update()
    assert window.controls["cancel"] == window.root.focus_get()
    assert window.controls["review"].get() == p.detail
    window.controls["primary"].invoke()
    assert not p.answered
    window.controls["cancel"].event_generate("<Return>")
    window.root.update()
    assert p.answered and p.wait() is None


def test_confirm_only_arms_current_live_prompt(window):
    import jibo_gui as gui
    p = core.Prompt("confirm", "Confirm var write", items=window.controller.flows._normalise_options(
        (("cancel", "Cancel"), ("confirm", "Confirm"))))
    window.show_prompt(p)
    window.root.update()
    deadline = time.monotonic() + gui._CONFIRM_ARM_MS / 1000 + 0.2
    while time.monotonic() < deadline:
        window.root.update()
        time.sleep(0.01)
    assert not window.controls["primary"].instate(["disabled"])
    window.controls["primary"].invoke()
    assert p.wait() == "confirm"


def test_multi_selection_supports_keyboard_without_mouse(window):
    p = core.Prompt("multi", "Choose partitions", items=window.controller.flows._normalise_options(
        (("var", "var"), ("services", "services"))), initial=("var",))
    window.show_prompt(p)
    window.root.update()
    window.page.event_generate("<Down>")
    window.page.event_generate("<space>")
    window.root.update()
    assert window.controls["chosen"] == {"var", "services"}


def test_password_entry_is_hidden_and_cleared_on_accept(window):
    p = core.Prompt("text", "Configure Wi-Fi", prompt="Wi-Fi password:", password=True)
    window.show_prompt(p)
    window.root.update()
    variable = window.controls["value"]
    assert window.controls["entry"].cget("show")
    variable.set("fakepass1")
    window.controls["primary"].invoke()
    assert variable.get() == "" and p.wait() == "fakepass1"


def test_close_refuses_to_interrupt_active_transfer(window):
    window.controller._busy = True
    with patch("jibo_gui.messagebox.showwarning") as warn:
        window.close()
    assert warn.call_count == 1 and not window.closed
    window.controller._busy = False


def test_close_cannot_treat_already_approved_prompt_as_cancellable(window):
    window.controller._busy = True
    pending = core.Prompt("confirm", "Confirm var write")
    pending.resolve("confirm")
    window.controller._pending = pending
    with patch("jibo_gui.messagebox.showwarning") as warning, \
            patch("jibo_gui.messagebox.askyesno", return_value=True) as question:
        window.close()
    assert warning.call_count == 1 and not question.called and not window.closed
    window.controller._busy = False


def test_full_var_flow_titles_do_not_claim_quick_edit(window):
    window.controller._active_label = "Set mode and configure Wi-Fi (quick edit)"
    assert "quick edit" not in window._kicker()
    window.show_outcome(core.Outcome("result", "set-mode-wifi",
                       "Set mode and configure Wi-Fi (quick edit)", ("Done",), "verified"))
    text = [str(widget.cget("text")) for widget in window.page.winfo_children()
            if "text" in widget.keys()]
    assert not any("quick edit" in value for value in text)


def test_close_prompt_defaults_no_and_does_not_quit_on_refusal(window):
    window.controller._busy = True
    window.controller._pending = core.Prompt("text", "Wi-Fi")
    with patch("jibo_gui.messagebox.askyesno", return_value=False) as question:
        window.close()
    assert question.call_args.kwargs["default"] == "no"
    assert not window.closed
    window.controller._busy = False


def test_progress_displays_numeric_percentage_and_operation_log(window):
    window.controller.write_output("Reading var\n\r[########------------] 40.0% | 200/500 MiB")
    window.show_progress()
    window.root.update()
    assert float(window.progress_bar.cget("value")) == 40
    assert window.progress_step.cget("text") == "Reading var"
    assert "200/500 MiB" in window.progress_status.cget("text")
    assert window.progress_log.get() == "Reading var"


PHOENIX_STEP_3 = ("3. At jibo.io, sign in and choose Robots \u2192 Add a Jibo. "
                  "Run the one-line command it gives you; it asks for his IP.")
PHOENIX_PREREQUISITE = ("He needs int-developer mode and your Wi-Fi first. "
                        "Use Set mode and configure Wi-Fi here if he isn't set up yet.")
PHOENIX_CARD_LINES = (
    "Next: connect him to Phoenix",
    "Optional. Phoenix setup happens at jibo.io, not in this toolkit.",
    "1. Restart Jibo normally, out of recovery mode.",
    "2. Note the IP on his boot screen (or in your router's device list).",
    PHOENIX_STEP_3,
)


def card_labels(card):
    """Visible label texts inside a Phoenix card, in display order."""
    labels = []
    def walk(widget):
        for child in widget.winfo_children():
            if child.winfo_class() == "Label" and child.winfo_viewable():
                labels.append(child)
            walk(child)
    walk(card)
    labels.sort(key=lambda label: label.winfo_rooty())
    return labels


def visible_texts(widget):
    texts = []
    if widget.winfo_viewable() and "text" in widget.keys():
        texts.append(str(widget.cget("text")))
    for child in widget.winfo_children():
        texts.extend(visible_texts(child))
    return texts


@pytest.mark.parametrize("guide", [False, True], ids=["completion", "guidance"])
def test_phoenix_step_3_matches_owner_copy_on_both_surfaces(window, guide):
    if guide:
        window.phoenix_entry.invoke()
    else:
        window.show_outcome(core.Outcome("result", "set-mode-wifi", "Setup", ("Saved.",),
                                         "verified", "int-developer"))
    window.root.update()
    steps = [str(label.cget("text")) for label in card_labels(window.controls["phoenix_card"])
             if str(label.cget("text")).startswith("3. ")]
    assert steps == [PHOENIX_STEP_3]


def test_phoenix_completion_card_has_exact_steps_and_no_prerequisite_line(window):
    w = window
    w.show_outcome(core.Outcome("result", "set-mode-wifi", "Set mode and configure Wi-Fi",
                                ("Robot mode set to int-developer.", "Saved Wi-Fi network."),
                                "verified", "int-developer"))
    w.root.update()
    card = w.controls["phoenix_card"]
    assert [label.cget("text") for label in card_labels(card)] == list(PHOENIX_CARD_LINES)
    texts = visible_texts(w.root)
    assert texts.count(PHOENIX_STEP_3) == 1
    assert not any("int-developer mode and your Wi-Fi" in text for text in texts)
    assert not any("Enter his IP" in text for text in texts)


def test_phoenix_guidance_page_adds_prerequisite_between_intro_and_steps(window):
    w = window
    w.phoenix_entry.invoke()
    w.root.update()
    assert w.page_name == "phoenix"
    card = w.controls["phoenix_card"]
    assert [label.cget("text") for label in card_labels(card)] == [
        *PHOENIX_CARD_LINES[:2], PHOENIX_PREREQUISITE, *PHOENIX_CARD_LINES[2:]]
    texts = visible_texts(w.root)
    assert texts.count(PHOENIX_PREREQUISITE) == 1 and texts.count(PHOENIX_STEP_3) == 1
    assert not any("Enter his IP" in text for text in texts)
    w.controls["primary"].invoke()  # Back to actions: the guide never changes dismissal
    w.root.update()
    assert w.page_name == "home" and not w.controller._phoenix_dismissed


def test_phoenix_card_absent_after_successful_developer_mode_setup(window):
    w = window
    w.show_outcome(core.Outcome("result", "set-mode-wifi", "Set mode and configure Wi-Fi",
                                ("Robot mode set to developer.", "Saved Wi-Fi network."),
                                "verified", "developer"))
    w.root.update()
    assert w.page_name == "result" and "phoenix_card" not in w.controls
    assert not any("Phoenix" in text for text in visible_texts(w.content))
    assert w.controls["primary"].cget("text") == "Back to actions"


def test_phoenix_completion_card_is_inline_dismissible_and_opens_only_on_click(window):
    w = window
    opened = []
    w.controller._browser_opener = lambda url: opened.append(url) or True
    result = core.Outcome("result", "set-mode-wifi", "Set mode and configure Wi-Fi",
                          ("Robot mode set to int-developer.", "Saved Wi-Fi network."),
                          "verified", "int-developer")
    w.show_outcome(result)
    w.root.update()
    assert w.page_name == "result" and w.controls["phoenix_card"].winfo_ismapped()
    assert w.controls["phoenix_title"].cget("text") == "Next: connect him to Phoenix"
    assert w.controls["primary"].cget("text") == "Back to actions"
    assert not opened
    w.controls["phoenix_open"].invoke()
    assert opened == ["https://jibo.io/guide"]
    w.controls["phoenix_dismiss"].invoke()
    w.root.update()
    assert w.page_name == "result" and "phoenix_card" not in w.controls
    w.show_outcome(result)
    assert "phoenix_card" not in w.controls
    assert not w.controller.api.calls


@pytest.mark.parametrize("state", ["none", "rcm", "dfu"])
def test_quiet_phoenix_entry_works_offline_after_dismissal(window, state):
    w = window
    opened = []
    w.controller._browser_opener = lambda url: opened.append(url) or True
    w.controller.dismiss_phoenix()
    w.controller.api.state = state
    w.controller.request_refresh()
    w.render_state()
    w.root.update()
    assert w.page_name == "home"  # no launch or USB-change popup
    assert w.phoenix_entry.cget("text") == "Connect to Phoenix (jibo.io)"
    assert w.phoenix_entry.winfo_viewable()
    assert not w.phoenix_entry.instate(["disabled"])
    w.phoenix_entry.invoke()
    w.root.update()
    assert w.page_name == "phoenix" and not opened
    assert w.controls["phoenix_card"].winfo_viewable()
    w.controls["phoenix_open"].invoke()
    assert opened == ["https://jibo.io/guide"]
    w.controls["phoenix_dismiss"].invoke()
    assert w.page_name == "home" and not w.controller.api.calls


def test_default_actions_keep_all_rows_visible_with_quiet_phoenix_entry(window):
    w = window
    w.root.update()
    last = w.action_rows["more-tools"]
    canvas = w.actions.canvas
    assert last.winfo_rooty() + last.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
    entry = w.phoenix_entry
    assert entry.winfo_rootx() + entry.winfo_width() <= w.content.winfo_rootx()


@pytest.mark.parametrize("state", ["none", "multiple", "rcm", "dfu"])
def test_phoenix_sidebar_link_fits_at_minimum_window_size(window, state):
    w = window
    w.controller.api.state = state
    w.controller.request_refresh()
    w.render_state()
    w.root.geometry("920x640")
    w.root.update()
    entry = w.phoenix_entry
    assert entry.winfo_viewable()
    assert entry.winfo_height() >= entry.winfo_reqheight()
    assert entry.winfo_width() >= entry.winfo_reqwidth()
    assert entry.winfo_rooty() + entry.winfo_height() <= w.root.winfo_rooty() + 640


@pytest.mark.parametrize("geometry", ["920x640", "1080x720"])
@pytest.mark.parametrize("guide", [False, True])
def test_phoenix_card_controls_fit_at_minimum_and_docs_window_sizes(window, guide, geometry):
    w = window
    w.root.geometry(geometry)
    if guide:
        w.show_phoenix()
    else:
        w.show_outcome(core.Outcome("result", "set-mode-wifi", "Set mode and configure Wi-Fi",
                                   ("Robot mode set to int-developer.", "Saved Wi-Fi network.",
                                    "Operation record: ~/Jibo-Backups/mode-wifi"),
                                   "verified", "int-developer"))
    w.root.update()
    card = w.controls["phoenix_card"]
    area = card.master.master.master  # ScrollArea -> canvas -> inner -> card
    for label in card_labels(card):
        assert label.winfo_height() >= label.winfo_reqheight()
        assert label.winfo_rootx() >= area.canvas.winfo_rootx()
        assert label.winfo_rootx() + label.winfo_width() <= (
            area.canvas.winfo_rootx() + area.canvas.winfo_width())
    area.canvas.yview_moveto(1)
    w.root.update()
    for key in ("phoenix_open", "phoenix_dismiss", "primary"):
        widget = w.controls[key]
        assert widget.winfo_viewable()
        assert widget.winfo_width() >= widget.winfo_reqwidth()
        assert widget.winfo_height() >= widget.winfo_reqheight()
        viewport = w.root if key == "primary" else area.canvas
        assert widget.winfo_rooty() >= viewport.winfo_rooty()
        assert widget.winfo_rooty() + widget.winfo_height() <= (
            viewport.winfo_rooty() + viewport.winfo_height())


def test_phoenix_entry_cannot_replace_active_transfer_or_prompt(window):
    w = window
    w.controller._busy = True
    w.show_progress()
    w.render_state()
    assert w.phoenix_entry.instate(["disabled"])
    w.show_phoenix()
    assert w.page_name == "progress"
    w.controller._busy = False


@pytest.mark.parametrize("kind,status,mode", [
    ("result", "verified", "developer"),
    ("result", "already current", "developer"),
    ("result", "verified", "normal"),
    ("result", "verified", "oobe"),
    ("result", "cancelled", "int-developer"),
    ("error", "verified", "int-developer"),
    ("result", "verified", ""),
])
def test_phoenix_card_absent_on_ineligible_completion(window, kind, status, mode):
    window.show_outcome(core.Outcome(kind, "set-mode-wifi", "Setup", ("Summary",), status, mode))
    window.root.update()
    assert "phoenix_card" not in window.controls
    assert window.controls["primary"].winfo_viewable()


def test_phoenix_not_now_survives_real_window_recreation(window):
    import jibo_gui as gui
    result = core.Outcome("result", "set-mode-wifi", "Setup", ("Saved.",), "verified", "int-developer")
    prefs = window.controller._preferences_path
    window.show_outcome(result)
    window.controls["phoenix_dismiss"].invoke()
    window.close()
    root = gui.create_root()
    fresh = gui.ToolkitWindow(root, core.GuiController(FakeApi(), runner=core.sync_runner,
                                                      preferences_path=prefs))
    try:
        fresh.show_outcome(result)
        root.update()
        assert "phoenix_card" not in fresh.controls
        fresh.show_home()
        fresh.phoenix_entry.invoke()
        root.update()
        assert fresh.controls["phoenix_card"].winfo_viewable()
    finally:
        fresh.close()


def test_real_gui_main_wires_local_dismissal_path(tmp_path, monkeypatch):
    import jibo_gui as gui
    api = FakeApi()
    api.BACKUP_ROOT = tmp_path / "Jibo-Backups"
    captured = []
    class Root:
        def mainloop(self):
            captured[0].dismiss_phoenix()
    monkeypatch.setattr(gui, "create_root", Root)
    monkeypatch.setattr(gui, "ToolkitWindow", lambda _root, c: captured.append(c))
    assert gui.main(api) == 0
    assert (api.BACKUP_ROOT / ".phoenix-guidance.json").is_file()
    assert not api.calls


def test_error_result_is_visibly_failed(window):
    window.show_outcome(core.Outcome("error", "write-var", "Action failed", ("Fake readback mismatch",)))
    window.root.update()
    assert window.page_name == "result" and window.controls["tone"] == "danger"

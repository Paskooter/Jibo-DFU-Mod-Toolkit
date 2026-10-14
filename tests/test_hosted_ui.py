"""Hosted Phoenix images in the TUI and GUI: synthetic backend only, no network/USB.

Headless tests always run. The real-window class at the end opts in with
JIBO_GUI_TEST_DISPLAY=1 (run under xvfb-run) like tests/test_gui_window.py.
"""
import os
import queue
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import jibo_gui_core as core
import jibo_tui
from gui_fakes import (Catalog, FakeApi, FakeHosted, FakeHostedApi, HostedError, HostedImage,
                       Progress, sample_catalog)


def keys(items):
    return [item.key for item in items]


# -- chooser model (pure) -------------------------------------------------------
def test_latest_first_recommended_older_collapsed_and_local_order_kept():
    packages = [Path("updates/b-official.tar.bz2"), Path("updates/a-official.tar.bz2")]
    items, choices = jibo_tui.hosted_chooser_items(sample_catalog(), (), packages)
    assert keys(items) == ["hosted:phx-2", "local:0", "local:1", "older"]
    assert items[0].tone == "recommended" and items[0].label.startswith(jibo_tui.LATEST_LABEL)
    assert [items[1].label, items[2].label] == ["b-official.tar.bz2", "a-official.tar.bz2"]
    assert items[3].label == "Show older Phoenix images (1)" and "hosted:phx-1" not in choices
    expanded, choices = jibo_tui.hosted_chooser_items(sample_catalog(), (), packages, show_older=True)
    assert keys(expanded)[-2:] == ["older", "hosted:phx-1"]
    assert expanded[-2].label == "Hide older Phoenix images"
    assert choices["hosted:phx-1"][1].version == "1.0.0"


def test_incompatible_latest_is_disabled_and_never_promotes_an_older_image():
    images = (HostedImage("phx-2", "2.0.0", is_latest=True, compatible=False,
                          incompatible_reason="Needs a newer toolkit."),
              HostedImage("phx-1", "1.0.0", sha256="1" * 64))
    items, choices = jibo_tui.hosted_chooser_items(Catalog(images, "phx-2", from_cache=True), ())
    assert items[0].key == "hosted:phx-2" and not items[0].enabled
    assert "Needs a newer toolkit." in items[0].reason and "(saved list)" in items[0].label
    assert [item.key for item in items if item.tone == "recommended"] == ["hosted:phx-2"]
    assert "hosted:phx-2" not in choices and "hosted:phx-1" not in choices


@pytest.mark.parametrize("code", ["offline", "signature", "expired", "http", "rate_limited",
                                  "dependency", "hash", "disk"])
def test_catalog_errors_are_inline_and_local_packages_stay_usable(code):
    items, choices = jibo_tui.hosted_chooser_items(None, (), [Path("updates/x.tar.bz2")],
                                                   error_code=code)
    assert keys(items) == ["status:catalog", "local:0"]
    status = items[0]
    assert not status.enabled and status.tone == "status"
    assert jibo_tui.hosted_error_text(code) in status.label
    assert "Local packages below still work." in status.label
    assert choices == {"local:0": ("local", Path("updates/x.tar.bz2"))}


def test_saved_list_error_uses_code_and_says_list_is_old():
    catalog = sample_catalog(from_cache=True, error="offline: connection refused")
    items, _ = jibo_tui.hosted_chooser_items(catalog, ())
    status = next(item for item in items if item.key == "status:catalog")
    assert jibo_tui.hosted_error_text("offline") in status.label
    assert "Showing the last checked list." in status.label
    assert "connection refused" not in status.label


def test_previously_downloaded_images_are_hidden_not_promoted_to_saved_choices():
    path = "updates/phoenix-phx-0.tar.bz2"
    saved = HostedImage("phx-0", "0.9.0", sha256="0" * 64, downloaded_path=path)
    packages = [Path(path), Path("updates/official.tar.bz2")]
    items, choices = jibo_tui.hosted_chooser_items(sample_catalog(), (saved,), packages)
    assert keys(items) == ["hosted:phx-2", "local:1", "older"]
    assert "hosted:phx-0" not in choices
    assert all("downloaded" not in item.label for item in items)


def test_progress_line_has_bytes_percent_rate_and_time_left():
    line = jibo_tui.format_progress(Progress("download", 1500000, 6000000, 25.0, 500000.0, 90.0))
    assert line == "Downloading 25% \u2014 1.5 MB of 6.0 MB, 500.0 KB/s, about 2 min left"
    assert jibo_tui.format_progress(Progress("verify", 6, 6, 100.0, None, None)).startswith(
        "Checking the download 100%")


def test_both_fresh_choices_are_enabled_for_hosted_images():
    items = jibo_tui.hosted_policy_items(FakeHosted(), HostedImage("phx-2", "2.0.0"))
    assert [(i.key, i.enabled) for i in items] == [("keep-calibration", True), ("without-calibration", True)]

# -- menu ------------------------------------------------------------------------
def test_only_install_is_offered_and_needs_dfu():
    api = FakeHostedApi(state="none")
    items = {item.key: item for item in jibo_tui.TerminalMenu(api).items}
    assert "download-phoenix" not in items
    assert not items["flash-update"].enabled
    dfu = {item.key: item for item in jibo_tui.TerminalMenu(FakeHostedApi(state="dfu")).items}
    assert dfu["flash-update"].enabled and dfu["flash-update"].label == jibo_tui.UPDATE_ACTION_LABEL


def test_old_device_layer_keeps_the_original_menu():
    """Control: without the hosted API the menu is exactly the pre-hosted one."""
    api = FakeApi()
    api.packages = []
    items = {item.key: item for item in jibo_tui.TerminalMenu(api).items}
    assert "download-phoenix" not in items
    assert items["flash-update"].label == "Install an official update package"
    assert not items["flash-update"].enabled
    assert items["flash-update"].reason == "No packages found in ./updates."


# -- TUI flow (prompts patched, no curses) ----------------------------------------
class Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def select(self, title, options, prompt="", detail="", cancel_label="Cancel", initial=None):
        items = jibo_tui._normalise_options(options)
        self.prompts.append((title, items, detail, initial))
        answer = self.answers.pop(0) if self.answers else None
        enabled = {item.key for item in items if item.enabled}
        return answer if answer in enabled else None


def run_tui(api, key, *answers, readiness=None):
    answers = Answers(*answers)
    readiness = readiness or jibo_tui.inspect_readiness(api)
    with patch.object(jibo_tui, "select_option", answers.select), \
            patch.object(jibo_tui, "_terminal_available", return_value=False):
        result = jibo_tui.execute_action(api, key, readiness)
    return result, answers.prompts


@pytest.mark.parametrize("review", [None, "cancel"])
def test_tui_install_review_defaults_to_cancel_and_cancel_writes_nothing(review):
    api = FakeHostedApi()
    result, prompts = run_tui(api, "flash-update", "hosted:phx-2", "keep-calibration", review)
    assert result["status"] == "cancelled" and api.writes == []
    title, items, detail, initial = prompts[-1]
    assert title == "Confirm Phoenix image install" and initial == "cancel"
    assert keys(items) == ["cancel", "confirm"]
    for text in ("not an official Jibo release", "no boot0/boot1, BCT, GPT",
                 "Var write: 524288000 bytes", "Only var gets a rollback copy"):
        assert text in detail
    policy_title, policy_items, _, policy_initial = prompts[1]
    assert policy_initial == "keep-calibration" and keys(policy_items) == ["keep-calibration", "without-calibration"]
    assert policy_items[1].enabled


def test_tui_confirmed_install_uses_public_hosted_flash_with_keep_calibration():
    api = FakeHostedApi()
    result, _ = run_tui(api, "flash-update", "hosted:phx-2", "keep-calibration", "confirm")
    assert result["status"] == "verified" and api.writes == ["flash_hosted_update"]
    name, args, kwargs = api.calls[-1]
    assert name == "flash_hosted_update" and args == ("updates/phoenix-phx-2.tar.bz2",)
    assert kwargs == {"var_policy": "keep-calibration", "port": "1-1", "resume_from": None}
    assert not any(call[0] == "flash_update" for call in api.calls)
    hosted_calls = [call[0] for call in api.hosted.calls]
    assert hosted_calls[:2] == ["fetch_catalog", "local_hosted_images"]
    assert api.hosted.calls[0][2] == jibo_tui.CHOOSER_TIMEOUT
    assert api.hosted.calls[0][1].endswith(os.path.join("updates", ".hosted"))
    summary = jibo_tui._result_summary("flash-update", result)
    assert summary == ["Phoenix 2.0.0 install: verified.", "Record: updates/op/flash-update.json"]
    # Control: an official package result keeps the original one-line summary.
    assert jibo_tui._result_summary("flash-update", {"status": "verified", "manifest": "m"}) == [
        "Official update: verified."]


def test_tui_offline_list_still_installs_a_local_package():
    api = FakeHostedApi(FakeHosted(error=HostedError("offline")),
                        packages=["updates/official.tar.bz2"])
    result, prompts = run_tui(api, "flash-update", "local:0", "keep-calibration", "confirm")
    assert api.writes == ["flash_update"]
    assert keys(prompts[0][1]) == ["status:catalog", "local:0"]


def test_tui_local_phoenix_archive_offers_the_same_two_fresh_choices():
    api = FakeHostedApi(FakeHosted(error=HostedError("offline")),
                        packages=["updates/phoenix-phx-9.tar.bz2"])
    result, prompts = run_tui(api, "flash-update", "local:0", "fresh")
    assert result["status"] == "cancelled" and api.writes == []
    _, items, _, _ = prompts[1]
    assert [(item.key, item.enabled) for item in items] == [
        ("keep-calibration", True), ("without-calibration", True)]


def test_tui_download_failure_is_reported_without_install():
    hosted = FakeHosted()
    hosted.download_error = HostedError("hash", "sha mismatch")
    api = FakeHostedApi(hosted)
    result, prompts = run_tui(api, "flash-update", "hosted:phx-2", "keep-calibration")
    assert result == {"status": "cancelled", "message": jibo_tui.hosted_error_text("hash")}
    assert api.writes == [] and len(prompts) == 2


def test_tui_canceled_check_shows_the_last_signed_list():
    hosted = FakeHosted(error=HostedError("canceled"))
    hosted.cached = sample_catalog()
    api = FakeHostedApi(hosted)
    result, prompts = run_tui(api, "flash-update")
    items = prompts[0][1]
    assert keys(items)[:2] == ["hosted:phx-2", "status:catalog"]
    assert "(saved list)" in items[0].label
    assert jibo_tui.hosted_error_text("check-canceled") in items[1].label
    assert result["status"] == "cancelled" and api.writes == []


def test_tui_local_archive_with_phoenix_plan_gets_the_phoenix_review():
    class MarkedLocal(FakeHostedApi):
        def flash_update(self, package, var_policy, port=None, confirmation=None,
                         unsafe_without_calibration=False):
            self.calls.append(("flash_update", (str(package),), {"var_policy": var_policy}))
            plan = {"package": str(package), "phoenix": {"id": "phx-9", "scope": "Userspace only"}}
            return {"status": "verified" if confirmation(plan) else "cancelled"}
    api = MarkedLocal(FakeHosted(error=HostedError("offline")), packages=["updates/renamed.tar.bz2"])
    result, prompts = run_tui(api, "flash-update", "local:0", "keep-calibration", None)
    assert result["status"] == "cancelled"
    title, _, detail, initial = prompts[-1]
    assert title == "Confirm Phoenix image install" and initial == "cancel"
    assert "not an official Jibo release" in detail






def test_tui_failed_install_keeps_record_and_does_not_offer_unsafe_resume():
    api = FakeHostedApi()
    api.fail_flashes = 1
    with pytest.raises(RuntimeError, match="Update record: updates/op/update-manifest.json"):
        run_tui(api, "flash-update", "hosted:phx-2", "keep-calibration")
    assert api.writes == [] and len([c for c in api.calls if c[0] == 'flash_hosted_update']) == 1


def test_failed_update_record_parses_only_the_backend_line():
    assert jibo_tui.failed_update_record(
        RuntimeError("x\nUpdate record: /a b/update-manifest.json")) == "/a b/update-manifest.json"
    assert jibo_tui.failed_update_record(RuntimeError("no record here")) is None


def test_tui_removed_download_action_is_rejected_without_network():
    api = FakeHostedApi(state="none")
    with pytest.raises(ValueError, match="Unknown menu action"):
        run_tui(api, "download-phoenix", "hosted:phx-2")
    assert api.writes == [] and api.hosted.calls == []


class ProgressScreen:
    def __init__(self, keys=()):
        self.keys = list(keys)
        self.lines = []

    def erase(self):
        pass

    def getmaxyx(self):
        return (24, 80)

    def addnstr(self, y, x, text, length, attr=0):
        self.lines.append(text[:length])

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def timeout(self, _ms):
        pass

    def getch(self):
        time.sleep(0.01)
        return self.keys.pop(0) if self.keys else -1


def test_tui_progress_screen_cancels_on_c_and_keeps_drawing():
    hosted = FakeHosted()
    token = hosted.CancelToken()
    events = queue.Queue()
    events.put(Progress("download", 1000, 4000, 25.0, 500.0, 6.0))
    thread = threading.Thread(target=lambda: token._event.wait(5), daemon=True)
    thread.start()
    screen = ProgressScreen([-1, ord("c")])
    with patch.object(jibo_tui.curses, "curs_set", lambda *_: None), \
            patch.object(jibo_tui.curses, "start_color", lambda: None), \
            patch.object(jibo_tui.curses, "use_default_colors", lambda: None):
        jibo_tui._progress_session(screen, "Downloading", "", events, token, thread)
    assert token.cancelled
    assert any(line.startswith("Downloading 25%") for line in screen.lines)


# -- GUI controller (headless, real worker threads) ---------------------------------
GATES = []


@pytest.fixture(autouse=True)
def release_gates():
    """Release every held synthetic download so no worker thread outlives its test."""
    yield
    while GATES:
        GATES.pop().set()


def gated():
    hosted = FakeHosted()
    hosted.gate = threading.Event()
    GATES.append(hosted.gate)
    return hosted


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def gui_controller(api, answers):
    remaining = list(answers)
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return remaining.pop(0) if remaining else None
    c = core.GuiController(api, prompter=ask, runner=core.sync_runner)
    c.request_refresh()
    c._runner = core.thread_runner
    return c, prompts


def finished(c, timeout=5.0):
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen.extend(c.drain())
        done = [event[1] for event in seen if event[0] == "finished"]
        if done:
            return done[-1], seen
        time.sleep(0.005)
    raise AssertionError("action did not finish")


def test_gui_network_work_runs_off_main_thread_and_progress_is_marshalled():
    api = FakeHostedApi()
    c, _ = gui_controller(api, ["hosted:phx-2", "keep-calibration", "cancel"])
    assert c.start_action("flash-update")
    result, events = finished(c)
    assert result.status == "cancelled" and api.writes == []
    main = threading.main_thread()
    assert api.hosted.threads and all(thread is not main for thread in api.hosted.threads)
    assert ("download", True) in events and ("download", False) in events
    assert any("Downloading 25%" in line for line in c.output_state().lines) or \
        "Download complete" in c.output_state().status


def test_gui_cancel_download_keeps_partial_writes_nothing_and_drops_late_callbacks():
    hosted = gated()
    api = FakeHostedApi(hosted)
    c, prompts = gui_controller(api, ["hosted:phx-2", "keep-calibration"])
    assert c.start_action("flash-update")
    assert hosted.started.wait(5) and wait_until(lambda: c.download_active)
    assert not c.start_action("flash-update")          # no double start
    assert not c.start_action("download-phoenix")
    assert c.cancel_download()
    result, _ = finished(c)
    assert result.status == "cancelled"
    assert result.lines == (jibo_tui.hosted_error_text("canceled"),)
    assert api.writes == [] and not any(call[0] == "flash_hosted_update" for call in api.calls)
    assert len(prompts) == 2                           # calibration first; no review after cancel
    before = c.output_state()
    hosted.progress(Progress("download", 3000, 4000, 75.0, 1.0, 1.0))   # late callback
    assert c.output_state() == before and not c.download_active


def test_gui_close_during_download_cancels_it_without_refusing():
    hosted = gated()
    api = FakeHostedApi(hosted)
    c, _ = gui_controller(api, ["hosted:phx-2", "keep-calibration"])
    assert c.start_action("flash-update")
    assert hosted.started.wait(5) and wait_until(lambda: c.download_active)
    assert c.shutdown_if_safe()
    assert hosted.cancel.cancelled
    result, _ = finished(c)
    assert result.status == "cancelled" and api.writes == []


def test_gui_robot_operation_still_refuses_close():
    """Control: the pre-hosted rule (no close during an approved transfer) is unchanged."""
    api = FakeHostedApi()
    c, _ = gui_controller(api, [])
    with c._lock:
        c._busy = True
    assert not c.shutdown_if_safe()


def test_gui_confirmed_install_review_is_cancel_default():
    api = FakeHostedApi()
    c, prompts = gui_controller(api, ["hosted:phx-2", "keep-calibration", "confirm"])
    assert c.start_action("flash-update")
    result, _ = finished(c)
    assert result.status == "verified" and api.writes == ["flash_hosted_update"]
    review = prompts[-1]
    assert review.kind == "confirm" and review.initial == "cancel"
    assert review.title == "Confirm Phoenix image install"


# -- frozen-API contract (runs once the real backend is in the same tree) -------------
def test_fakes_match_the_frozen_hosted_api_when_the_backend_is_present():
    """Fails on any data-class or signature drift between these fakes and jibo_hosted_updates."""
    import dataclasses
    import inspect
    import gui_fakes
    real = pytest.importorskip("jibo_hosted_updates")
    for name in ("HostedImage", "Catalog", "Progress"):
        assert [f.name for f in dataclasses.fields(getattr(real, name))] == \
            [f.name for f in dataclasses.fields(getattr(gui_fakes, name))], name
    for name in ("fetch_catalog", "download_image", "local_hosted_images", "hosted_var_policies"):
        assert list(inspect.signature(getattr(real, name)).parameters) == \
            list(inspect.signature(getattr(FakeHosted, name)).parameters)[1:], name
    dfu = pytest.importorskip("jibo_dfu")
    if hasattr(dfu, "flash_hosted_update"):
        assert list(inspect.signature(dfu.flash_hosted_update).parameters) == \
            list(inspect.signature(FakeHostedApi.flash_hosted_update).parameters)[1:]
        assert getattr(dfu, "hosted", None) is real


# -- real Tk window (opt-in, run under xvfb-run) ----------------------------------
needs_display = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1",
                                   reason="opt-in real-window tests require Tk and a display")


@pytest.fixture
def hosted_window(tmp_path):
    """Real Tk window over a synthetic hosted backend; message boxes never block."""
    import jibo_gui as gui
    hosted = gated()
    api = FakeHostedApi(hosted, state="dfu")
    root = gui.create_root()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(exc)
    c = core.GuiController(api, runner=core.sync_runner, preferences_path=tmp_path / "p.json")
    asked = []
    answers = {"close": False}

    def askyesno(title, *_args, **_kwargs):
        asked.append(title)
        return answers["close"]
    with patch.object(gui.messagebox, "askyesno", askyesno), \
            patch.object(gui.messagebox, "showwarning", lambda *a, **k: asked.append(a[0])):
        w = gui.ToolkitWindow(root, c)
        root.update()
        c._runner = core.thread_runner
        try:
            yield w, hosted, api, asked, answers
        finally:
            hosted.gate.set()
            answers["close"] = True
            if not w.closed:
                w.close()
            assert wait_until(lambda: not c.busy)
    assert not errors, errors


def pump(w, predicate, timeout=5.0):
    return wait_until(lambda: (w.root.update(), predicate())[1], timeout)


def start_download(w, hosted):
    assert pump(w, lambda: w.controller.snapshot().checked)
    w.open_action("flash-update")
    assert pump(w, lambda: w.prompt is not None)       # shown by the Tk poll, not the worker
    prompt = w.prompt
    assert [item.key for item in prompt.items][0] == "hosted:phx-2"
    w.answer("hosted:phx-2")
    assert pump(w, lambda: w.prompt is not None and w.prompt.title == "Choose calibration handling")
    w.answer("keep-calibration")
    assert hosted.started.wait(5)
    assert pump(w, lambda: w._cancel_shown)


@needs_display
def test_window_stays_responsive_and_cancels_a_download(hosted_window):
    w, hosted, api, _asked, _answers = hosted_window
    start_download(w, hosted)
    ticks = []
    w.root.after(10, lambda: ticks.append(1))
    assert pump(w, lambda: bool(ticks), 2.0)            # Tk loop is not blocked by the download
    assert "Downloading 25%" in w.progress_status.cget("text")
    assert w.progress_cancel.winfo_ismapped()
    w.progress_cancel.invoke()
    assert pump(w, lambda: w.page_name == "result")
    assert api.writes == [] and not w._cancel_shown and hosted.cancel.cancelled


@needs_display
def test_window_close_during_download_asks_then_cancels(hosted_window):
    w, hosted, api, asked, answers = hosted_window
    start_download(w, hosted)
    w.close()
    assert asked == ["Stop the download and quit?"]
    assert not w.closed and not hosted.cancel.cancelled
    answers["close"] = True
    w.close()
    assert w.closed and hosted.cancel.cancelled
    assert wait_until(lambda: not w.controller.busy)
    assert api.writes == []

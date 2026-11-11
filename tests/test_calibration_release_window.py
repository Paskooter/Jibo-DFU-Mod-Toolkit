"""The local-only More tools menu rendered in a real Tk window."""
import os
import socket
from unittest.mock import patch

import pytest
from test_gui_window import window

pytestmark = pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1", reason="real Tk display required")


@pytest.mark.parametrize("poll_due", [False, True])
def test_more_tools_window_has_no_calibration_service_entries(window, monkeypatch, poll_due):
    w = window
    if poll_due:
        # Deterministically deliver the queued 'started' event on the same
        # nested update that used to race the directly rendered fixture prompt.
        real_update = w.root.update
        def update_with_due_poll():
            w.root.after_cancel(w._poll_job)
            w._poll()
            return real_update()
        monkeypatch.setattr(w.root, "update", update_with_due_poll)
    captured = []
    rendered = []
    def ask(prompt):
        captured.append(prompt)
        # This synchronous test prompter renders directly, unlike production's
        # queued prompt. Deliver the earlier 'started' event through the real
        # poller first, so a due timer cannot replace this prompt with progress.
        w.root.after_cancel(w._poll_job)
        w._poll()
        w.show_prompt(prompt)
        w.root.update()
        options = w.controls["options"]
        rendered.append((list(options), [option.winfo_viewable() for option in options.values()]))
        w.controls["cancel"].invoke()
        return prompt.wait()
    w.controller._prompter = ask
    with patch.object(socket, "socket", side_effect=AssertionError("socket opened")) as sockets, \
            patch.object(socket, "create_connection", side_effect=AssertionError("network attempted")) as connections, \
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")) as dns:
        w.controller.request_refresh()
        assert w.controller.start_action("more-tools")
        assert len(captured) == 1
        assert captured[0].title == "More tools"
        assert rendered[0][0] == ["set-mode", "configure-wifi", "probe-dfu-gpt", "write-var",
                                  "remove-boot-network-status", "inspect-backup", "edit-backup"]
        assert all(rendered[0][1])
        outcomes = [event[1] for event in w.controller.drain() if event[0] == "finished"]
        assert outcomes[-1].kind == "result"
        sockets.assert_not_called()
        connections.assert_not_called()
        dns.assert_not_called()

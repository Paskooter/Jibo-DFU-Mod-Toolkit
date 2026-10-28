"""Post-launch entry tests: synthetic USB snapshots and clock, never real USB."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import jibo_dfu as d


LAUNCH = "Starting verified ARM-state SPL at 0x80108000."
APX = {"port": "1-1", "state": "rcm"}


@pytest.fixture
def entry(tmp_path, monkeypatch):
    original_progress = d.run_with_progress
    monotonic, sleep = d.time.monotonic, d.time.sleep
    loader = tmp_path / "loader.bin"
    loader.write_bytes(b"synthetic loader")
    clock = [0.0]
    snapshots = []
    last = [[]]
    listing = Mock(return_value=([d.MARKER, "var"], ""))
    transfer = Mock(return_value=LAUNCH)

    def devices():
        if snapshots:
            last[0] = snapshots.pop(0)
        return [dict(device) for device in last[0]]

    def usb_snapshot(found):
        return [("0955", "7740" if device["state"] == "rcm" else "701a", device["port"])
                for device in found]

    usb = Mock(side_effect=usb_snapshot)
    monkeypatch.setattr(d, "devices", devices)
    # Also bound future diagnostics to the synthetic bus on the old-code RED run.
    monkeypatch.setattr(d, "_entry_usb_snapshot", usb, raising=False)
    monkeypatch.setattr(d, "ROOT", tmp_path)
    (tmp_path / "release-manifest.json").write_text('{"revision": "' + "b" * 40 + '"}')
    monkeypatch.setattr(d, "_utc_now", lambda: "2026-01-01T00:00:00Z")
    monkeypatch.setattr(d, "_shofel_dfu_tool", lambda _: "/synthetic/shofel2_t124")
    monkeypatch.setattr(d, "tool", lambda *_: "synthetic-dfu-util")
    monkeypatch.setattr(d, "run_with_progress", transfer)
    monkeypatch.setattr(d, "dfu_alternatives", listing)
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(d.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def run(states, **kwargs):
        snapshots[:] = states
        return d.enter_shofel_dfu(port="1-1", loader=loader, timeout=kwargs.pop("timeout", 1),
                                  confirm_meerkat_rev02=True, **kwargs)

    return SimpleNamespace(run=run, clock=clock, listing=listing, transfer=transfer, usb=usb,
                           progress=original_progress, monotonic=monotonic, sleep=sleep)


def test_one_new_dfu_on_different_path_is_verified_and_returned(entry):
    result = entry.run([[APX], [], [{"port": "2-3.1", "state": "dfu"}]])
    assert result["port"] == "2-3.1"
    assert result["loader_verified"] is True
    assert result["alternatives"] == [d.MARKER, "var"]
    entry.listing.assert_called_once_with("synthetic-dfu-util", "2-3.1")
    assert entry.transfer.call_args.args[0][1:3] == ["--usb-port-path", "1-1"]
    assert entry.clock[0] < 1


@pytest.mark.parametrize("before, after", [
    ([APX], [{"port": "2-1", "state": "dfu"}, {"port": "2-2", "state": "dfu"}]),
    ([APX, {"port": "1-2", "state": "dfu"}], [{"port": "1-2", "state": "dfu"}]),
    ([APX, {"port": "1-2", "state": "dfu"}], [{"port": "2-1", "state": "dfu"}]),
    ([APX, {"port": "1-2", "state": "rcm"}], [{"port": "2-1", "state": "dfu"}]),
    ([APX], [{"port": "2-1", "state": "dfu"}, {"port": "1-2", "state": "rcm"}]),
])
def test_cross_path_ambiguity_refuses_before_listing(entry, before, after):
    with pytest.raises(d.DfuError, match="Cannot safely choose DFU"):
        entry.run([before, after])
    entry.listing.assert_not_called()
    assert entry.clock[0] == 0  # Refuse clearly, rather than hiding ambiguity as a timeout.


def test_same_path_still_wins_with_other_recovery_devices(entry):
    result = entry.run([[APX, {"port": "1-2", "state": "dfu"}],
                        [{"port": "1-1", "state": "dfu"},
                         {"port": "1-2", "state": "dfu"},
                         {"port": "2-1", "state": "dfu"}]])
    assert result["port"] == "1-1"
    entry.listing.assert_called_once_with("synthetic-dfu-util", "1-1")


def test_cross_path_waits_until_target_apx_disappears(entry):
    result = entry.run([[APX], [APX, {"port": "2-1", "state": "dfu"}],
                        [{"port": "2-1", "state": "dfu"}]])
    assert result["port"] == "2-1"
    assert entry.clock[0] == 0.25


def test_two_new_dfu_paths_across_snapshots_are_still_ambiguous(entry):
    with pytest.raises(d.DfuError, match="Cannot safely choose DFU"):
        entry.run([[APX], [APX, {"port": "2-1", "state": "dfu"}],
                   [{"port": "2-2", "state": "dfu"}]])
    entry.listing.assert_not_called()


def test_second_dfu_appearing_while_alternatives_settle_refuses(entry):
    with pytest.raises(d.DfuError, match="Cannot safely choose DFU"):
        entry.run([[APX], [{"port": "2-1", "state": "dfu"}],
                   [{"port": "2-1", "state": "dfu"}, {"port": "2-2", "state": "dfu"}]])
    entry.listing.assert_called_once_with("synthetic-dfu-util", "2-1")


def test_new_path_must_still_expose_the_jibo_recovery_profile(entry):
    entry.listing.return_value = (["vendor-dfu", "var"], "")
    with pytest.raises(d.DfuError, match="not Jibo's recovery loader"):
        entry.run([[APX], [{"port": "2-1", "state": "dfu"}]])
    assert entry.clock[0] == 1


SAFE_TRANSCRIPT = (
    "Pinned loader validated: 432064 bytes, SHA-256 " + "a" * 64,
    "RAMINIT phase 0 (payload entry): 0x00000001",
    "RAMINIT phase 1 (PMC STRAPPING_OPT_A): 0x00000020",
    "RAMINIT phase 2 (RAM_CODE): 0x00000002",
    "RAMINIT phase 3 (board profile gate): 0x00000001",
    "RAMINIT phase 4 (before SDRAM init): 0x00000002",
    "RAMINIT phase 5 (after SDRAM init): 0x00000001",
    "RAMINIT phase 6 (DRAM size MiB): 0x00000800",
    "RAMINIT phase 7 (required DRAM range flags): 0x0000000f",
    "RAMINIT phase 8 (DRAM scratch word 0): 0x4a424f31",
    "RAMINIT phase 9 (DRAM scratch word 1): 0x74656e65",
    "RAMINIT phase 10 (DRAM scratch word 2): 0x31323334",
    "RAMINIT phase 11 (DRAM scratch word 3): 0xa5c35a3c",
    "RAMINIT phase 12 (DRAM scratch restore flags): 0x000000ff",
    "RAMINIT phase 14 (complete): 0x00000001",
    "Staged 65536 / 432064 bytes",
    "Staged 432064 / 432064 bytes",
    "Loader copied to 0x80108000 and verified by SHA-256 readback.",
    LAUNCH,
)
UNSAFE_TRANSCRIPT = (
    'Chip ID: SYNTHETIC-NOT-LOGGED\nserial="SYNTHETIC-NOT-LOGGED"\n'
    'RAMINIT phase 1 (SYNTHETIC-NOT-LOGGED): 0x00000020\n'
    'RAMINIT phase 2 (RAM_CODE): 0x00000002 SYNTHETIC-NOT-LOGGED\n'
    'Staged 432064 / 432064 bytes SYNTHETIC-NOT-LOGGED\n'
    'Loader copied to 0x80108000 and verified by SHA-256 readback. SYNTHETIC-NOT-LOGGED\n'
    'Starting verified ARM-state SPL at 0x80108000. SYNTHETIC-NOT-LOGGED\n'
)


@pytest.mark.parametrize("result_kind", ["ready", "timeout", "profile"])
def test_successful_helper_transcript_reaches_gui_log_and_support_details(entry, result_kind):
    import jibo_gui_core as core

    collector = core.OutputCollector()
    sink = SimpleNamespace(capturing=True, write_output=collector.feed)
    entry.transfer.return_value = UNSAFE_TRANSCRIPT + "\r".join(SAFE_TRANSCRIPT) + "\n"
    if result_kind == "profile":
        entry.listing.return_value = (["vendor-dfu", "var"], "")
    states = [[APX], [] if result_kind == "timeout" else [{"port": "1-1", "state": "dfu"}]]
    with core.capture_output(sink):
        if result_kind == "ready":
            result = entry.run(states)
            details = result["support_details"]
            assert tuple(result["helper_transcript"]) == SAFE_TRANSCRIPT
        else:
            with pytest.raises(d.DfuError) as caught:
                entry.run(states)
            details = str(caught.value)
    log = "\n".join(collector.lines)
    assert "Details for support" in details
    assert "Details for support" in log
    for line in SAFE_TRANSCRIPT:
        assert line in log
        assert line in details
    assert "SYNTHETIC-NOT-LOGGED" not in log + details


def test_launch_marker_must_be_a_complete_allowlisted_line(entry):
    entry.transfer.return_value = UNSAFE_TRANSCRIPT
    with pytest.raises(d.DfuError, match="did not confirm the loader launch"):
        entry.run([[APX], [{"port": "1-1", "state": "dfu"}]])
    entry.listing.assert_not_called()


def test_timeout_lists_usb_id_paths_first_last_times_and_versions(entry, capsys):
    entry.usb.side_effect = [
        [("0955", "7740", "1-1"), ("1234", "5678", "3-2")],
        [("1234", "5678", "3-2")],
        [("0955", "7740", "1-1"), ("1234", "5678", "3-2")],
        [],
    ]
    with pytest.raises(d.DfuError) as caught:
        entry.run([[APX], [APX], [], [APX], []])
    details = str(caught.value)
    assert "Code: DFU-WAIT-TIMEOUT" in details
    assert "Stage: dfu-usb-wait" in details
    assert "Wait started (UTC): 2026-01-01T00:00:00Z" in details
    assert "Wait ended (UTC): 2026-01-01T00:00:00Z" in details
    assert "Elapsed: 1.000s; limit: 1.000s" in details
    assert "USB snapshots: 4" in details
    assert "USB devices seen during wait: 2" in details
    assert "0955:7740 path=1-1 first=+0.000s last=+0.500s" in details
    assert "1234:5678 path=3-2 first=+0.000s last=+0.500s" in details
    assert "New DFU paths: 0" in details
    assert "Toolkit build: " + "b" * 40 in details
    assert "Python: " in details
    assert "Helper capability: dfu-stage-launch=1" in details
    assert "Check" in details and "try Enter DFU again" in details
    assert entry.clock[0] == 1
    entry.listing.assert_not_called()
    log = capsys.readouterr().out
    import re
    match = re.search(r"Reference: (dfu-[0-9a-f]{12})", details)
    assert match is not None
    assert match[1] in log


def test_timeout_separates_dfu_seen_elsewhere_from_never_seen(entry):
    with pytest.raises(d.DfuError) as caught:
        entry.run([[APX], [APX, {"port": "2-1", "state": "dfu"}]])
    details = str(caught.value)
    assert "DFU appeared" in details
    assert "safe handoff" in details
    assert "0955:701a path=2-1 first=+0.000s last=+0.750s" in details
    assert "New DFU paths: 1" in details
    entry.listing.assert_not_called()


def test_wait_prints_elapsed_progress_without_extending_deadline(entry, capsys):
    with pytest.raises(d.DfuError):
        entry.run([[APX], []], timeout=6)
    assert "Waiting for DFU: 5s of 6s" in capsys.readouterr().out
    assert entry.clock[0] == 6


def test_no_devices_timeout_is_explicit(entry):
    with pytest.raises(d.DfuError) as caught:
        entry.run([[APX], []])
    assert "USB devices seen during wait: 0\n  (none observed)" in str(caught.value)


def test_ambiguity_details_carry_the_device_counts_and_no_partition_claim(entry):
    with pytest.raises(d.DfuError) as caught:
        entry.run([[APX], [{"port": "2-1", "state": "dfu"}, {"port": "2-2", "state": "dfu"}]])
    details = str(caught.value)
    assert "Code: DFU-WAIT-CHOICE" in details
    assert "New DFU paths: 2" in details
    assert "No partition operation was attempted" in details
    assert "0955:701a path=2-1" in details
    assert "0955:701a path=2-2" in details


def test_entry_usb_snapshot_reads_public_descriptors_only(tmp_path):
    for port, vid, pid in (("1-1", "0955", "7740"), ("2-3.1", "0955", "701A"),
                           ("usb1", "1d6b", "0002"), ("3-2", "1234", "5678"),
                           ("private-name", "0955", "701a"), ("4-4", "0955", "not-public")):
        path = tmp_path / port
        path.mkdir()
        (path / "idVendor").write_text(vid + "\n")
        (path / "idProduct").write_text(pid + "\n")
        (path / "serial").write_text("SYNTHETIC-NOT-LOGGED")
        (path / "product").write_text("SYNTHETIC-NOT-LOGGED")
    (tmp_path / "5-1").mkdir()  # A device can disappear between directory and descriptor reads.
    assert hasattr(d, "_entry_usb_snapshot"), "The wait needs a public-descriptor USB snapshot"
    assert set(d._entry_usb_snapshot([APX], root=tmp_path)) == {
        ("0955", "7740", "1-1"), ("0955", "701a", "2-3.1"),
        ("1d6b", "0002", "usb1"), ("1234", "5678", "3-2"),
    }


def test_mac_usb_snapshot_uses_only_supplied_recovery_paths(monkeypatch):
    monkeypatch.setattr(d.sys, "platform", "darwin")
    assert hasattr(d, "_entry_usb_snapshot"), "Mac diagnostics must not read Linux sysfs"
    assert d._entry_usb_snapshot([{"port": "0-3.2", "state": "dfu"}]) == [
        ("0955", "701a", "0-3.2")]


def test_real_progress_capture_retains_only_safe_success_output(entry, monkeypatch, capsys):
    # Exercise the actual subprocess capture/return seam with a Python child, not a USB helper.
    import sys
    monkeypatch.setattr(d.time, "monotonic", entry.monotonic)
    monkeypatch.setattr(d.time, "sleep", entry.sleep)
    raw = UNSAFE_TRANSCRIPT + "\r".join(SAFE_TRANSCRIPT) + "\n"

    def transfer(_argv, **kwargs):
        kwargs.pop("cwd")
        return entry.progress([sys.executable, "-c", "print(" + repr(raw) + ", end='')"], **kwargs)

    entry.transfer.side_effect = transfer
    result = entry.run([[APX], [{"port": "1-1", "state": "dfu"}]])
    assert tuple(result["helper_transcript"]) == SAFE_TRANSCRIPT
    assert "SYNTHETIC-NOT-LOGGED" not in result["support_details"] + capsys.readouterr().out


@pytest.mark.parametrize("manifest", ['{"revision": "SYNTHETIC-NOT-LOGGED"}', '[]', 'not json'])
def test_support_details_do_not_echo_invalid_build_metadata(entry, manifest):
    (d.ROOT / "release-manifest.json").write_text(manifest)
    result = entry.run([[APX], [{"port": "1-1", "state": "dfu"}]])
    assert "Toolkit build: unavailable" in result["support_details"]
    assert "SYNTHETIC-NOT-LOGGED" not in result["support_details"]


def test_linux_device_paths_come_from_current_sysfs_topology(tmp_path):
    old = tmp_path / "1-1"
    old.mkdir()
    (old / "idVendor").write_text("0955\n")
    (old / "idProduct").write_text("7740\n")
    assert d.devices(tmp_path) == [APX]
    (old / "idVendor").unlink()
    (old / "idProduct").unlink()
    old.rmdir()
    new = tmp_path / "2-3.1"
    new.mkdir()
    (new / "idVendor").write_text("0955\n")
    (new / "idProduct").write_text("701a\n")
    assert d.devices(tmp_path) == [{"port": "2-3.1", "state": "dfu"}]


@pytest.mark.skipif(os.environ.get("JIBO_GUI_TEST_DISPLAY") != "1", reason="requires Tk under Xvfb")
def test_timeout_details_can_be_copied_from_real_gui_operation_log(entry, tmp_path):
    import jibo_gui as gui
    import jibo_gui_core as core
    from gui_fakes import FakeApi

    api = FakeApi("rcm")
    api.enter_shofel_dfu = lambda **_: entry.run([[APX], []])
    entry.transfer.return_value = UNSAFE_TRANSCRIPT + "\n".join(SAFE_TRANSCRIPT)
    root = gui.create_root()
    controller = core.GuiController(api, runner=core.sync_runner, prompter=lambda _: "confirm",
                                    preferences_path=tmp_path / "preferences.json")
    window = gui.ToolkitWindow(root, controller)
    try:
        root.update()
        with core.capture_output(controller):
            assert controller.start_action("enter-dfu-shofel")
        outcome = [event[1] for event in controller.drain() if event[0] == "finished"][-1]
        assert outcome.kind == "error"
        window.show_outcome(outcome)
        window.controls["log_toggle"].invoke()
        root.update()
        log = window.controls["log"]
        assert log.winfo_ismapped()
        assert "Code: DFU-WAIT-TIMEOUT" in log.get()
        text = log.text
        text.tag_add("sel", "1.0", "end-1c")
        text.event_generate("<<Copy>>")
        copied = root.clipboard_get()
        assert "Details for support" in copied
        assert all(line in copied for line in SAFE_TRANSCRIPT)
        assert "SYNTHETIC-NOT-LOGGED" not in copied
        assert "USB devices seen during wait: 0" in copied
    finally:
        window.close()

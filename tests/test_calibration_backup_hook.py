"""Exercise the real verified-backup event boundary without USB."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import jibo_dfu as dfu
from jibo_calibration_flow import operation

CID = "0123456789abcdef0123456789abcdef"

class Session:
    def __init__(self):
        self.events = []
    def on_verified_var_backup_ready(self, backup, identity):
        self.events.append((backup, identity))

class HookTests(unittest.TestCase):
    def test_verified_reuse_emits_once_and_failed_backup_emits_nothing(self):
        session = Session()
        existing = {"image": "synthetic.img", "sha256": "a" * 64, "manifest": "synthetic.json", "created_utc": "synthetic"}
        with patch.object(dfu, "tool", return_value="dfu-util"), patch.object(dfu, "_dfu_context", return_value=("1-1", (dfu.MARKER, "var"), "serial-sha256:" + "a" * 64)), patch.object(dfu, "calibration_identity", return_value={"cid_serial": CID, "usb_port": "1-1"}), patch.object(dfu, "_find_verified_backup", return_value=existing), patch.object(dfu, "_verify_var_backup_for_robot") as verify, operation(session):
            result = dfu.backup_var(port="1-1")
            self.assertEqual(result["status"], "existing verified backup reused")
            self.assertEqual(len(session.events), 1)
            verify.side_effect = dfu.DfuError("verification failed")
            with self.assertRaises(dfu.DfuError):
                dfu.backup_var(port="1-1")
            self.assertEqual(len(session.events), 1)
    def test_changed_device_does_not_emit(self):
        session = Session()
        with patch.object(dfu, "_backup_var_without_event", return_value={"status": "backup complete"}), patch.object(dfu, "calibration_identity", side_effect=({"cid_serial": CID, "usb_port": "1-1"}, {"cid_serial": "1" * 32, "usb_port": "1-1"})), operation(session):
            result = dfu.backup_var(port="1-1")
            self.assertEqual(result["status"], "backup complete")
            self.assertEqual(session.events, [])
    def test_plain_cli_has_no_callback_or_extra_hardware_probe(self):
        with patch.object(dfu, "_backup_var_without_event", return_value={"status": "backup complete"}), patch.object(dfu, "calibration_identity", side_effect=AssertionError("unattended ID probe")):
            self.assertEqual(dfu.backup_var()["status"], "backup complete")
    def test_remote_input_resolver_and_no_identity_no_lookup(self):
        import jibo_calibration_flow as flow
        import jibo_calibration_restore as restore
        from types import SimpleNamespace
        backup = {"image": "synthetic.img", "manifest": "synthetic.json", "sha256": "a" * 64}
        download = SimpleNamespace(files={"synthetic": b"data"}, manifest={"bundle_sha256": "b" * 64})
        class RemoteSession:
            def __init__(self): self.lookups = 0
            def lookup_for_fresh_setup(self, identity):
                self.lookups += 1
                return download
        session = RemoteSession()
        with patch.object(dfu, "_select_factory_calibration_backup", side_effect=dfu.DfuError("missing cameras")), patch.object(dfu, "_verify_var_backup_for_robot"), patch.object(restore, "local_identity"), patch.object(restore, "build_fresh_var", return_value={"image": "candidate.img"}), patch.object(dfu, "calibration_identity", return_value={"cid_serial": CID, "usb_port": "1-1"}), operation(session):
            source, candidate, remote = dfu._resolve_factory_fresh_var("tag", backup, "stock.img", "new.img", "1-1", "dfu-util")
            self.assertEqual(source, backup)
            self.assertIs(remote, download)
            self.assertEqual(candidate["image"], "candidate.img")
            self.assertEqual(session.lookups, 1)
            with patch.object(restore, "local_identity", side_effect=Exception("no identity")), self.assertRaises(Exception):
                dfu._resolve_factory_fresh_var("tag", backup, "stock.img", "new.img", "1-1", "dfu-util")
            self.assertEqual(session.lookups, 1)

    def test_cid_requires_exact_unanimous_loader_serials(self):
        for serials in ((CID, CID), (CID, "1" * 32), (CID.upper(),), ("0" * 32,), (" " + CID,), ("UNKNOWN",)):
            output = "\n".join('serial="' + s + '"' for s in serials)
            with self.subTest(serials=serials), patch.object(dfu, "tool", return_value="dfu-util"), patch.object(dfu, "_dfu_context", return_value=("1-1", (), "tag", output)):
                if serials == (CID, CID):
                    self.assertEqual(dfu.calibration_identity("1-1"), {"cid_serial": CID, "usb_port": "1-1"})
                else:
                    with self.assertRaises(dfu.DfuError):
                        dfu.calibration_identity("1-1")

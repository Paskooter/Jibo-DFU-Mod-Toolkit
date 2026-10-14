"""Flow integration controls: no network before affirmative consent."""
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
from gui_fakes import FakeApi
import jibo_gui_core as gui_core
import jibo_tui

class DummyClient:
    def __init__(self):
        self.core = core
        self.requests = []
    def submit(self, key, files):
        self.requests.append((key, files))
        return SimpleNamespace(message="Submitted for review", receipt="private receipt", detail="")
    def lookup(self, key):
        self.requests.append(("lookup", key))
        return SimpleNamespace(files=dict(core.FILES), manifest={"bundle_sha256": core.VECTOR["candidate_sha256"], "revision": 1, "has_conflicts": False, "assurance": "community-reviewed"})

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.flow = importlib.import_module("jibo_calibration_flow")
        sharing = patch("jibo_calibration_client.CALIBRATION_SHARING_AVAILABLE", True)
        sharing.start()
        self.addCleanup(sharing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = DummyClient()
        self.api = FakeApi()
        self.api.BACKUP_ROOT = Path(self.temp.name)
        self.api.calibration_client_factory = lambda: self.client
        self.api.calibration_identity = lambda port=None: {"cid_serial": core.VECTOR["cid_serial"], "usb_port": "1-1"}
        self.backup = {"status": "backup complete", "image": "synthetic.img", "sha256": "f" * 64}
    def test_decline_cancel_remembered_and_default_share_no_requests(self):
        for answer in (None, "local", "share"):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                self.api.BACKUP_ROOT = Path(directory)
                prompts = []
                def select(title, options, **kwargs):
                    prompts.append((title, options, kwargs)); return answer
                session = self.flow.SharingFlow(self.api, select)
                result = session.on_verified_var_backup_ready(self.backup, self.api.calibration_identity())
                self.assertEqual(prompts[0][0], self.flow.CONSENT_TITLE)
                self.assertEqual(prompts[0][2]["initial"], "share")
                self.assertIn(self.flow.CONSENT_BODY, prompts[0][2]["detail"])
                self.assertNotIn("touchscreen calibration", prompts[0][2]["detail"])
                if answer != "share":
                    self.assertEqual(self.client.requests, [])
                    self.assertEqual(result, "Not shared")
                    session = self.flow.SharingFlow(self.api, lambda *a, **k: self.fail("remembered decline prompted"))
                    self.assertEqual(session.on_verified_var_backup_ready(self.backup, self.api.calibration_identity()), "Not shared")
                else:
                    self.assertEqual(result, "Submitted for review")
                    self.assertEqual(len(self.client.requests), 1)
    def test_decline_against_real_loopback_client_makes_zero_http_requests(self):
        from test_calibration_client import Server, Ed25519PrivateKey
        import os
        import jibo_calibration_client as wire
        if Ed25519PrivateKey is None:
            self.skipTest("optional cryptography unavailable")
        with Server() as server, patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)):
            self.api.calibration_client_factory = lambda: wire.CalibrationClient(self.api.BACKUP_ROOT / "calibration", core=core)
            for answer in (None, "local"):
                with tempfile.TemporaryDirectory() as directory:
                    self.api.BACKUP_ROOT = Path(directory)
                    def select(*args, **kwargs):
                        self.assertEqual(server.requests, [])
                        return answer
                    session = self.flow.SharingFlow(self.api, select)
                    self.assertEqual(session.on_verified_var_backup_ready(self.backup, self.api.calibration_identity()), "Not shared")
                    self.assertEqual(server.requests, [])

    def test_one_callback_per_operation_failure_is_nonblocking(self):
        count = []
        session = self.flow.SharingFlow(self.api, lambda *a, **k: count.append(1) or "share")
        with self.flow.operation(session):
            self.flow.on_verified_var_backup_ready(self.backup, self.api.calibration_identity())
            self.flow.on_verified_var_backup_ready(self.backup, self.api.calibration_identity())
        self.assertEqual(count, [1])
        self.assertEqual(len(self.client.requests), 1)
        session = self.flow.SharingFlow(self.api, lambda *a, **k: "share")
        session.save_preference(core.VECTOR["robot_key"], choice="ask")
        with patch.object(self.client, "submit", side_effect=RuntimeError("private server body")):
            self.assertEqual(session.on_verified_var_backup_ready(self.backup, self.api.calibration_identity()), "Submission outcome unknown; your local backup is safe")
    def test_unverified_event_is_refused_without_prompt(self):
        session = self.flow.SharingFlow(self.api, lambda *a, **k: self.fail("unsafe prompt"))
        for status in ("failed", "prepared", "dry run", "unknown"):
            self.assertEqual(session.on_verified_var_backup_ready(dict(self.backup, status=status), self.api.calibration_identity()), "Not shared")
        self.assertEqual(self.client.requests, [])
    def test_lookup_requires_both_lookup_and_use_confirmation_and_stable_robot(self):
        for answers, expected in (((None,), False), (("lookup", None), False), (("lookup", "use"), True)):
            it = iter(answers)
            session = self.flow.SharingFlow(self.api, lambda *a, **k: next(it, None))
            result = session.lookup_for_fresh_setup(self.api.calibration_identity())
            self.assertEqual(result is not None, expected)
        it = iter(("lookup", "use"))
        session = self.flow.SharingFlow(self.api, lambda *a, **k: next(it, None))
        self.api.calibration_identity = lambda port=None: {"cid_serial": "1" * 32, "usb_port": "1-1"}
        with self.assertRaises(Exception):
            session.lookup_for_fresh_setup({"cid_serial": core.VECTOR["cid_serial"], "usb_port": "1-1"})
    def test_gui_private_shared_flow_consent_prompt(self):
        prompts = []
        def ask(p):
            prompts.append(p); return "local"
        def backup_var(**kwargs):
            self.flow.on_verified_var_backup_ready(self.backup, self.api.calibration_identity())
            return self.backup
        self.api.backup_var = backup_var
        c = gui_core.GuiController(self.api, prompter=ask, runner=gui_core.sync_runner)
        c.request_refresh(); self.assertTrue(c.start_action("backup-var"))
        self.assertEqual(prompts[0].kind, "calibration-consent")
        self.assertEqual(prompts[0].initial, "share")
        self.assertEqual(self.client.requests, [])
        result = [e[1] for e in c.drain() if e[0] == "finished"][-1]
        self.assertIn("Calibration sharing: Not shared", result.lines)

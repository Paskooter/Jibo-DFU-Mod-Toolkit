"""Run the real full-flash control path with synthetic files and fake USB only."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
import jibo_dfu as dfu
import jibo_updates as updates
from test_updates import compact_set

class RemoteFlashTests(unittest.TestCase):
    def setUp(self):
        self.sharing = patch("jibo_calibration_client.CALIBRATION_SHARING_AVAILABLE", True)
        self.sharing.start()
        self.addCleanup(self.sharing.stop)

    def test_remote_bytes_need_resolved_review_and_force_readback(self):
        for approve in (False, True):
            with self.subTest(approve=approve), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = root / "release/flash_jibo/output/images"; folder.mkdir(parents=True)
                for filename in updates.IMAGE_NAMES:
                    (folder / filename).write_bytes(b"synthetic package placeholder")
                stock = root / "stock.img"; stock.write_bytes(b"S" * 512)
                calibrated = root / "calibrated.img"; calibrated.write_bytes(b"C" * 512)
                operation = root / "operation"; operation.mkdir()
                capacities = dict(updates.KNOWN_CAPACITIES)
                capacities["skills"] = 10_991_139_328
                names = list(dfu.UPDATE_ORDER) + ["emmc-000"]
                backup = {"image": "synthetic-backup.img", "sha256": "a" * 64, "manifest": "synthetic.json"}
                candidate = {"image": str(calibrated), "sha256": dfu._sha256_file(calibrated), "calibration_files": list(dfu.images.FACTORY_CAMERA_PATHS)}
                remote = SimpleNamespace(manifest={"revision": 1, "robot_key": core.VECTOR["robot_key"], "bundle_sha256": core.VECTOR["candidate_sha256"]})
                reviews, writes, reads = [], [], []
                def confirm(plan):
                    reviews.append(plan)
                    return approve if "calibration_source" in plan else True
                def transfer(argv, **kwargs):
                    writes.append(argv[argv.index("-a") + 1])
                def readback(util, port, partition, size, destination, *args):
                    reads.append(partition)
                    return dfu._sha256_file(calibrated if partition == "var" else stock)
                with ExitStack() as stack:
                    for target, name, kwargs in (
                        (dfu, "devices", {"return_value": [{"port": "1-1", "state": "dfu"}]}),
                        (dfu, "_dfu_context", {"return_value": ("1-1", names, "synthetic-device", "")}),
                        (dfu, "_read_gpt_capacities", {"return_value": capacities}),
                        (dfu, "_new_operation_dir", {"return_value": operation}),
                        (dfu, "backup_var", {"return_value": backup}),
                        (dfu, "_resolve_factory_fresh_var", {"return_value": (backup, candidate, remote)}),
                        (dfu, "calibration_identity", {"return_value": {"cid_serial": core.VECTOR["cid_serial"], "usb_port": "1-1"}}),
                        (dfu.updates, "prepare_compact_images", {"return_value": compact_set({n: stock for n in dfu.UPDATE_ORDER})}),
                        (dfu, "run_with_progress", {"side_effect": transfer}),
                        (dfu, "_upload_partition", {"side_effect": readback}),
                        (dfu, "run", {"return_value": ""})):
                        stack.enter_context(patch.object(target, name, **kwargs))
                    stack.enter_context(patch("jibo_calibration_client.load_core", return_value=core))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    result = dfu.flash_update(root / "release", "keep-calibration", port="1-1", dfu_util="dfu-util", confirmation=confirm, verify_readback=False)
                self.assertEqual(len(reviews), 2)
                self.assertEqual(reviews[-1]["calibration_source"], "Community reviewed; not factory-authenticated")
                self.assertIn("mandatory", reviews[-1]["readback_policy"])
                if not approve:
                    self.assertEqual(result["status"], "cancelled")
                    self.assertEqual(writes, [])
                    self.assertEqual(reads, [])
                else:
                    self.assertEqual(writes, list(dfu.UPDATE_ORDER))
                    self.assertEqual(reads, writes)
                    saved = json.loads(Path(result["manifest"]).read_text())
                    self.assertTrue(all(w["status"] == "verified" for w in saved["writes"]))

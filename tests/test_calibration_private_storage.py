"""Private storage and optional verified partition extraction handoff."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
import jibo_calibration_client as client
import jibo_calibration_flow as flow
import jibo_dfu as dfu
from gui_fakes import FakeApi

class StorageTests(unittest.TestCase):
    def test_all_created_private_directories_chowned_to_invoking_user(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "calibration" / "receipts" / "synthetic.json"
            owned = []
            client._private_write(target, {"synthetic": "receipt"}, chown=lambda p: owned.append(Path(p)))
            self.assertIn(target.parent.parent, owned)
            self.assertIn(target.parent, owned)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.parent.stat().st_mode & 0o777, 0o700)
    def test_optional_partition_images_require_matching_identity_size_and_hash(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(dfu, "BACKUP_ROOT", Path(directory)):
            folder = Path(directory) / "partition-backup-synthetic"; folder.mkdir()
            image = folder / "partition.img"; image.write_bytes(b"synthetic partition bytes")
            cid = core.VECTOR["cid_serial"]
            record = {"kind": "jibo-partition-backup", "partition": "skills", "device_tag": "serial-sha256:" + hashlib.sha256(cid.encode()).hexdigest(), "image": image.name, "size_bytes": image.stat().st_size, "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
            manifest = folder / "backup-manifest.json"; manifest.write_text(json.dumps(record))
            identity = {"cid_serial": cid, "usb_port": "1-1"}
            self.assertEqual(dfu.calibration_partition_images(identity), {"skills_image": str(image)})
            image.write_bytes(b"different partition bytes")
            self.assertEqual(dfu.calibration_partition_images(identity), {})
    def test_shared_extractor_receives_verified_optional_partition_images(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeApi(); api.BACKUP_ROOT = Path(directory)
            from test_calibration_flow import DummyClient
            api.calibration_client_factory = DummyClient
            api.calibration_partition_images = lambda identity: {"rootfs_image": "synthetic-rootfs.img", "skills_image": "synthetic-skills.img"}
            session = flow.SharingFlow(api, lambda *a, **k: "local")
            real_extract = core.extract
            with patch.object(core, "extract", wraps=real_extract) as extract:
                session.on_verified_var_backup_ready({"status": "backup complete", "image": "synthetic-var.img"}, {"cid_serial": core.VECTOR["cid_serial"], "usb_port": "1-1"})
            extract.assert_called_once_with("synthetic-var.img", rootfs_image="synthetic-rootfs.img", skills_image="synthetic-skills.img")

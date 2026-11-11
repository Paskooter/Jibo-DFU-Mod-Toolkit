"""Database-assisted fresh candidate: synthetic ext4 only, no device writes."""
import base64
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
import jibo_images as images

@unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "requires e2fsprogs")
class RemoteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.restore = importlib.import_module("jibo_calibration_restore")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stock = self.image("stock", GENERIC_CAMERA, True)
        self.identity = self.image("identity", {}, False)
    def image(self, name, files, caches):
        tree = self.root / (name + "-tree")
        (tree / "jibo/lps").mkdir(parents=True)
        (tree / "etc").mkdir()
        (tree / "jibo/identity.json").write_text(json.dumps({"name": "Example-Unit-Robot", "serial_number": "synthetic", "cpuid": "synthetic", "wifi_mac": "synthetic"}))
        (tree / "jibo/mode.json").write_text('{"mode":"normal"}')
        if caches:
            for suffix in ("left_eye_1", "right_eye_0", "other_variant"):
                (tree / "jibo/lps" / ("distortionPrecomputeFile_" + suffix)).write_bytes(b"synthetic cache")
        for spec in core.ITEMS:
            if spec.key in files and spec.partition == "var":
                path = tree / spec.image_path.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(files[spec.key])
        output = self.root / (name + ".img")
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", "-d", str(tree), str(output), "8192"], check=True, capture_output=True)
        return output
    def test_remote_exact_files_identity_local_modes_cache_removal_source_unchanged(self):
        before = images.sha256_file(self.identity)
        output = self.root / "fresh.img"
        result = self.restore.build_fresh_var(self.stock, self.identity, output, core.FILES, core=core)
        self.assertEqual(result["identity_restored"], True)
        for spec in core.ITEMS:
            if spec.key in core.FILES:
                self.assertEqual(images._extract(output, spec.image_path, self.root / "readback"), core.FILES[spec.key])
                mode = "0100600" if spec.key == "audio" else "0100644"
                self.assertEqual(images._file_metadata(output, spec.image_path), {"mode": mode, "uid": "0", "gid": "0"})
        self.assertNotIn("distortionPrecomputeFile_", images._run_debugfs(output, "ls -p /jibo/lps"))
        self.assertEqual(images.sha256_file(self.identity), before)
        images._check_ext4(output)
    def test_missing_or_unknown_identity_refuses_before_candidate_exists(self):
        for identity in (b"{}", b'{"name":"Example-Unit-Robot","serial_number":"s","cpuid":"c","wifi_mac":"w","owner":"synthetic"}'):
            images._replace_file(self.identity, "/jibo/identity.json", identity, self.root)
            output = self.root / "refused.img"
            with self.assertRaises(Exception):
                self.restore.build_fresh_var(self.stock, self.identity, output, core.FILES, core=core)
            self.assertFalse(output.exists())


# The remote service stays disabled. These directly exercise the offline
# adapter with invented files, not a server lookup or downloaded candidate.
import pytest
from calibration_support import GENERIC_CAMERA, IDENTITY, PATHS, journal_pending, make_image


@pytest.mark.skipif(not all(shutil.which(n) for n in ("mke2fs", "debugfs", "e2fsck")),
                    reason="requires e2fsprogs")
@pytest.mark.parametrize("key", ["left", "right", "inter_camera"])
@pytest.mark.parametrize("default_kind", ["package", "pinned"])
def test_offline_restore_refuses_any_generic_camera_before_candidate_exists(tmp_path, monkeypatch, key, default_kind):
    import jibo_calibration as calibration
    import jibo_calibration_client as client
    import jibo_calibration_restore as restore
    assert client.CALIBRATION_SHARING_AVAILABLE is False
    stock = make_image(tmp_path, "stock.img", {PATHS[k]: raw for k, raw in GENERIC_CAMERA.items()})
    identity = make_image(tmp_path, "identity.img", {images.FACTORY_IDENTITY_PATH: IDENTITY})
    camera = {k: core.FILES[k] for k in ("left", "right", "inter_camera")}
    if default_kind == "package":
        camera[key] = json.dumps(json.loads(GENERIC_CAMERA[key]), indent=2).encode("ascii")
    else:
        import hashlib
        monkeypatch.setitem(calibration._STOCK_HASHES, key, hashlib.sha256(camera[key]).hexdigest())
    for k, raw in camera.items():
        assert calibration.validate_item(k, raw) == raw
    saved = {p: images.sha256_file(p) for p in (stock, identity)}
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        restore.build_fresh_var(stock, identity, tmp_path / "refused.img", camera, core=calibration)
    assert not (tmp_path / "refused.img").exists()
    assert {p: images.sha256_file(p) for p in saved} == saved
    assert client.CALIBRATION_SHARING_AVAILABLE is False


@pytest.mark.skipif(not all(shutil.which(n) for n in ("mke2fs", "debugfs", "e2fsck", "dumpe2fs")),
                    reason="requires e2fsprogs")
def test_local_identity_reads_journal_only_data_on_disposable_workspace_copy(tmp_path, monkeypatch):
    import jibo_calibration_client as client
    import jibo_calibration_restore as restore
    assert client.CALIBRATION_SHARING_AVAILABLE is False
    before = make_image(tmp_path, "before.img", {})
    after = tmp_path / "after.img"
    shutil.copyfile(before, after)
    images._put_factory_file(after, images.FACTORY_IDENTITY_PATH, IDENTITY, tmp_path, 0o600)
    backup = journal_pending(tmp_path, before, after)
    assert images._extract(backup, images.FACTORY_IDENTITY_PATH, tmp_path / "raw", optional=True) is None
    stock = make_image(tmp_path, "stock.img", {PATHS[k]: v for k, v in GENERIC_CAMERA.items()})
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    destination = workspace / "fresh.img"
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    replayed = []
    replay = images._replay_journal_on_copy

    def observe(image):
        image = Path(image)
        assert image not in saved
        assert image.stat().st_mode & 0o777 == 0o600
        replayed.append(image)
        return replay(image)

    monkeypatch.setattr(images, "_replay_journal_on_copy", observe)
    assert restore.local_identity(backup) == (IDENTITY, "Test-Only-Robot-Name")
    result = restore.build_fresh_var(stock, backup, destination, core.FILES, core=core)
    assert images._extract(destination, images.FACTORY_IDENTITY_PATH, tmp_path / "read") == IDENTITY
    assert result["sha256"] == images.sha256_file(destination)
    assert replayed[0].parent.parent == backup.parent
    assert replayed[1].parent.parent == workspace
    assert len(replayed) == 3 and replayed[2] == destination
    assert all(not p.exists() for p in replayed[:2])
    assert sorted(p.name for p in workspace.iterdir()) == [destination.name]
    assert {path: images.sha256_file(path) for path in saved} == saved
    assert client.CALIBRATION_SHARING_AVAILABLE is False


@pytest.mark.skipif(not all(shutil.which(n) for n in ("mke2fs", "debugfs", "e2fsck", "dumpe2fs")),
                    reason="requires e2fsprogs")
def test_local_identity_refuses_post_journal_invalid_data_without_changing_inputs(tmp_path):
    import jibo_calibration_restore as restore
    from jibo_calibration_client import ClientError
    before = make_image(tmp_path, "before.img", {images.FACTORY_IDENTITY_PATH: IDENTITY})
    after = tmp_path / "after.img"
    shutil.copyfile(before, after)
    invalid = IDENTITY[:-1] + b',"unknown":"synthetic"}'
    images._put_factory_file(after, images.FACTORY_IDENTITY_PATH, invalid, tmp_path, 0o600)
    backup = journal_pending(tmp_path, before, after)
    assert images._extract(backup, images.FACTORY_IDENTITY_PATH, tmp_path / "raw") == IDENTITY
    stock = make_image(tmp_path, "stock.img", {PATHS[k]: v for k, v in GENERIC_CAMERA.items()})
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    with pytest.raises(ClientError, match="outside the audited profile"):
        restore.build_fresh_var(stock, backup, workspace / "refused.img", core.FILES, core=core)
    assert not list(workspace.iterdir())
    assert {path: images.sha256_file(path) for path in saved} == saved

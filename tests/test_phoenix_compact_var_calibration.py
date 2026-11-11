"""Synthetic compact/journal calibration controls; no USB, mounts or robots."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

import pytest

import jibo_calibration as calibration
import jibo_images as images
from calibration_support import (
    FACTORY_RECORD, FILES, GENERIC_CAMERA, IDENTITY, PATHS, POINTERCAL,
    journal_pending, make_image,
)

CAMERAS = ("left", "right", "inter_camera")
VAR_KEYS = (*CAMERAS, "imu", "audio")
MARKER = "/etc/first_boot_resize.done"
pytestmark = pytest.mark.skipif(
    not all(shutil.which(name) for name in ("mke2fs", "debugfs", "dumpe2fs", "e2fsck")),
    reason="e2fsprogs required")


def _entries(files):
    return {PATHS[key]: raw for key, raw in files.items()}


def _pending_var(root):
    before = make_image(root, "before.img", _entries(GENERIC_CAMERA))
    after = root / "after.img"
    shutil.copyfile(before, after)
    metadata = {}
    for index, (path, raw) in enumerate({**_entries(FILES), images.FACTORY_IDENTITY_PATH: IDENTITY}.items()):
        metadata[path] = {"mode": "0100640", "uid": str(100 + index), "gid": "10"}
        if path in (PATHS["audio"], images.FACTORY_IDENTITY_PATH):
            metadata[path]["mode"] = "0100600"
        spec = next((s for s in calibration.ITEMS if s.image_path == path), None)
        if spec is not None:
            images._put_calibration_item(after, spec, raw, metadata[path], root)
        else:
            images._put_factory_file(after, path, raw, root, 0o600, metadata[path])
    images._put_factory_file(after, MARKER, b"old completed boot\n", root, 0o644)
    images._put_factory_file(after, "/jibo/credentials.json", b'{"region":"api"}', root, 0o600)
    images._check_ext4(after)
    return journal_pending(root, before, after), metadata


CONDITIONAL = [("rootfs", "touchscreen", POINTERCAL), ("skills", "factory_record", FACTORY_RECORD)]


def _pending_partition(root, key, content, previous=None):
    before = make_image(root, "before.img", {} if previous is None else {PATHS[key]: previous})
    after = root / "after.img"
    shutil.copyfile(before, after)
    metadata = {"mode": "0100640", "uid": "123", "gid": "10"}
    spec = next(s for s in calibration.ITEMS if s.key == key)
    images._put_calibration_item(after, spec, content, metadata, root)
    images._check_ext4(after)
    return journal_pending(root, before, after), metadata


@pytest.mark.parametrize("partition,key,content", CONDITIONAL)
def test_conditional_partition_reads_journal_only_bytes_and_metadata(tmp_path, monkeypatch, partition, key, content):
    backup, metadata = _pending_partition(tmp_path, key, content)
    assert images._extract(backup, PATHS[key], tmp_path / "raw", optional=True) is None
    stock = make_image(tmp_path, "stock.img", {})
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    replayed = []
    replay = images._replay_journal_on_copy

    def observe(image):
        image = Path(image)
        assert image not in saved and image.parent.parent == workspace
        assert image.stat().st_mode & 0o777 == 0o600
        replayed.append(image)
        return replay(image)

    monkeypatch.setattr(images, "_replay_journal_on_copy", observe)
    destination = workspace / "fresh.img"
    built = images.build_calibrated_partition(stock, backup, destination, partition)
    assert built["calibration_status"] == {key: "restored"}
    assert built["calibration_files"] == [PATHS[key]]
    assert images._extract(destination, PATHS[key], tmp_path / "read") == content
    assert images._file_metadata(destination, PATHS[key]) == metadata
    assert built["sha256"] == images.sha256_file(destination)
    assert destination.stat().st_size == stock.stat().st_size
    assert len(replayed) == 1 and not replayed[0].exists()
    assert sorted(p.name for p in workspace.iterdir()) == [destination.name]
    assert {path: images.sha256_file(path) for path in saved} == saved


@pytest.mark.parametrize("partition,key,content", CONDITIONAL)
def test_conditional_partition_journal_replay_refusal_leaves_inputs_unchanged(tmp_path, partition, key, content):
    backup, _ = _pending_partition(tmp_path, key, content)
    stock = make_image(tmp_path, "stock.img", {})
    images._run_debugfs(stock, "symlink " + PATHS[key] + " /missing", writable=True)
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    with pytest.raises(images.ImageError, match="not a regular file"):
        images.build_calibrated_partition(stock, backup, workspace / "refused.img", partition)
    assert not list(workspace.iterdir())
    assert {path: images.sha256_file(path) for path in saved} == saved


@pytest.mark.parametrize("partition,key,content", CONDITIONAL)
def test_conditional_partition_uses_post_journal_unsupported_status(tmp_path, partition, key, content):
    backup, _ = _pending_partition(tmp_path, key, b"unsupported synthetic format", previous=content)
    assert images._extract(backup, PATHS[key], tmp_path / "raw") == content
    stock = make_image(tmp_path, "stock.img", {})
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    destination = tmp_path / "unused.img"
    built = images.build_calibrated_partition(stock, backup, destination, partition)
    assert built["calibration_status"] == {key: "kept_local_unsupported"}
    assert built["calibration_files"] == [] and Path(built["image"]) == stock
    assert not destination.exists()
    assert {path: images.sha256_file(path) for path in saved} == saved


def test_keep_calibration_reads_journal_only_var_bytes_and_metadata(tmp_path, monkeypatch):
    backup, metadata = _pending_var(tmp_path)
    # This is not a clean-image proxy: raw debugfs sees old defaults and no identity/IMU/audio.
    assert images._extract(backup, PATHS["left"], tmp_path / "raw") == GENERIC_CAMERA["left"]
    for path in (images.FACTORY_IDENTITY_PATH, PATHS["imu"], PATHS["audio"]):
        assert images._extract(backup, path, tmp_path / "raw", optional=True) is None
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    output_dir = tmp_path / "accounted-workspace"
    output_dir.mkdir()
    destination = output_dir / "fresh.img"
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    replayed = []
    replay = images._replay_journal_on_copy

    def observe_replay(image):
        image = Path(image)
        assert image not in saved
        assert image.stat().st_mode & 0o777 == 0o600
        replayed.append(image)
        return replay(image)

    monkeypatch.setattr(images, "_replay_journal_on_copy", observe_replay)
    assert images.factory_calibration_available(backup, stock)
    built = images.build_factory_fresh_var(stock, backup, destination)
    assert built["camera_matches_stock"] is False
    assert built["sha256"] == images.sha256_file(destination)
    assert destination.stat().st_size == stock.stat().st_size
    for path, raw in {**_entries(FILES), images.FACTORY_IDENTITY_PATH: IDENTITY}.items():
        assert images._extract(destination, path, tmp_path / "read") == raw
        assert images._file_metadata(destination, path) == metadata[path]
    assert {key: built["calibration_status"][key] for key in VAR_KEYS} == dict.fromkeys(VAR_KEYS, "restored")
    for path in (MARKER, "/jibo/credentials.json"):
        assert images._extract(destination, path, tmp_path / "absent", optional=True) is None
    assert images._extract(destination, "/jibo/mode.json", tmp_path / "mode") == b'{"mode":"oobe"}'
    assert len(replayed) == 2
    assert replayed[0].parent.parent == stock.parent  # Default eligibility workspace.
    assert replayed[1].parent.parent == destination.parent
    assert all(not path.exists() for path in replayed)
    assert sorted(p.name for p in output_dir.iterdir()) == [destination.name]
    assert {path: images.sha256_file(path) for path in saved} == saved
    assert "needs_recovery" in images._run_debugfs(backup, "features").split()


@pytest.mark.parametrize("key", [*CAMERAS, "identity"])
def test_keep_calibration_refuses_silent_source_metadata_write_failure(tmp_path, monkeypatch, key):
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    path = images.FACTORY_IDENTITY_PATH if key == "identity" else PATHS[key]
    metadata = {path: {"mode": "0100600", "uid": "123", "gid": "10"}}
    backup = make_image(tmp_path, "backup.img", {
        **_entries({k: FILES[k] for k in CAMERAS}), images.FACTORY_IDENTITY_PATH: IDENTITY}, metadata)
    saved = {p: images.sha256_file(p) for p in (stock, backup)}
    real_debugfs = images._run_debugfs

    def drop_metadata_write(image, command, **kwargs):
        if command.startswith("set_inode_field " + path + " "):
            return ""  # Successful tool exit is not metadata readback evidence.
        return real_debugfs(image, command, **kwargs)

    monkeypatch.setattr(images, "_run_debugfs", drop_metadata_write)
    with pytest.raises(images.ImageError, match="permissions did not match"):
        images.build_factory_fresh_var(stock, backup, tmp_path / "refused.img")
    assert not (tmp_path / "refused.img").exists()
    assert {p: images.sha256_file(p) for p in saved} == saved


@pytest.mark.parametrize("generic_key", [*CAMERAS, "all"])
def test_keep_calibration_refuses_each_parsed_package_default(tmp_path, generic_key):
    camera = {key: FILES[key] for key in CAMERAS}
    for key in CAMERAS if generic_key == "all" else (generic_key,):
        # Parsed equality, not raw bytes: both whitespace and key order differ.
        value = dict(reversed(list(json.loads(GENERIC_CAMERA[key]).items())))
        camera[key] = json.dumps(value, indent=2).encode("ascii")
        assert camera[key] != GENERIC_CAMERA[key]
    for key in CAMERAS:
        assert calibration.validate_item(key, camera[key]) == camera[key]
        assert calibration.validate_item(key, GENERIC_CAMERA[key]) == GENERIC_CAMERA[key]
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    backup = make_image(tmp_path, "backup.img", {**_entries(camera), images.FACTORY_IDENTITY_PATH: IDENTITY})
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    assert not images.factory_calibration_available(backup, stock)
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        images.build_factory_fresh_var(stock, backup, tmp_path / "refused.img")
    assert not (tmp_path / "refused.img").exists()
    assert {path: images.sha256_file(path) for path in saved} == saved


@pytest.mark.parametrize("pinned_key", CAMERAS)
def test_keep_calibration_refuses_pinned_stock_even_when_package_differs(tmp_path, monkeypatch, pinned_key):
    # Synthetic pin only: exercise the real stock_equivalent hash rule, never
    # pretend these invented measurements came from a factory/robot.
    assert not any(calibration.stock_equivalent(FILES).values())
    monkeypatch.setitem(calibration._STOCK_HASHES, pinned_key, hashlib.sha256(FILES[pinned_key]).hexdigest())
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    backup = make_image(tmp_path, "backup.img", {
        **_entries({key: FILES[key] for key in CAMERAS}), images.FACTORY_IDENTITY_PATH: IDENTITY})
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    assert calibration.stock_equivalent(FILES)[pinned_key]
    assert not images.factory_calibration_available(backup, stock)
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        images.build_factory_fresh_var(stock, backup, tmp_path / "refused.img")
    assert not (tmp_path / "refused.img").exists()
    assert {path: images.sha256_file(path) for path in saved} == saved


@pytest.mark.parametrize("key", ["left", "right"])
def test_keep_calibration_refuses_generic_measurements_despite_supported_version_change(tmp_path, key):
    camera = {k: FILES[k] for k in CAMERAS}
    value = json.loads(GENERIC_CAMERA[key])
    value["version"] = "1.0.0" if value["version"] == "0.1" else "0.1"
    camera[key] = json.dumps(value).encode("ascii")
    # Both versions are supported; the changed string is not a new measurement.
    assert calibration.validate_item(key, camera[key]) == camera[key]
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    backup = make_image(tmp_path, "backup.img", {**_entries(camera), images.FACTORY_IDENTITY_PATH: IDENTITY})
    saved = {p: images.sha256_file(p) for p in (stock, backup)}
    assert not images.factory_calibration_available(backup, stock)
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        images.build_factory_fresh_var(stock, backup, tmp_path / "refused.img")
    assert not (tmp_path / "refused.img").exists()
    assert {p: images.sha256_file(p) for p in saved} == saved


@pytest.mark.parametrize("generic_key", CAMERAS)
def test_keep_calibration_refuses_package_measurements_with_extra_fields(tmp_path, generic_key):
    camera = {key: FILES[key] for key in CAMERAS}
    value = json.loads(GENERIC_CAMERA[generic_key])
    value["synthetic_extra_field"] = "not a different camera measurement"
    camera[generic_key] = json.dumps(value).encode("ascii")
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    backup = make_image(tmp_path, "backup.img", {**_entries(camera), images.FACTORY_IDENTITY_PATH: IDENTITY})
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    assert not images.factory_calibration_available(backup, stock)
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        images.build_factory_fresh_var(stock, backup, tmp_path / "refused.img")
    assert not (tmp_path / "refused.img").exists()
    assert {path: images.sha256_file(path) for path in saved} == saved


def test_keep_calibration_refuses_defaults_committed_only_in_journal(tmp_path):
    before = make_image(tmp_path, "before.img", {
        **_entries({key: FILES[key] for key in CAMERAS}), images.FACTORY_IDENTITY_PATH: IDENTITY})
    after = tmp_path / "after.img"
    shutil.copyfile(before, after)
    images._put_factory_file(after, PATHS["right"], GENERIC_CAMERA["right"], tmp_path, 0o644)
    backup = journal_pending(tmp_path, before, after)
    assert images._extract(backup, PATHS["right"], tmp_path / "raw") == FILES["right"]
    stock = make_image(tmp_path, "stock.img", _entries(GENERIC_CAMERA))
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    assert not images.factory_calibration_available(backup, stock, workdir=workspace)
    with pytest.raises(images.ImageError, match="generic camera calibration"):
        images.build_factory_fresh_var(stock, backup, workspace / "refused.img")
    assert not list(workspace.iterdir())
    assert {path: images.sha256_file(path) for path in saved} == saved


def test_settled_image_clean_input_is_read_only_without_a_replay_copy(tmp_path, monkeypatch):
    image = make_image(tmp_path, "clean.img", {})
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    saved = images.sha256_file(image)

    def unwanted_replay(_image):
        raise AssertionError("Clean images do not require a replay copy")

    monkeypatch.setattr(images, "_replay_journal_on_copy", unwanted_replay)
    with images._settled_image(image, workspace) as settled:
        assert settled == image
    assert not list(workspace.iterdir()) and images.sha256_file(image) == saved


@pytest.mark.parametrize("failure", ["replay", "consumer"])
def test_settled_image_cleans_disposable_copy_after_failure(tmp_path, monkeypatch, failure):
    backup, _ = _pending_partition(tmp_path, "touchscreen", POINTERCAL)
    saved = images.sha256_file(backup)
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    replay = images._replay_journal_on_copy
    replayed = []

    def fail_after_real_replay(image):
        image = Path(image)
        assert image.parent.parent == workspace and image != backup
        assert image.stat().st_mode & 0o777 == 0o600
        replayed.append(image)
        replay(image)
        if failure == "replay":
            raise images.ImageError("Synthetic replay failure")

    monkeypatch.setattr(images, "_replay_journal_on_copy", fail_after_real_replay)
    with pytest.raises(images.ImageError, match="Synthetic"):
        with images._settled_image(backup, workspace) as settled:
            assert images._extract(settled, PATHS["touchscreen"], tmp_path / "read") == POINTERCAL
            raise images.ImageError("Synthetic consumer failure")
    assert len(replayed) == 1 and not replayed[0].exists()
    assert not list(workspace.iterdir()) and images.sha256_file(backup) == saved


def test_settled_image_refuses_insufficient_replay_space_before_copy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    backup, _ = _pending_partition(tmp_path, "touchscreen", POINTERCAL)
    saved = images.sha256_file(backup)
    workspace = tmp_path / "accounted"
    workspace.mkdir()
    observed = []

    def usage(path):
        observed.append(Path(path))
        return SimpleNamespace(free=backup.stat().st_size - 1)

    def forbidden_copy(*_args, **_kwargs):
        raise AssertionError("A full replay copy must not start without room for it")

    monkeypatch.setattr(images.shutil, "disk_usage", usage)
    monkeypatch.setattr(images.shutil, "copyfile", forbidden_copy)
    with pytest.raises(images.ImageError, match="free space.*journal"):
        with images._settled_image(backup, workspace):
            raise AssertionError("Insufficient replay workspace was accepted")
    assert observed == [workspace]
    assert not list(workspace.iterdir()) and images.sha256_file(backup) == saved


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "fifo"])
def test_settled_image_refuses_nonregular_inputs_without_allocating(tmp_path, monkeypatch, kind):
    image = tmp_path / "input"
    if kind == "directory":
        image.mkdir()
    elif kind == "symlink":
        target = make_image(tmp_path, "target.img", {})
        image.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(image)
    workspace = tmp_path / "accounted"
    workspace.mkdir()

    def unwanted_read(*_args, **_kwargs):
        raise AssertionError("Nonregular inputs must be rejected before debugfs")

    monkeypatch.setattr(images, "_run_debugfs", unwanted_read)
    with pytest.raises(images.ImageError, match="regular partition images"):
        with images._settled_image(image, workspace):
            raise AssertionError("A nonregular input was yielded")
    assert not list(workspace.iterdir())


@pytest.mark.skipif(not shutil.which("resize2fs"), reason="resize2fs required")
def test_compact_phoenix_var_keeps_every_var_item_through_first_boot_resize(tmp_path):
    # Match the compact package's 2 MiB/1 KiB/128-inode geometry, not its bytes.
    tree = tmp_path / "stock-tree"
    for path, raw in {**_entries(GENERIC_CAMERA), MARKER: b"", "/etc/first_boot_resize": b"#!/bin/sh\n"}.items():
        target = tree / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    stock = tmp_path / "compact.ext4"
    subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024", "-N", "128",
                    "-O", "^metadata_csum,^64bit", "-d", str(tree), str(stock), "2048"],
                   check=True, capture_output=True)
    for directory in ("/jibo/imu", "/jibo/audio"):
        assert calibration._image_stat(stock, directory) is None
    backup, metadata = _pending_var(tmp_path)
    saved = {path: images.sha256_file(path) for path in (stock, backup)}
    candidate = tmp_path / "fresh.ext4"
    built = images.build_factory_fresh_var(stock, backup, candidate)
    assert candidate.stat().st_size == 2 * 1024 * 1024
    grown = tmp_path / "disposable-grown.ext4"
    shutil.copyfile(candidate, grown)
    with grown.open("r+b") as stream:
        stream.truncate(8 * 1024 * 1024)
    # The stock first-boot sequence; these are files, never block devices/mounts.
    for argv in (["e2fsck", "-fy", str(grown)], ["resize2fs", str(grown)], ["e2fsck", "-fn", str(grown)]):
        result = subprocess.run(argv, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    header = subprocess.run(["dumpe2fs", "-h", str(grown)], check=True,
                            capture_output=True, text=True).stdout
    assert int(header.split("Block count:")[1].split()[0]) == 8192
    assert calibration.extract(grown).files == FILES
    for path, raw in {**_entries(FILES), images.FACTORY_IDENTITY_PATH: IDENTITY}.items():
        assert images._extract(grown, path, tmp_path / "read") == raw
        assert images._file_metadata(grown, path) == metadata[path]
    assert images._extract(grown, MARKER, tmp_path / "absent", optional=True) is None
    assert images._extract(grown, "/jibo/credentials.json", tmp_path / "absent", optional=True) is None
    assert built["sha256"] == images.sha256_file(candidate)
    assert {path: images.sha256_file(path) for path in saved} == saved

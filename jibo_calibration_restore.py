"""Offline adapter for verified remote measurements plus independently local identity.

The network client never handles identity. This adapter never creates a donor backup.
Non-var remote items fail closed until their restore ownership/mode is pinned.
"""
import json
from pathlib import Path
import re
import shutil
import tempfile

import jibo_images as images
from jibo_calibration_client import ClientError, _json, load_core


def local_identity(image):
    with tempfile.TemporaryDirectory(prefix="jibo-local-identity-") as directory:
        path = images.FACTORY_IDENTITY_PATH
        images._file_metadata(image, path)  # Regular files only.
        raw = images._extract(image, path, Path(directory) / "identity")
        if not raw or len(raw) > 65536:
            raise ClientError("A complete verified local robot identity is required; calibration cannot reconstruct it.")
        value = _json(raw)
        if (not isinstance(value, dict) or set(value) != {"name", "serial_number", "cpuid", "wifi_mac"} or
                not isinstance(value["name"], str) or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+){2,3}", value["name"]) or
                any(not isinstance(value[k], str) or not value[k] for k in ("serial_number", "cpuid", "wifi_mac"))):
            raise ClientError("Local identity is missing or outside the audited profile; no partition write was attempted.")
        return raw, value["name"]


def _ensure_directory(image, path):
    output = images._run_debugfs(image, "stat " + path, allow_missing=True)
    if re.search(r"File not found|ext2_lookup", output, re.I):
        images._run_debugfs(image, "mkdir " + path, writable=True)
        output = images._run_debugfs(image, "stat " + path)
    if not re.search(r"Type:\s+directory\b", output):
        raise ClientError("Unsafe calibration destination directory.")


def build_fresh_var(stock, identity_image, destination, files, core=None):
    core = core or load_core()
    stock, identity_image, destination = Path(stock), Path(identity_image), Path(destination)
    if stock.is_symlink() or identity_image.is_symlink() or not stock.is_file() or not identity_image.is_file() or destination.exists() or destination.is_symlink():
        raise ClientError("Fresh calibration inputs must be regular files and output must be new.")
    specs = {s.key: s for s in core.ITEMS}
    if not {"left", "right", "inter_camera"}.issubset(files) or not set(files).issubset(specs):
        raise ClientError("Missing or unsupported remote calibration.")
    if any(specs[k].partition != "var" for k in files):
        raise ClientError("Downloaded rootfs/skills calibration requires an audited partition restore profile; no files were dropped and no partition write was attempted.")
    for key, data in files.items():
        if core.validate_item(key, data) != data:
            raise ClientError("Calibration validator changed original bytes.")
    identity, name = local_identity(identity_image)
    try:
        shutil.copyfile(stock, destination)
        destination.chmod(0o600)
        images._replay_journal_on_copy(destination)
        with tempfile.TemporaryDirectory(prefix="jibo-remote-calibration-") as temporary:
            for directory in ("/jibo", "/jibo/lps", "/etc"):
                _ensure_directory(destination, directory)
            for key, data in files.items():
                spec = specs[key]
                _ensure_directory(destination, str(Path(spec.image_path).parent))
                mode = 0o600 if key == "audio" else 0o644
                images._put_factory_file(destination, spec.image_path, data, temporary, mode)
                for field, value in (("mode", format(0o100000 | mode, "07o")), ("uid", "0"), ("gid", "0")):
                    images._run_debugfs(destination, "set_inode_field {} {} {}".format(spec.image_path, field, value), writable=True)
            for path, data, mode in ((images.FACTORY_IDENTITY_PATH, identity, 0o600), ("/jibo/mode.json", b'{"mode":"oobe"}', 0o600), ("/etc/hostname", (name + "\n").encode("ascii"), 0o644), ("/etc/hosts", ("127.0.0.1\tlocalhost\n127.0.1.1\t" + name + "\n").encode("ascii"), 0o644)):
                images._put_factory_file(destination, path, data, temporary, mode)
            listing = images._run_debugfs(destination, "ls -p /jibo/lps")
            for entry in listing.splitlines():
                fields = entry.split("/")
                if len(fields) > 5 and fields[5].startswith("distortionPrecomputeFile_"):
                    basename = fields[5]
                    if not re.fullmatch(r"distortionPrecomputeFile_[A-Za-z0-9_.-]+", basename):
                        raise ClientError("Unsupported cache pathname; fresh candidate refused.")
                    images._run_debugfs(destination, "rm /jibo/lps/" + basename, writable=True)
            if "distortionPrecomputeFile_" in images._run_debugfs(destination, "ls -p /jibo/lps"):
                raise ClientError("Stale distortion cache remains; candidate refused.")
            for key, data in files.items():
                if images._extract(destination, specs[key].image_path, Path(temporary) / "readback") != data:
                    raise ClientError("Calibration offline readback did not match.")
        images._rearm_first_boot_resize(destination)
        images._check_ext4(destination)
        if destination.stat().st_size != stock.stat().st_size:
            raise ClientError("Fresh calibration image changed length.")
        return {"image": str(destination), "sha256": images.sha256_file(destination), "calibration_files": [specs[k].image_path for k in files], "identity_restored": True, "mode": "oobe", "camera_matches_stock": all(core.stock_equivalent(files).get(k, False) for k in ("left", "right", "inter_camera"))}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

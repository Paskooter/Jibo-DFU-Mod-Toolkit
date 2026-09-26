import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
import zlib
from unittest.mock import patch

import jibo_updates as updates
import jibo_dfu as toolkit
REAL_RESIZE_PREFLIGHT = toolkit._preflight_resize_script_write


STOCK_13_RESIZE_SCRIPT = b'''#!/bin/sh

if [ -f /var/etc/first_boot_resize.done ]
then
    echo "First boot filesystem configuration already complete"
else
    echo "First boot filesystem configuration"
    # On-line resize the initial rootfs first
    /usr/sbin/resize2fs /dev/mmcblk0p1
    /bin/sync
    # Resize the secondary rootfs
    /usr/sbin/resize2fs /dev/mmcblk0p2
    # Resize the file systems the first time
    /bin/umount /var
    /usr/sbin/e2fsck -f /dev/mmcblk0p5
    /usr/sbin/resize2fs /dev/mmcblk0p5
    /bin/umount /usr/local
    /usr/sbin/e2fsck -f /dev/mmcblk0p4
    /usr/sbin/resize2fs /dev/mmcblk0p4
    /bin/umount /opt
    /usr/sbin/e2fsck -f /dev/mmcblk0p6
    /usr/sbin/resize2fs /dev/mmcblk0p6

    # Remount everything once we're done.
    /bin/mount -a
    touch /var/etc/first_boot_resize.done
fi
'''


def compact_set(images):
    return updates.CompactImageSet(images, STOCK_13_RESIZE_SCRIPT)


def make_gpt_prefix(skills_size=10_991_139_328):
    sector = 512
    names_and_sizes = (
        ("rootfsA", 1_048_576_000),
        ("rootfsB", 1_048_576_000),
        ("recovery", 52_428_800),
        ("services", 2_097_152_000),
        ("var", 524_288_000),
        ("skills", skills_size),
    )
    entries = bytearray(128 * 128)
    first_lba = 34
    for index, (name, size) in enumerate(names_and_sizes):
        count = size // sector
        entry = memoryview(entries)[index * 128:(index + 1) * 128]
        entry[:16] = bytes([index + 1]) * 16
        entry[16:32] = bytes([index + 20]) * 16
        struct.pack_into("<QQQ", entry, 32, first_lba, first_lba + count - 1, 0)
        entry[56:128] = name.encode("utf-16le").ljust(72, b"\x00")
        first_lba += count

    last_usable = first_lba - 1
    data = bytearray(32 * 1024)
    header = memoryview(data)[sector:2 * sector]
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 20, 0)
    struct.pack_into("<QQQQ", header, 24, 1, last_usable + 1, 34, last_usable)
    header[56:72] = b"D" * 16
    struct.pack_into("<QIII", header, 72, 2, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)
    data[2 * sector:2 * sector + len(entries)] = entries
    return bytes(data)


def add_image_files(archive, prefix="release/flash_jibo/output/images", skip=()):
    for name in updates.IMAGE_NAMES:
        if name in skip:
            continue
        payload = (name + " placeholder").encode("ascii")
        info = tarfile.TarInfo(prefix + "/" + name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


class UpdatePackageTests(unittest.TestCase):
    def setUp(self):
        self._resize_preflight = patch.object(
            toolkit, "_preflight_resize_script_write", return_value={"allocated_bytes": 1024})
        self._resize_write = patch.object(
            toolkit, "write_partition_file_live",
            return_value={"status": "verified", "operation_directory": "/tmp/file-transaction"})
        self._resize_preflight.start()
        self._resize_write.start()
        self.addCleanup(self._resize_write.stop)
        self.addCleanup(self._resize_preflight.stop)

    def _resume_fixture(self, root, first_candidate_hash=None):
        package_path = root / "jibo-13.0.0-update.tar.bz2"
        with tarfile.open(package_path, "w:bz2") as archive:
            add_image_files(archive)
        contents = {"rootfsA": b"A" * 512, "rootfsB": b"B" * 512,
                    "services": b"S" * 512, "skills": b"K" * 512}
        candidates = {}
        hashes = {}
        for name, payload in contents.items():
            candidates[name] = root / (name + ".prepared")
            candidates[name].write_bytes(payload)
            hashes[name] = hashlib.sha256(payload).hexdigest()
        var_image = root / "var.img"
        var_image.write_bytes(b"safe-var")
        var_hash = hashlib.sha256(b"safe-var").hexdigest()
        backup_manifest = root / "backup-manifest.json"
        backup_manifest.write_text(json.dumps({
            "schema": 1, "kind": "jibo-var-backup", "partition": "var",
            "size_bytes": 8, "sha256": var_hash, "image": var_image.name,
            "device_tag": "usb-identity-unavailable",
        }))
        capacities = {"rootfsA": 1024, "rootfsB": 1024, "services": 1024,
                      "skills": 1024, "var": 8}
        partitions = [
            {"name": name, "bytes": capacities[name]}
            for name in ("rootfsA", "rootfsB", "services", "skills")
        ]
        manifest = root / "old-update-manifest.json"
        first_hash = first_candidate_hash or hashes["rootfsA"]
        manifest.write_text(json.dumps({
            "schema": 1, "kind": "jibo-full-flash-update",
            "created_utc": "2026-09-25T00:00:00Z", "status": "failed",
            "package": str(package_path.resolve()), "version": package_path.name,
            "image_strategy": "compact-prefix-v1", "resize_tag": "a" * 32,
            "usb_port": "1-1", "var_policy": "preserve current configuration",
            "partitions": partitions,
            "backups": {"var": {"status": "backup complete",
                                  "image": str(var_image), "manifest": str(backup_manifest),
                                  "size_bytes": 8, "sha256": var_hash}},
            "writes": [{"partition": "rootfsA", "candidate_sha256": first_hash,
                        "size_bytes": 1024, "transfer_bytes": 512,
                        "status": "write started"}],
        }))
        package = updates.validate_package(package_path)
        names = ["rootfsA", "rootfsB", "services", "skills", "var", toolkit.MARKER]
        alt_output = '\n'.join('Found DFU: alt={}, name="{}"'.format(i, name)
                               for i, name in enumerate(names))
        operation = root / "resume-operation"
        operation.mkdir()
        return (package_path, package, candidates, hashes, var_hash, capacities,
                manifest, names, alt_output, operation)

    def _patch_resume_context(self, package, candidates, capacities, names,
                              alt_output, operation, var_hash, upload_side_effect=None):
        return [
            patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]),
            patch.object(toolkit.updates, "validate_package", return_value=package),
            patch.object(toolkit, "_dfu_context",
                         return_value=("1-1", names, "usb-identity-unavailable", alt_output)),
            patch.object(toolkit, "_read_gpt_capacities", return_value=capacities),
            patch.object(toolkit, "_new_operation_dir", return_value=operation),
            patch.object(toolkit.updates, "prepare_compact_images",
                         return_value=compact_set(candidates)),
            patch.object(toolkit, "_upload_var", return_value=var_hash),
            patch.object(toolkit, "_upload_partition", side_effect=upload_side_effect),
            patch.object(toolkit, "run_with_progress"),
            patch.object(toolkit, "run", return_value=""),
        ]

    def test_resume_checks_incomplete_record_then_skips_matching_partition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(root)
            uploads = []

            def upload(_tool, _port, name, _size, _destination):
                uploads.append(name)
                return hashes[name]

            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, upload)
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6] as live_var, patches[7] as readback, \
                        patches[8] as transfer, patches[9] as reset:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            self.assertEqual(result["status"], "transferred; reset requested")
            self.assertEqual(uploads, ["rootfsA"])
            live_var.assert_not_called()
            self.assertEqual([argv[argv.index("-a") + 1]
                              for args, _kwargs in transfer.call_args_list
                              for argv in [args[0]]
                              if "-D" in argv], ["rootfsB", "services", "skills"])
            reset.assert_called_once()
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(saved["resumed_from"], str(manifest.resolve()))
            self.assertEqual(saved["writes"][0]["status"], "verified")
            self.assertTrue(saved["writes"][0]["resumed_without_write"])
            self.assertTrue(saved["package_sha256"])

    def test_resume_rewrites_recorded_partition_when_full_readback_differs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(root)
            calls = {}

            def upload(_tool, _port, name, _size, _destination):
                calls[name] = calls.get(name, 0) + 1
                if name == "rootfsA" and calls[name] == 1:
                    return "f" * 64
                return hashes[name]

            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, upload)
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6], patches[7], patches[8] as transfer, patches[9]:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            writes = [args[0][args[0].index("-a") + 1]
                      for args, _kwargs in transfer.call_args_list if "-D" in args[0]]
            self.assertEqual(writes, ["rootfsA", "rootfsB", "services", "skills"])
            self.assertEqual(calls["rootfsA"], 1)
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertFalse(saved["writes"][0].get("resumed_without_write", False))

    def test_resume_trusts_recorded_completed_transfer_without_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(root)
            old = json.loads(manifest.read_text())
            old["writes"][0]["status"] = "transfer complete"
            manifest.write_text(json.dumps(old))
            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, upload_side_effect=AssertionError("unexpected readback"))
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6] as live_var, patches[7] as readback, \
                        patches[8] as transfer, patches[9]:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            live_var.assert_not_called()
            readback.assert_not_called()
            written = [argv[argv.index("-a") + 1]
                       for args, _kwargs in transfer.call_args_list
                       for argv in [args[0]] if "-D" in argv]
            self.assertEqual(written, ["rootfsB", "services", "skills"])
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertTrue(saved["writes"][0]["resumed_without_write"])

    def test_resume_reuses_saved_var_without_live_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(root)
            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                "0" * 64, lambda *_args: hashes["rootfsA"])
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6] as live_var, patches[7] as readback, \
                        patches[8] as transfer, patches[9] as reset:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            self.assertEqual(result["status"], "transferred; reset requested")
            live_var.assert_not_called()
            self.assertGreaterEqual(readback.call_count, 1)
            self.assertGreaterEqual(transfer.call_count, 1)
            reset.assert_called_once()

    def test_resume_rewrites_when_fresh_candidate_is_not_equivalent_to_old_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_hash = hashlib.sha256(b"older timestamped candidate").hexdigest()
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(
                 root, first_candidate_hash=old_hash)
            calls = {}

            def upload(_tool, _port, name, _size, _destination):
                calls[name] = calls.get(name, 0) + 1
                return old_hash if name == "rootfsA" and calls[name] == 1 else hashes[name]

            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, upload)
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6] as live_var, patches[7] as readback, \
                        patches[8] as transfer, patches[9] as reset:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            live_var.assert_not_called()
            self.assertEqual(calls["rootfsA"], 1)
            self.assertEqual([args[0][args[0].index("-a") + 1]
                              for args, _kwargs in transfer.call_args_list if "-D" in args[0]],
                             ["rootfsA", "rootfsB", "services", "skills"])
            reset.assert_called_once()
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(saved["writes"][0]["candidate_sha256"], hashes["rootfsA"])

    def test_legacy_full_image_resume_reflashes_compact_prefixes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_hash = hashlib.sha256(b"older timestamped candidate").hexdigest()
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(
                 root, first_candidate_hash=old_hash)
            legacy = json.loads(manifest.read_text())
            legacy.pop("image_strategy")
            legacy.pop("resize_tag")
            manifest.write_text(json.dumps(legacy))

            def upload(_tool, _port, name, _size, _destination):
                return old_hash if name == "rootfsA" else hashes[name]

            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, upload)
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6], patches[7], patches[8] as transfer, patches[9]:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = toolkit.flash_update(
                            package_path, True, "1-1", "dfu-util", None,
                            "FLASH UPDATE", False, manifest)
            self.assertEqual([args[0][args[0].index("-a") + 1]
                              for args, _kwargs in transfer.call_args_list if "-D" in args[0]],
                             ["rootfsA", "rootfsB", "services", "skills"])
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertTrue(saved["legacy_resume_reflashed_compact"])
            self.assertEqual(len(saved["legacy_full_image_attempts"]), 1)

    def test_resume_rejects_noncontiguous_or_malformed_prior_write_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (_, package, _, _, _, capacities,
             manifest, _, _, _) = self._resume_fixture(root)
            old = json.loads(manifest.read_text())
            old["writes"][0]["partition"] = "rootfsB"
            manifest.write_text(json.dumps(old))
            with self.assertRaisesRegex(toolkit.DfuError, "contiguous partition prefix"):
                toolkit._validate_resume_manifest(
                    manifest, package, True, capacities,
                    ["rootfsA", "rootfsB", "services", "skills"])

    def test_verify_update_write_reads_only_and_removes_temporary_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"S" * 512
            expected_hash = hashlib.sha256(payload).hexdigest()
            manifest = root / "update-manifest.json"
            manifest.write_text(json.dumps({
                "kind": "jibo-full-flash-update", "writes": [{
                    "partition": "services", "size_bytes": 1024,
                    "transfer_bytes": len(payload),
                    "candidate_sha256": expected_hash, "status": "write started"}]}))
            readback_paths = []

            def upload(_tool, _port, _name, _size, destination):
                readback_paths.append(Path(destination))
                Path(destination).write_bytes(payload)
                return hashlib.sha256(payload).hexdigest()

            with patch.object(toolkit, "_dfu_context",
                              return_value=("1-1", ["var", "services"], "device", "listing")), \
                    patch.object(toolkit, "_read_gpt_capacities",
                                 return_value={"services": 1024}), \
                    patch.object(toolkit, "_upload_partition", side_effect=upload) as read:
                result = toolkit.verify_update_write(manifest, "services", "1-1", "dfu-util")
            self.assertEqual(result["status"], "match")
            read.assert_called_once()
            self.assertFalse(readback_paths[0].exists())

    def test_verify_update_write_checks_live_partition_size_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "update-manifest.json"
            manifest.write_text(json.dumps({
                "kind": "jibo-full-flash-update", "writes": [{
                    "partition": "services", "size_bytes": 512,
                    "candidate_sha256": "a" * 64}]}))
            with patch.object(toolkit, "_dfu_context",
                              return_value=("1-1", ["var", "services"], "device", "listing")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value={"services": 1024}), \
                    patch.object(toolkit, "_upload_partition") as read:
                with self.assertRaisesRegex(toolkit.DfuError, "does not match"):
                    toolkit.verify_update_write(manifest, "services", "1-1", "dfu-util")
            read.assert_not_called()

    def test_verify_update_write_reads_only_compact_skills_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"K" * 1536
            manifest = root / "update-manifest.json"
            manifest.write_text(json.dumps({
                "kind": "jibo-full-flash-update", "image_strategy": "compact-prefix-v1",
                "writes": [{"partition": "skills", "size_bytes": 4096,
                            "transfer_bytes": len(payload),
                            "candidate_sha256": hashlib.sha256(payload).hexdigest()}]}))
            names = ["skills-000", "skills-001", "skills-002", "skills-003",
                     "var", toolkit.MARKER]
            listing = "\n".join('Found DFU: alt={}, name="{}"'.format(index, name)
                                for index, name in enumerate(names))
            chunks = {"skills-000": payload[:1024], "skills-001": payload[1024:]}
            reads = []

            def upload(_tool, _port, alternative, size, destination):
                reads.append((alternative, size))
                piece = chunks[alternative]
                self.assertEqual(len(piece), size)
                Path(destination).write_bytes(piece)
                return hashlib.sha256(piece).hexdigest()

            with patch.object(toolkit, "SKILLS_CHUNK_BYTES", 1024), \
                    patch.object(toolkit, "_dfu_context",
                                 return_value=("1-1", names, "device", listing)), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value={"skills": 4096}), \
                    patch.object(toolkit, "_upload_partition", side_effect=upload):
                result = toolkit.verify_update_write(manifest, "skills", "1-1", "dfu-util")
            self.assertEqual(result["status"], "match")
            self.assertEqual(result["size_bytes"], 1536)
            self.assertEqual(result["partition_capacity_bytes"], 4096)
            self.assertEqual(reads, [("skills-000", 1024), ("skills-001", 512)])

    def test_gpt_layout_parser_returns_exact_partition_extents(self):
        layout = updates.parse_gpt_layout_prefix(make_gpt_prefix())
        self.assertEqual(layout["rootfsA"]["first_lba"], 34)
        self.assertEqual(layout["rootfsA"]["size_bytes"], updates.KNOWN_CAPACITIES["rootfsA"])
        self.assertEqual(layout["var"]["last_lba"] - layout["var"]["first_lba"] + 1,
                         updates.KNOWN_CAPACITIES["var"] // 512)

    def test_gpt_layout_parser_rejects_corrupt_partition_table_crc(self):
        prefix = bytearray(make_gpt_prefix())
        prefix[1024 + 56] ^= 1
        with self.assertRaisesRegex(updates.UpdateError, "partition-entry array CRC"):
            updates.parse_gpt_layout_prefix(bytes(prefix))

    def test_preserve_var_flash_never_writes_var_or_reads_back_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            candidate = root / "prepared.img"
            candidate.write_bytes(b"P" * 512)
            operation = root / "operation"
            operation.mkdir()
            capacities = {name: size for name, size in updates.KNOWN_CAPACITIES.items()}
            capacities["skills"] = 10_991_139_328
            transfers = []
            download_sizes = {}

            def transfer(argv, timeout, label, download_size=None,
                         allow_progress_completion=False):
                self.assertTrue(allow_progress_completion)
                transfers.append(argv)
                download_sizes[argv[argv.index("-a") + 1]] = download_size

            names = ["rootfsA", "rootfsB", "services", "skills", "var", "emmc-000"]
            alt_output = '\n'.join('Found DFU: alt={}, name="{}"'.format(index, name)
                                   for index, name in enumerate(names))
            with patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context", return_value=("1-1", names, "device", alt_output)), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value={"image": "saved-var.img"}) as var_backup, \
                    patch.object(toolkit, "_backup_partition_once", return_value={"image": "saved.img"}) as backup, \
                    patch.object(toolkit, "_backup_skills_partition_once") as skills_backup, \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set({name: candidate for name in ("rootfsA", "rootfsB", "services", "skills")})), \
                    patch.object(toolkit, "_upload_partition", return_value=toolkit._sha256_file(candidate)) as upload, \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", True, port="1-1", dfu_util="dfu-util",
                                                  confirmation="FLASH UPDATE")
            written = [argv[argv.index("-a") + 1] for argv in transfers if "-D" in argv]
            self.assertEqual(written, ["rootfsA", "rootfsB", "services", "skills"])
            self.assertEqual(download_sizes, {name: 512 for name in written})
            var_backup.assert_called_once_with("1-1", "dfu-util")
            backup.assert_not_called()
            skills_backup.assert_not_called()
            upload.assert_not_called()
            self.assertEqual(result["status"], "transferred; reset requested")
            self.assertEqual(result["backups"], {"var": {"image": "saved-var.img"}})

    def test_full_readback_option_backs_up_only_var_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            candidate = root / "prepared.img"
            candidate.write_bytes(b"P" * 512)
            operation = root / "operation"
            operation.mkdir()
            capacities = dict(updates.KNOWN_CAPACITIES)
            capacities["skills"] = 10_991_139_328
            names = ["rootfsA", "rootfsB", "services", "skills", "var", "emmc-000"]
            transfers = []
            with patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context", return_value=("1-1", names, "device", "")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value={"image": "saved-var.img"}) as var_backup, \
                    patch.object(toolkit, "_backup_partition_once") as other_backup, \
                    patch.object(toolkit, "_backup_skills_partition_once") as skills_backup, \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set({name: candidate for name in toolkit.UPDATE_ORDER})), \
                    patch.object(toolkit, "_upload_partition", return_value=toolkit._sha256_file(candidate)) as upload, \
                    patch.object(toolkit, "run_with_progress", side_effect=lambda argv, **_kwargs: transfers.append(argv)), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", False, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE",
                                                  verify_readback=True)
            var_backup.assert_called_once_with("1-1", "dfu-util")
            other_backup.assert_not_called()
            skills_backup.assert_not_called()
            self.assertEqual(result["backups"], {"var": {"image": "saved-var.img"}})
            self.assertEqual(upload.call_count, 5)
            self.assertEqual(result["status"], "verified; reset requested")
            self.assertEqual([argv[argv.index("-a") + 1] for argv in transfers],
                             list(toolkit.UPDATE_ORDER))

    def test_chunked_skills_update_validates_and_transfers_exact_gpt_slices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            rootfs = root / "rootfs.prepared"
            services = root / "services.prepared"
            skills = root / "skills.prepared"
            rootfs.write_bytes(b"R" * 512)
            services.write_bytes(b"S" * 512)
            skills_payload = bytes(range(256)) * 10
            skills.write_bytes(skills_payload)
            operation = root / "operation"
            operation.mkdir()
            capacities = {"rootfsA": 1024, "rootfsB": 1024, "services": 1024,
                          "var": toolkit.EXPECTED_VAR_SIZE, "skills": 4096}
            chunk_names = ("skills-000", "skills-001", "skills-002", "skills-003")
            names = ["rootfsA", "rootfsB", "services", "var", "emmc-000", *chunk_names]
            # Standard dfu-util -l output identifies alternatives but has no
            # size field; chunk coverage is derived from the live GPT table.
            alt_output = '\n'.join(
                'Found DFU: alt={}, name="{}"'.format(index, name)
                for index, name in enumerate(names))
            prepared = {"rootfsA": rootfs, "rootfsB": rootfs,
                        "services": services, "skills": skills}
            written = {}
            download_sizes = {}

            def transfer(argv, timeout, label, download_size=None,
                         allow_progress_completion=False):
                self.assertTrue(allow_progress_completion)
                if "-D" in argv:
                    alternative = argv[argv.index("-a") + 1]
                    written[alternative] = Path(argv[argv.index("-D") + 1]).read_bytes()
                    download_sizes[alternative] = download_size

            def upload(_dfu_util, _port, alternative, size, destination):
                payload = written[alternative]
                if len(payload) != size:
                    raise AssertionError("{} transferred {} bytes; expected {}".format(
                        alternative, len(payload), size))
                Path(destination).write_bytes(payload)
                return hashlib.sha256(payload).hexdigest()

            with patch.object(toolkit, "SKILLS_CHUNK_BYTES", 1024), \
                    patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context",
                                 return_value=("1-1", names, "device", alt_output)), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value={"image": "saved-var.img"}) as var_backup, \
                    patch.object(toolkit, "_backup_partition_once", return_value={"image": "saved.img"}) as backup, \
                    patch.object(toolkit, "_backup_skills_partition_once",
                                 return_value={"image": "saved-skills.img"}) as skills_backup, \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set(prepared)), \
                    patch.object(toolkit, "_upload_partition", side_effect=upload), \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", True, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE")

            var_backup.assert_called_once_with("1-1", "dfu-util")
            skills_backup.assert_not_called()
            backup.assert_not_called()
            self.assertEqual([written[name] for name in chunk_names[:3]],
                             [skills_payload[:1024], skills_payload[1024:2048], skills_payload[2048:]])
            self.assertEqual([download_sizes[name] for name in chunk_names[:3]], [1024, 1024, 512])
            self.assertNotIn("skills-003", written)
            self.assertEqual(result["status"], "transferred; reset requested")
            manifest = json.loads((operation / "update-manifest.json").read_text())
            skills_write = next(item for item in manifest["writes"] if item["partition"] == "skills")
            self.assertEqual([item["alternative"] for item in skills_write["chunks"]], list(chunk_names[:3]))
            self.assertEqual(skills_write["transfer_bytes"], len(skills_payload))
            self.assertEqual(skills_write["size_bytes"], capacities["skills"])
            self.assertTrue(all(item["status"] == "transfer complete" for item in skills_write["chunks"]))

    def test_skills_chunk_map_must_match_gpt_capacity_and_reported_sizes(self):
        with patch.object(toolkit, "SKILLS_CHUNK_BYTES", 1024):
            chunks = toolkit._validate_skills_chunk_alternatives(
                2560, ["skills-000", "skills-001", "skills-002"],
                'Found DFU: name="skills-000"\n'
                'Found DFU: name="skills-001"\n'
                'Found DFU: name="skills-002"')
            self.assertEqual([item["size_bytes"] for item in chunks], [1024, 1024, 512])
            with self.assertRaisesRegex(toolkit.DfuError, "missing skills-001"):
                toolkit._validate_skills_chunk_alternatives(
                    2560, ["skills-000", "skills-002"],
                    'Found DFU: name="skills-000", size=1024\n'
                    'Found DFU: name="skills-002", size=512')
            with self.assertRaisesRegex(toolkit.DfuError, "GPT requires 1024 bytes"):
                toolkit._validate_skills_chunk_alternatives(
                    2560, ["skills-000", "skills-001", "skills-002"],
                    'Found DFU: name="skills-000", size=1024\n'
                    'Found DFU: name="skills-001", size=512\n'
                    'Found DFU: name="skills-002", size=512')

    def test_skills_backup_assembles_exact_original_partition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = bytes(range(256)) * 10
            pieces = {"skills-000": payload[:1024],
                      "skills-001": payload[1024:2048],
                      "skills-002": payload[2048:]}
            with patch.object(toolkit, "SKILLS_CHUNK_BYTES", 1024):
                chunks = toolkit._expected_skills_chunks(len(payload))

            def upload(_tool, _port, alternative, size, destination):
                data = pieces[alternative]
                self.assertEqual(len(data), size)
                Path(destination).write_bytes(data)
                return hashlib.sha256(data).hexdigest()

            destination = root / "original-skills.img"
            with patch.object(toolkit, "_upload_partition", side_effect=upload) as transfer:
                with redirect_stdout(io.StringIO()):
                    result = toolkit._upload_skills_partition(
                        "dfu-util", "1-1", len(payload), chunks,
                        destination=destination, workdir=root)
            self.assertEqual(transfer.call_count, 3)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual([entry["alternative"] for entry in result["chunks"]], list(pieces))

    def test_parse_dfu_capacities_only_accepts_explicit_byte_sizes(self):
        output = ('Found DFU: alt=1, name="rootfsA", size=1048576000\n'
                  'Found DFU: alt=2, name="skills"\n')
        self.assertEqual(updates.parse_alt_capacities(output), {"rootfsA": 1048576000})

    def test_parse_complete_gpt_prefix_derives_variable_skills_capacity(self):
        capacities = updates.parse_gpt_prefix(make_gpt_prefix())
        self.assertEqual(capacities["rootfsA"], 1_048_576_000)
        self.assertEqual(capacities["services"], 2_097_152_000)
        self.assertEqual(capacities["skills"], 10_991_139_328)

    def test_gpt_rejects_partial_entry_table_and_bad_crc(self):
        prefix = make_gpt_prefix()
        with self.assertRaisesRegex(updates.UpdateError, "complete partition-entry table"):
            updates.parse_gpt_prefix(prefix[:4096])
        damaged = bytearray(prefix)
        damaged[1024 + 56] ^= 1
        with self.assertRaisesRegex(updates.UpdateError, "partition-entry array CRC"):
            updates.parse_gpt_prefix(bytes(damaged))

    def test_archive_discovery_only_accepts_full_flash_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            archive_path = folder / "jibo-pvt-flash-build-5.4.2.tar.bz2"
            with tarfile.open(archive_path, "w:bz2") as archive:
                add_image_files(archive)
            found = updates.discover_packages(folder)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].version, "5.4.2")
            self.assertTrue(found[0].is_archive)
            self.assertIn("rootfs.ext4", found[0].archive_members)

    def test_archive_rejects_path_traversal_and_image_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "bad.tar.bz2"
            with tarfile.open(archive_path, "w:bz2") as archive:
                add_image_files(archive)
                traversal = tarfile.TarInfo("../../outside")
                traversal.size = 1
                archive.addfile(traversal, io.BytesIO(b"x"))
            with self.assertRaisesRegex(updates.UpdateError, "unsafe path"):
                updates.validate_package(archive_path)

        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "bad-link.tar.bz2"
            with tarfile.open(archive_path, "w:bz2") as archive:
                add_image_files(archive, skip=("rootfs.ext4",))
                link = tarfile.TarInfo("release/flash_jibo/output/images/rootfs.ext4")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                archive.addfile(link)
            with self.assertRaisesRegex(updates.UpdateError, "not a regular file"):
                updates.validate_package(archive_path)

    @unittest.skipUnless(all(shutil.which(name) for name in
                             ("mke2fs", "e2fsck", "dumpe2fs", "resize2fs", "debugfs")),
                         "e2fsprogs is required for the compact image rehearsal")
    def test_compact_prefixes_expand_cleanly_over_a_stale_skills_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                image = images_dir / filename
                with image.open("wb") as stream:
                    stream.truncate(4 * 1024 * 1024)
                subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024",
                                str(image)], check=True, capture_output=True)
            script_path = root / "first_boot_resize"
            script_path.write_bytes(STOCK_13_RESIZE_SCRIPT)
            inittab_path = root / "inittab"
            inittab_path.write_text("::sysinit:/var/etc/first_boot_resize\n")
            for image, internal_path, host_path in (
                    (images_dir / "var.ext4", "/etc/first_boot_resize", script_path),
                    (images_dir / "rootfs.ext4", "/etc/inittab", inittab_path)):
                subprocess.run(["debugfs", "-w", "-R", "mkdir /etc", str(image)],
                               check=True, capture_output=True)
                subprocess.run(["debugfs", "-w", "-R",
                                "write {} {}".format(host_path, internal_path), str(image)],
                               check=True, capture_output=True)

            package = updates.validate_package(root)
            skills_capacity = 8 * 1024 * 1024 + 512
            capacities = {"rootfsA": 8 * 1024 * 1024, "rootfsB": 8 * 1024 * 1024,
                          "services": 8 * 1024 * 1024, "skills": skills_capacity,
                          "var": 4 * 1024 * 1024}
            known = {name: capacities[name] for name in
                     ("rootfsA", "rootfsB", "services", "skills", "var")}
            with patch.dict(updates.KNOWN_CAPACITIES, known, clear=True):
                compact = updates.prepare_compact_images(package, True, capacities,
                                                         root / "compact")
            self.assertNotIn("var", compact.images)
            for partition, candidate in compact.images.items():
                self.assertEqual(candidate.stat().st_size, 4 * 1024 * 1024)
            self.assertEqual(compact.resize_script, STOCK_13_RESIZE_SCRIPT)

            target = root / "skills-with-stale-tail.img"
            with target.open("wb") as stream:
                remaining = skills_capacity
                block = b"\xa5" * (1024 * 1024)
                while remaining:
                    piece = block[:min(len(block), remaining)]
                    stream.write(piece)
                    remaining -= len(piece)
            with target.open("r+b") as stream, compact.images["skills"].open("rb") as source:
                shutil.copyfileobj(source, stream)
            check = subprocess.run(["e2fsck", "-f", "-p", str(target)],
                                   text=True, capture_output=True)
            self.assertIn(check.returncode, (0, 1), check.stdout + check.stderr)
            subprocess.run(["resize2fs", "-f", str(target)], check=True, capture_output=True)
            final_check = subprocess.run(["e2fsck", "-f", "-n", str(target)],
                                         text=True, capture_output=True)
            self.assertEqual(final_check.returncode, 0, final_check.stdout + final_check.stderr)
            self.assertEqual(updates._filesystem_geometry(target), (1024, skills_capacity // 1024))

    def test_preserved_var_resize_script_is_tagged_and_skips_var_fsck(self):
        tag = "0123456789abcdef0123456789abcdef"
        script = toolkit._rearm_preserved_var_resize_script(STOCK_13_RESIZE_SCRIPT, tag)
        self.assertIn(b'"$tag"', script)
        self.assertIn(b'printf \'%s\\n\' "$tag" > "$marker"', script)
        self.assertIn(b"resize2fs /dev/mmcblk0p1", script)
        self.assertIn(b"resize2fs /dev/mmcblk0p2", script)
        self.assertIn(b"resize2fs /dev/mmcblk0p4", script)
        self.assertIn(b"resize2fs /dev/mmcblk0p6", script)
        self.assertNotIn(b"mmcblk0p5", script)
        self.assertIn(b'e2fsck -f -p /dev/mmcblk0p4 || [ "$?" -eq 1 ]', script)
        script_path = Path(tempfile.gettempdir()) / "jibo-rearmed-resize-test.sh"
        try:
            script_path.write_bytes(script)
            subprocess.run(["sh", "-n", str(script_path)], check=True, capture_output=True)
        finally:
            script_path.unlink(missing_ok=True)
        unsafe = STOCK_13_RESIZE_SCRIPT.replace(
            b"/usr/sbin/resize2fs /dev/mmcblk0p2",
            b"/sbin/mkfs.ext4 /dev/mmcblk0p2")
        with self.assertRaisesRegex(toolkit.DfuError, "known safe 13.0 stock resize script"):
            toolkit._rearm_preserved_var_resize_script(unsafe, tag)

    def test_resize_script_preflight_rejects_insufficient_existing_allocation(self):
        current = (b"#!/bin/sh\n/var/etc/first_boot_resize.done\n"
                   b"/usr/sbin/resize2fs /dev/mmcblk0p1\n")
        replacement = toolkit._rearm_preserved_var_resize_script(
            STOCK_13_RESIZE_SCRIPT, "0123456789abcdef0123456789abcdef")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(toolkit, "_file_loader_context"), \
                patch.object(toolkit, "_stat_partition_file_rpc", return_value={
                    "size_bytes": len(current), "allocated_bytes": 512}), \
                patch.object(toolkit, "_read_partition_file_rpc", return_value=current):
            with self.assertRaisesRegex(toolkit.DfuError, "has only 512 bytes allocated"):
                REAL_RESIZE_PREFLIGHT("dfu-util", "1-1", replacement, temp)

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "e2fsck", "dumpe2fs", "resize2fs")),
                         "e2fsprogs is required for the offline resize check")
    def test_prepare_expands_ext4_offline_and_omits_var_when_preserving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            source_hashes = {}
            for filename in updates.IMAGE_NAMES:
                image = images_dir / filename
                with image.open("wb") as stream:
                    stream.truncate(4 * 1024 * 1024)
                subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024", str(image)],
                               check=True, capture_output=True)
                source_hashes[filename] = hashlib.sha256(image.read_bytes()).hexdigest()
            package = updates.validate_package(root)
            capacities = {name: 8 * 1024 * 1024 for name in
                          ("rootfsA", "rootfsB", "services", "var", "skills")}
            capacities["skills"] += 512  # GPT sector that cannot hold a whole ext4 block.
            small_known = {name: capacities[name] for name in
                           ("rootfsA", "rootfsB", "services", "var")}
            with patch.dict(updates.KNOWN_CAPACITIES, small_known, clear=True):
                prepared_fresh = updates.prepare_images(package, False, capacities, root / "fresh")
                for name in ("rootfsA", "rootfsB", "services", "skills", "var"):
                    self.assertEqual(prepared_fresh[name].stat().st_size, capacities[name])
                self.assertEqual(prepared_fresh["rootfsA"], prepared_fresh["rootfsB"])
                prepared_preserved = updates.prepare_images(package, True, capacities, root / "preserve")
            self.assertNotIn("var", prepared_preserved)
            self.assertFalse((root / "preserve" / "var.ext4").exists())
            for filename, original_hash in source_hashes.items():
                self.assertEqual(hashlib.sha256((images_dir / filename).read_bytes()).hexdigest(), original_hash)
            for image in set(prepared_fresh.values()):
                result = subprocess.run(["e2fsck", "-f", "-n", str(image)],
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive_path = root / "jibo-pvt-flash-build-test.tar.bz2"
            with tarfile.open(archive_path, "w:bz2") as archive:
                for filename in updates.IMAGE_NAMES:
                    archive.add(images_dir / filename,
                                arcname="release/flash_jibo/output/images/" + filename)
            archived_package = updates.validate_package(archive_path)
            with patch.dict(updates.KNOWN_CAPACITIES, small_known, clear=True):
                prepared_archive = updates.prepare_images(archived_package, True, capacities,
                                                          root / "from-archive")
            self.assertEqual(prepared_archive["skills"].stat().st_size, capacities["skills"])
            self.assertNotIn("var", prepared_archive)

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "resize2fs", "e2fsck")),
                         "e2fsprogs is required for the ext4 timestamp comparison")
    def test_resize_timestamp_is_reproducible_and_only_timestamp_differences_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ext4"
            with source.open("wb") as stream:
                stream.truncate(4 * 1024 * 1024)
            subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024",
                            "-O", "^metadata_csum", str(source)], check=True, capture_output=True)
            copies = [root / "first.ext4", root / "second.ext4"]
            for copy in copies:
                shutil.copyfile(source, copy)
                with copy.open("r+b") as stream:
                    stream.truncate(16 * 1024 * 1024)
                subprocess.run(["resize2fs", "-f", str(copy), "16384"],
                               check=True, capture_output=True)
                with copy.open("r+b") as stream:
                    stream.truncate(16 * 1024 * 1024)
            with copies[1].open("r+b") as stream:
                for position in (1072, 8 * 1024 * 1024 + 1072):
                    stream.seek(position)
                    stream.write(b"\x01\x02\x03\x04")
            for copy in copies:
                updates._restore_ext4_write_time(source, copy)
                check = subprocess.run(["e2fsck", "-f", "-n", str(copy)],
                                       text=True, capture_output=True)
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual(hashlib.sha256(copies[0].read_bytes()).hexdigest(),
                             hashlib.sha256(copies[1].read_bytes()).hexdigest())
            with copies[1].open("r+b") as stream:
                stream.seek(1072)
                stream.write(b"\x05\x06\x07\x08")
            self.assertTrue(updates.equivalent_except_ext4_write_time(copies[0], copies[1]))
            with copies[1].open("r+b") as stream:
                stream.seek(4096)
                stream.write(b"\xff")
            self.assertFalse(updates.equivalent_except_ext4_write_time(copies[0], copies[1]))


class CompactFlashIntegrationTests(unittest.TestCase):
    def test_preserved_var_rearm_happens_after_prefix_writes_and_before_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            candidate = root / "compact.ext4"
            candidate.write_bytes(b"P" * 512)
            operation = root / "operation"
            operation.mkdir()
            capacities = {name: size for name, size in updates.KNOWN_CAPACITIES.items()}
            capacities["skills"] = 10_991_139_328
            names = ["rootfsA", "rootfsB", "services", "skills", "var", "emmc-000",
                     toolkit.MARKER]
            listing = "\n".join('Found DFU: alt={}, name="{}"'.format(index, name)
                                for index, name in enumerate(names))
            events = []

            def transfer(argv, **_kwargs):
                if "-D" in argv:
                    events.append("write-" + argv[argv.index("-a") + 1])

            def rearm(*args, **_kwargs):
                events.append("rearm")
                self.assertEqual(args[0], "var")
                self.assertEqual(args[1], "/etc/first_boot_resize")
                self.assertNotIn(b"mmcblk0p5", args[2])
                self.assertIn(b"printf '%s\\n'", args[2])
                return {"status": "verified", "operation_directory": "/tmp/file-transaction"}

            def reset(argv, **_kwargs):
                self.assertIn("-R", argv)
                events.append("reset")
                return ""

            with patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context",
                                 return_value=("1-1", names, "serial-sha256:" + "a" * 64, listing)), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value={"image": "saved-var.img"}), \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set({name: candidate for name in
                                                           ("rootfsA", "rootfsB", "services", "skills")})), \
                    patch.object(toolkit, "_preflight_resize_script_write",
                                 side_effect=lambda *_args, **_kwargs: events.append("preflight") or
                                 {"allocated_bytes": 1024}), \
                    patch.object(toolkit, "write_partition_file_live", side_effect=rearm), \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", side_effect=reset):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", True, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE")

            self.assertEqual(events, ["preflight", "write-rootfsA", "write-rootfsB",
                                      "write-services", "write-skills", "rearm", "reset"])
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(saved["image_strategy"], "compact-prefix-v1")
            self.assertEqual(saved["writes"][-1]["size_bytes"], capacities["skills"])
            self.assertEqual(saved["writes"][-1]["transfer_bytes"], 512)
            self.assertEqual(saved["first_boot_resize_rearm"]["status"], "verified")


if __name__ == "__main__":
    unittest.main()

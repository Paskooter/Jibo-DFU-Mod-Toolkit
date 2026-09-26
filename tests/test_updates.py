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
    def _resume_fixture(self, root, first_candidate_hash=None):
        package_path = root / "jibo-13.0.0-update.tar.bz2"
        with tarfile.open(package_path, "w:bz2") as archive:
            add_image_files(archive)
        contents = {"rootfsA": b"A001", "rootfsB": b"B001",
                    "services": b"S001", "skills": b"K001"}
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
        capacities = {"rootfsA": 4, "rootfsB": 4, "services": 4,
                      "skills": 4, "var": 8}
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
            "usb_port": "1-1", "var_policy": "preserve current configuration",
            "partitions": partitions,
            "backups": {"var": {"status": "backup complete",
                                  "image": str(var_image), "manifest": str(backup_manifest),
                                  "size_bytes": 8, "sha256": var_hash}},
            "writes": [{"partition": "rootfsA", "candidate_sha256": first_hash,
                        "size_bytes": 4, "status": "write started"}],
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
            patch.object(toolkit.updates, "prepare_images", return_value=candidates),
            patch.object(toolkit, "_upload_var", return_value=var_hash),
            patch.object(toolkit, "_upload_partition", side_effect=upload_side_effect),
            patch.object(toolkit, "run_with_progress"),
            patch.object(toolkit, "run", return_value=""),
        ]

    def test_resume_skips_recorded_partition_only_after_full_matching_readback(self):
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
            self.assertEqual(result["status"], "verified; reset requested")
            self.assertEqual(uploads, ["rootfsA", "rootfsB", "services", "skills"])
            live_var.assert_called_once()
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
            self.assertEqual(calls["rootfsA"], 2)
            saved = json.loads(Path(result["manifest"]).read_text())
            self.assertFalse(saved["writes"][0].get("resumed_without_write", False))

    def test_resume_refuses_when_live_var_does_not_match_saved_rollback(self):
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
                        with self.assertRaisesRegex(toolkit.DfuError, "var SHA-256 does not match"):
                            toolkit.flash_update(
                                package_path, True, "1-1", "dfu-util", None,
                                "FLASH UPDATE", False, manifest)
            live_var.assert_called_once()
            readback.assert_not_called()
            transfer.assert_not_called()
            reset.assert_not_called()

    def test_resume_fails_closed_when_fresh_candidate_differs_from_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_hash = hashlib.sha256(b"different package candidate").hexdigest()
            (package_path, package, candidates, hashes, var_hash, capacities,
             manifest, names, alt_output, operation) = self._resume_fixture(
                 root, first_candidate_hash=bad_hash)
            patches = self._patch_resume_context(
                package, candidates, capacities, names, alt_output, operation,
                var_hash, lambda *_args: hashes["rootfsA"])
            with patch.object(toolkit, "EXPECTED_VAR_SIZE", 8):
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6] as live_var, patches[7] as readback, \
                        patches[8] as transfer, patches[9] as reset:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(toolkit.DfuError, "Freshly prepared rootfsA"):
                            toolkit.flash_update(
                                package_path, True, "1-1", "dfu-util", None,
                                "FLASH UPDATE", False, manifest)
            live_var.assert_not_called()
            readback.assert_not_called()
            transfer.assert_not_called()
            reset.assert_not_called()

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
            payload = b"services readback"
            expected_hash = hashlib.sha256(payload).hexdigest()
            manifest = root / "update-manifest.json"
            manifest.write_text(json.dumps({
                "kind": "jibo-full-flash-update", "writes": [{
                    "partition": "services", "size_bytes": len(payload),
                    "candidate_sha256": expected_hash, "status": "write started"}]}))
            readback_paths = []

            def upload(_tool, _port, _name, _size, destination):
                readback_paths.append(Path(destination))
                Path(destination).write_bytes(payload)
                return hashlib.sha256(payload).hexdigest()

            with patch.object(toolkit, "_dfu_context",
                              return_value=("1-1", ["var", "services"], "device")), \
                    patch.object(toolkit, "_read_gpt_capacities",
                                 return_value={"services": len(payload)}), \
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
                    "partition": "services", "size_bytes": 16,
                    "candidate_sha256": "a" * 64}]}))
            with patch.object(toolkit, "_dfu_context",
                              return_value=("1-1", ["var", "services"], "device")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value={"services": 32}), \
                    patch.object(toolkit, "_upload_partition") as read:
                with self.assertRaisesRegex(toolkit.DfuError, "does not match"):
                    toolkit.verify_update_write(manifest, "services", "1-1", "dfu-util")
            read.assert_not_called()

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

    def test_preserve_var_flash_never_writes_var_and_verifies_each_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            candidate = root / "prepared.img"
            candidate.write_bytes(b"prepared")
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
                    patch.object(toolkit.updates, "prepare_images", return_value={name: candidate for name in ("rootfsA", "rootfsB", "services", "skills")}), \
                    patch.object(toolkit, "_upload_partition", return_value=toolkit._sha256_file(candidate)) as upload, \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", True, port="1-1", dfu_util="dfu-util",
                                                  confirmation="FLASH UPDATE")
            written = [argv[argv.index("-a") + 1] for argv in transfers if "-D" in argv]
            self.assertEqual(written, ["rootfsA", "rootfsB", "services", "skills"])
            self.assertEqual(download_sizes,
                             {name: capacities[name] for name in written})
            var_backup.assert_called_once_with("1-1", "dfu-util")
            backup.assert_not_called()
            skills_backup.assert_not_called()
            self.assertEqual(upload.call_count, 4)
            self.assertEqual(result["status"], "verified; reset requested")
            self.assertEqual(result["backups"], {"var": {"image": "saved-var.img"}})

    def test_fresh_var_flash_backs_up_only_var_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images_dir = root / "release" / "flash_jibo" / "output" / "images"
            images_dir.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (images_dir / filename).write_bytes(b"package placeholder")
            candidate = root / "prepared.img"
            candidate.write_bytes(b"prepared")
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
                    patch.object(toolkit.updates, "prepare_images",
                                 return_value={name: candidate for name in toolkit.UPDATE_ORDER}), \
                    patch.object(toolkit, "_upload_partition", return_value=toolkit._sha256_file(candidate)) as upload, \
                    patch.object(toolkit, "run_with_progress", side_effect=lambda argv, **_kwargs: transfers.append(argv)), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", False, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE")
            var_backup.assert_called_once_with("1-1", "dfu-util")
            other_backup.assert_not_called()
            skills_backup.assert_not_called()
            self.assertEqual(result["backups"], {"var": {"image": "saved-var.img"}})
            self.assertEqual(upload.call_count, 5)
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
            rootfs.write_bytes(b"root")
            services.write_bytes(b"serv")
            skills_payload = bytes(range(256)) * 10
            skills.write_bytes(skills_payload)
            operation = root / "operation"
            operation.mkdir()
            capacities = {"rootfsA": 4, "rootfsB": 4, "services": 4,
                          "var": toolkit.EXPECTED_VAR_SIZE, "skills": len(skills_payload)}
            chunk_names = ("skills-000", "skills-001", "skills-002")
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
                    patch.object(toolkit.updates, "prepare_images", return_value=prepared), \
                    patch.object(toolkit, "_upload_partition", side_effect=upload), \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = toolkit.flash_update(root / "release", True, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE")

            var_backup.assert_called_once_with("1-1", "dfu-util")
            skills_backup.assert_not_called()
            backup.assert_not_called()
            self.assertEqual([written[name] for name in chunk_names],
                             [skills_payload[:1024], skills_payload[1024:2048], skills_payload[2048:]])
            self.assertEqual([download_sizes[name] for name in chunk_names], [1024, 1024, 512])
            self.assertEqual(result["status"], "verified; reset requested")
            manifest = json.loads((operation / "update-manifest.json").read_text())
            skills_write = next(item for item in manifest["writes"] if item["partition"] == "skills")
            self.assertEqual([item["alternative"] for item in skills_write["chunks"]], list(chunk_names))
            self.assertTrue(all(item["status"] == "verified" for item in skills_write["chunks"]))

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


if __name__ == "__main__":
    unittest.main()

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

            def upload(_tool, _port, _name, _size, destination, alternative_size=None):
                self.assertEqual(alternative_size, 1024)
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

            def upload(_tool, _port, alternative, size, destination, alternative_size=None):
                reads.append((alternative, size, alternative_size))
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
            self.assertEqual(reads, [("skills-000", 1024, 1024), ("skills-001", 512, 1024)])

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
                    result = toolkit.flash_update(root / "release", "without-calibration", unsafe_without_calibration=True, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE",
                                                  verify_readback=True)
            var_backup.assert_called_once_with("1-1", "dfu-util", refresh=True)
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
                        "services": services, "skills": skills, "var": rootfs}
            written = {}
            download_sizes = {}

            def transfer(argv, timeout, label, download_size=None,
                         allow_progress_completion=False):
                self.assertTrue(allow_progress_completion)
                if "-D" in argv:
                    alternative = argv[argv.index("-a") + 1]
                    written[alternative] = Path(argv[argv.index("-D") + 1]).read_bytes()
                    download_sizes[alternative] = download_size

            def upload(_dfu_util, _port, alternative, size, destination, _alternative_size=None):
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
                    result = toolkit.flash_update(root / "release", "without-calibration", unsafe_without_calibration=True, port="1-1",
                                                  dfu_util="dfu-util", confirmation="FLASH UPDATE")

            var_backup.assert_called_once_with("1-1", "dfu-util", refresh=True)
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

            def upload(_tool, _port, alternative, size, destination, _alternative_size=None):
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
                compact = updates.prepare_compact_images(package, capacities,
                                                         root / "compact")
            self.assertIn("var", compact.images)
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

    def test_rtm3_first_boot_resize_runs_through_rcs(self):
        files = {
            "/etc/inittab": b"::sysinit:/etc/init.d/rcS\n",
            "/etc/init.d/rcS": (b"for i in /etc/init.d/S??* ;do\n"
                                 b"    $i start\n"
                                 b"done\n"),
            "/etc/init.d/S03fs-resize": (
                b"#!/bin/sh\ncase \"$1\" in\n    start)\n"
                b"        /var/etc/first_boot_resize\n"
                b"        /bin/mv /var/etc/first_boot_resize "
                b"/var/etc/first_boot_resize.done\n        ;;\nesac\n"),
        }
        with patch.object(updates, "_read_ext4_file",
                          side_effect=lambda _image, path, _description: files[path]):
            self.assertTrue(updates._runs_first_boot_resize(Path("rootfs.ext4")))
            files["/etc/init.d/S03fs-resize"] = b"#!/bin/sh\nexit 0\n"
            self.assertFalse(updates._runs_first_boot_resize(Path("rootfs.ext4")))



    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "e2fsck", "dumpe2fs", "resize2fs")),
                         "e2fsprogs is required for the offline resize check")
    def test_prepare_expands_all_partitions_offline_and_leaves_inputs_unchanged(self):
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
                prepared_fresh = updates.prepare_images(package, capacities, root / "fresh")
                for name in ("rootfsA", "rootfsB", "services", "skills", "var"):
                    self.assertEqual(prepared_fresh[name].stat().st_size, capacities[name])
                self.assertEqual(prepared_fresh["rootfsA"], prepared_fresh["rootfsB"])
                prepared_again = updates.prepare_images(package, capacities, root / "again")
            self.assertIn("var", prepared_again)
            self.assertTrue((root / "again" / "var.ext4").exists())
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
                prepared_archive = updates.prepare_images(archived_package, capacities,
                                                          root / "from-archive")
            self.assertEqual(prepared_archive["skills"].stat().st_size, capacities["skills"])
            self.assertIn("var", prepared_archive)

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "resize2fs", "e2fsck")),
                         "e2fsprogs is required for the ext4 timestamp comparison")
    def test_resize_timestamp_is_reproducible_and_only_timestamp_differences_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ext4"
            with source.open("wb") as stream:
                stream.truncate(4 * 1024 * 1024)
            subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024", "-I", "256",
                            "-O", "resize_inode,^metadata_csum", str(source)],
                           check=True, capture_output=True)
            source_bytes = source.read_bytes()
            # Group-zero inode table, then the seventh 256-byte inode.
            inode7 = struct.unpack_from("<I", source_bytes, 2048 + 8)[0] * 1024 + 6 * 256
            copies = [root / "first.ext4", root / "second.ext4"]
            # Exercise different e2fsprogs clock ticks without sleeping or
            # depending on how quickly a particular CI runner can resize.
            for copy, timestamp in zip(copies, (1_600_000_000, 1_700_000_000)):
                shutil.copyfile(source, copy)
                with copy.open("r+b") as stream:
                    stream.truncate(16 * 1024 * 1024)
                with patch.dict("os.environ", {"E2FSPROGS_FAKE_TIME": str(timestamp)}):
                    subprocess.run(["resize2fs", "-f", str(copy), "16384"],
                                   check=True, capture_output=True)
                with copy.open("r+b") as stream:
                    stream.truncate(16 * 1024 * 1024)
                # Validate the clock injection itself, not just a hash that
                # could pass by chance if both resizes used wall-clock time.
                self.assertEqual(struct.unpack_from("<I", copy.read_bytes(), inode7 + 8)[0],
                                 timestamp)
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
            self.assertEqual(source.read_bytes(), source_bytes)
            for start, end in ((8, 20), (132, 152)):
                self.assertEqual(copies[0].read_bytes()[inode7 + start:inode7 + end],
                                 source_bytes[inode7 + start:inode7 + end])
            with copies[1].open("r+b") as stream:
                stream.seek(1072)
                stream.write(b"\x05\x06\x07\x08")
            self.assertTrue(updates.equivalent_except_ext4_write_time(copies[0], copies[1]))
            timestamp_only = copies[1].read_bytes()
            # Each mutant starts with only the allowed superblock difference.
            # Adjacent inode fields and even inode timestamps remain outside
            # the equivalence allowance; reproducibility must fix the writer.
            for position in (4096, inode7 + 20, inode7 + 152, inode7 + 8):
                with self.subTest(mutant_offset=position):
                    copies[1].write_bytes(timestamp_only)
                    with copies[1].open("r+b") as stream:
                        stream.seek(position)
                        stream.write(b"\xff" if position == 4096 else
                                     bytes([timestamp_only[position] ^ 0xff]))
                    self.assertFalse(updates.equivalent_except_ext4_write_time(copies[0], copies[1]))

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "resize2fs", "e2fsck", "dumpe2fs")),
                         "e2fsprogs is required for reproducible update preparation")
    def test_prepare_classic_ext4_is_reproducible_with_and_without_resize_inode(self):
        # Cover 128-byte legacy inodes as well as the large-inode regression
        # above. Real stock images do not have the resize_inode feature.
        for resize_feature in ("resize_inode", "^resize_inode"):
            with self.subTest(feature=resize_feature), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                images_dir = root / "release" / "flash_jibo" / "output" / "images"
                images_dir.mkdir(parents=True)
                source = images_dir / "rootfs.ext4"
                with source.open("wb") as stream:
                    stream.truncate(4 * 1024 * 1024)
                with patch.dict("os.environ", {"E2FSPROGS_FAKE_TIME": "1500000000"}):
                    subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", "-b", "1024",
                                    "-I", "128", "-O", resize_feature + ",^metadata_csum,^64bit",
                                    str(source)], check=True, capture_output=True)
                for filename in updates.IMAGE_NAMES:
                    if filename != source.name:
                        shutil.copyfile(source, images_dir / filename)
                source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in images_dir.iterdir()}
                package = updates.validate_package(root)
                capacities = {name: 16 * 1024 * 1024 for name in
                              ("rootfsA", "rootfsB", "services", "var", "skills")}
                prepared = []
                for timestamp in (1_600_000_000, 1_700_000_000):
                    with patch.dict(updates.KNOWN_CAPACITIES, capacities, clear=True), \
                            patch.dict("os.environ", {"E2FSPROGS_FAKE_TIME": str(timestamp)}):
                        prepared.append(updates.prepare_images(
                            package, capacities, root / str(timestamp)))
                for name in capacities:
                    self.assertEqual(hashlib.sha256(prepared[0][name].read_bytes()).hexdigest(),
                                     hashlib.sha256(prepared[1][name].read_bytes()).hexdigest(), name)
                self.assertEqual(source_hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                 for p in images_dir.iterdir()})







class FactoryFreshVarTests(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs required")
    def test_fresh_v3_restores_imu_audio_bytes_modes_and_reports_missing_items(self):
        from calibration_support import FILES, PATHS, TEMP_DIR, IDENTITY, make_image
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp:
            root = Path(temp)
            stock = make_image(root, "stock.ext4", {PATHS[k]: FILES[k] for k in
                                                    ("left", "right", "inter_camera")})
            metadata = {PATHS["imu"]: {"mode": "0100644", "uid": "0", "gid": "0"},
                        PATHS["audio"]: {"mode": "0100600", "uid": "0", "gid": "0"}}
            backup = make_image(root, "backup.ext4", {**{PATHS[k]: v for k, v in FILES.items()},
                                                       "/jibo/identity.json": IDENTITY}, metadata)
            before = toolkit.images.sha256_file(backup)
            built = toolkit.images.build_factory_fresh_var(stock, backup, root / "fresh.ext4")
            for key, mode in (("imu", "0100644"), ("audio", "0100600")):
                self.assertEqual(toolkit.images._extract(built["image"], PATHS[key], root / "out"), FILES[key])
                self.assertEqual(toolkit.images._file_metadata(built["image"], PATHS[key]),
                                 {"mode": mode, "uid": "0", "gid": "0"})
                self.assertEqual(built["calibration_status"][key], "restored")
                self.assertIn(PATHS[key], built["calibration_files"])
            self.assertEqual(before, toolkit.images.sha256_file(backup))
            partial = make_image(root, "partial.ext4", {**{PATHS[k]: FILES[k] for k in
                                                          ("left", "right", "inter_camera")},
                                                        "/jibo/identity.json": IDENTITY})
            missing = toolkit.images.build_factory_fresh_var(stock, partial, root / "partial-fresh.ext4")
            for key in ("imu", "audio"):
                self.assertEqual(missing["calibration_status"][key], "absent")
            for key in ("touchscreen", "factory_record"):
                self.assertEqual(missing["calibration_status"][key], "not preserved (no backup of that partition)")

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs required")
    def test_fresh_v3_removes_every_distortion_cache_from_candidate_only(self):
        from calibration_support import FILES, PATHS, TEMP_DIR, IDENTITY, make_image, debug
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp:
            root = Path(temp)
            caches = ["/jibo/lps/distortionPrecomputeFile_" + suffix for suffix in
                      ("left_eye_1", "right_eye_0", "left_eye_0", "right_eye_1", "future_variant")]
            cameras = {PATHS[k]: FILES[k] for k in ("left", "right", "inter_camera")}
            stock = make_image(root, "stock.ext4", {**cameras, **{p: b"stock cache" for p in caches}})
            backup = make_image(root, "backup.ext4", {**cameras, "/jibo/identity.json": IDENTITY,
                                                       **{p: b"saved cache" for p in caches}})
            debug(stock, "symlink /jibo/lps/distortionPrecomputeFile_link " + PATHS["left"])
            before = [toolkit.images.sha256_file(p) for p in (stock, backup)]
            built = toolkit.images.build_factory_fresh_var(stock, backup, root / "fresh.ext4")
            for path in caches + ["/jibo/lps/distortionPrecomputeFile_link"]:
                self.assertIsNone(toolkit.images._extract(built["image"], path, root / "out", optional=True))
            self.assertEqual(before, [toolkit.images.sha256_file(p) for p in (stock, backup)])
            self.assertEqual(set(built["removed_distortion_caches"]), set(caches + ["/jibo/lps/distortionPrecomputeFile_link"]))

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs required")
    def test_fresh_v3_reports_unsupported_optional_files_without_copying(self):
        from calibration_support import FILES, PATHS, TEMP_DIR, IDENTITY, make_image, debug
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp:
            root = Path(temp)
            cameras = {PATHS[k]: FILES[k] for k in ("left", "right", "inter_camera")}
            stock = make_image(root, "stock.ext4", cameras)
            backup = make_image(root, "backup.ext4", {**cameras, "/jibo/identity.json": IDENTITY,
                                                       PATHS["imu"]: b'{"private": "secret"}',
                                                       PATHS["audio"]: b"oversize" * 100})
            before = toolkit.images.sha256_file(backup)
            built = toolkit.images.build_factory_fresh_var(stock, backup, root / "fresh.ext4")
            for key in ("imu", "audio"):
                self.assertEqual(built["calibration_status"][key], "kept_local_unsupported")
                self.assertIsNone(toolkit.images._extract(built["image"], PATHS[key], root / "out", optional=True))
            self.assertEqual(before, toolkit.images.sha256_file(backup))

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs required")
    def test_fresh_v3_conditional_partitions_preserve_existing_items_only(self):
        from calibration_support import PATHS, TEMP_DIR, POINTERCAL, FACTORY_RECORD, make_image
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp:
            root = Path(temp)
            for partition, key, content in (("rootfs", "touchscreen", POINTERCAL),
                                             ("skills", "factory_record", FACTORY_RECORD)):
                stock = make_image(root, partition + "-stock.ext4", {})
                metadata = {PATHS[key]: {"mode": "0100640", "uid": "0", "gid": "10"}}
                backup = make_image(root, partition + "-backup.ext4", {PATHS[key]: content}, metadata)
                before = toolkit.images.sha256_file(backup)
                built = toolkit.images.build_calibrated_partition(stock, backup, root / (partition + "-fresh.ext4"), partition)
                self.assertEqual(toolkit.images._extract(built["image"], PATHS[key], root / "out"), content)
                self.assertEqual(toolkit.images._file_metadata(built["image"], PATHS[key]),
                                 {"mode": "0100640", "uid": "0", "gid": "10"})
                self.assertEqual(built["calibration_status"][key], "restored")
                self.assertEqual(Path(built["image"]).stat().st_size, stock.stat().st_size)
                self.assertEqual(before, toolkit.images.sha256_file(backup))
                missing = toolkit.images.build_calibrated_partition(stock, stock, root / (partition + "-absent.ext4"), partition)
                self.assertEqual(missing["calibration_status"][key], "absent")
                self.assertEqual(Path(missing["image"]), stock)
                unchecked = toolkit.images.build_calibrated_partition(stock, None, root / (partition + "-unchecked.ext4"), partition)
                self.assertEqual(unchecked["calibration_status"][key], "not preserved (no backup of that partition)")
                self.assertIsNone(toolkit.images._extract(unchecked["image"], PATHS[key], root / "out", optional=True))

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs required")
    def test_fresh_v3_flash_uses_same_robot_partition_backups_before_any_write(self):
        from calibration_support import FILES, PATHS, TEMP_DIR, IDENTITY, POINTERCAL, FACTORY_RECORD, make_image
        with tempfile.TemporaryDirectory(dir=TEMP_DIR) as temp:
            root = Path(temp)
            folder = root / "release/flash_jibo/output/images"
            folder.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (folder / filename).write_bytes(b"package placeholder")
            stock = make_image(root, "stock.ext4", {PATHS[k]: FILES[k] for k in
                                                    ("left", "right", "inter_camera")})
            saved_var = make_image(root, "var-backup.ext4", {**{PATHS[k]: v for k, v in FILES.items()},
                                                            "/jibo/identity.json": IDENTITY})
            saved_root = make_image(root, "rootfs-backup.ext4", {PATHS["touchscreen"]: POINTERCAL})
            saved_skills = make_image(root, "skills-backup.ext4", {PATHS["factory_record"]: FACTORY_RECORD})
            tag = "serial-sha256:" + "a" * 64
            backups = root / "backups"
            for name, image in (("rootfsA", saved_root), ("skills", saved_skills)):
                directory = backups / ("partition-backup-zzz-" + name)
                directory.mkdir(parents=True)
                shutil.copyfile(image, directory / "partition.img")
                (directory / "backup-manifest.json").write_text(json.dumps({
                    "kind": "jibo-partition-backup", "device_tag": tag, "partition": name,
                    "size_bytes": image.stat().st_size, "sha256": toolkit.images.sha256_file(image)}))
            # An older same-robot backup without pointercal must not hide a later valid one.
            older = backups / "partition-backup-aaa-rootfsA"
            older.mkdir()
            shutil.copyfile(stock, older / "partition.img")
            (older / "backup-manifest.json").write_text(json.dumps({
                "kind": "jibo-partition-backup", "device_tag": tag, "partition": "rootfsA",
                "size_bytes": stock.stat().st_size, "sha256": toolkit.images.sha256_file(stock)}))
            # A valid but OTHER robot's rootfsA must never be selected.
            foreign = backups / "partition-backup-rootfsA"
            foreign.mkdir()
            shutil.copyfile(stock, foreign / "partition.img")
            (foreign / "backup-manifest.json").write_text(json.dumps({
                "kind": "jibo-partition-backup", "device_tag": "serial-sha256:" + "b" * 64,
                "partition": "rootfsA", "size_bytes": stock.stat().st_size,
                "sha256": toolkit.images.sha256_file(stock)}))
            capacities = {name: stock.stat().st_size for name in toolkit.UPDATE_ORDER}
            before = [toolkit.images.sha256_file(p) for p in (saved_var, saved_root, saved_skills)]
            var_manifest = root / "backup-manifest.json"
            var_manifest.write_text(json.dumps({"kind": "jibo-var-backup", "device_tag": tag,
                                                "sha256": before[0]}))
            backup = {"image": str(saved_var), "sha256": before[0], "manifest": str(var_manifest)}
            operation = root / "operation"
            operation.mkdir()
            written = []

            def transfer(argv, **_kwargs):
                name = argv[argv.index("-a") + 1]
                image = argv[argv.index("-D") + 1]
                key = "touchscreen" if name in ("rootfsA", "rootfsB") else "factory_record" if name == "skills" else None
                if key:
                    self.assertEqual(toolkit.images._extract(image, PATHS[key], root / "out"),
                                     POINTERCAL if key == "touchscreen" else FACTORY_RECORD)
                written.append(name)

            with patch.object(toolkit, "BACKUP_ROOT", backups), \
                    patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context", return_value=("1-1", list(toolkit.UPDATE_ORDER), tag, "")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value=backup), \
                    patch.object(toolkit, "_select_factory_calibration_backup", return_value=backup), \
                    patch.object(toolkit, "EXPECTED_VAR_SIZE", saved_var.stat().st_size), \
                    patch.object(toolkit.updates, "prepare_compact_images", return_value=compact_set(
                        {name: stock for name in toolkit.UPDATE_ORDER})), \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = toolkit.flash_update(root / "release", "keep-calibration", port="1-1", dfu_util="dfu-util",
                                              confirmation="FLASH UPDATE")
            manifest = json.loads(Path(result["manifest"]).read_text())
            for key in ("imu", "audio", "touchscreen", "factory_record"):
                self.assertEqual(manifest["factory_calibration"]["status"][key], "restored")
            self.assertEqual(written, list(toolkit.UPDATE_ORDER))
            self.assertEqual(before, [toolkit.images.sha256_file(p) for p in (saved_var, saved_root, saved_skills)])

    @unittest.skipUnless(all(shutil.which(name) for name in ("mke2fs", "debugfs", "e2fsck")),
                         "e2fsprogs is required for factory var tests")
    def test_factory_fresh_var_keeps_only_robot_identity_and_camera_calibration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stock, backup, result = (root / name for name in
                                     ("stock.ext4", "backup.ext4", "result.ext4"))
            for image in (stock, backup):
                with image.open("wb") as stream:
                    stream.truncate(8 * 1024 * 1024)
                subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", str(image)],
                               check=True, capture_output=True)
                for directory in ("/jibo", "/jibo/lps", "/etc"):
                    subprocess.run(["debugfs", "-w", "-R", "mkdir " + directory,
                                    str(image)], check=True, capture_output=True)

            def put(image, path, content):
                host = root / "payload"
                host.write_bytes(content)
                subprocess.run(["debugfs", "-w", "-R",
                                "write {} {}".format(host, path), str(image)],
                               check=True, capture_output=True)

            for path in toolkit.images.FACTORY_CAMERA_PATHS:
                put(stock, path, b'{"value":"default"}')
                put(backup, path, b'{"value":"calibrated"}')
            put(backup, "/jibo/identity.json", json.dumps({
                "name": "Example-Unit-Test-Robot", "serial_number": "BOJW-1000-0001-0002-0003",
                "cpuid": "123", "wifi_mac": "00:11:22:33:44:55"}).encode())
            put(backup, "/etc/wpa_supplicant.conf", b"old-user-network")
            put(stock, "/etc/wpa_supplicant.conf", b"stock-network-template")
            put(stock, "/etc/hostname", b"jibo\n")
            put(stock, "/etc/hosts", b"127.0.0.1 localhost\n")
            self.assertTrue(toolkit.images.factory_calibration_available(backup, stock))
            built = toolkit.images.build_factory_fresh_var(stock, backup, result)
            self.assertEqual(result.stat().st_size, stock.stat().st_size)
            self.assertEqual(built["mode"], "oobe")
            self.assertFalse(built["camera_matches_stock"])
            for path in toolkit.images.FACTORY_CAMERA_PATHS:
                self.assertEqual(toolkit.images._extract(
                    result, path, root / "out"), b'{"value":"calibrated"}')
                self.assertEqual(toolkit.images._extract(
                    stock, path, root / "out"), b'{"value":"default"}')
            self.assertEqual(toolkit.images._extract(
                result, "/jibo/mode.json", root / "out"), b'{"mode":"oobe"}')
            self.assertEqual(toolkit.images._extract(
                result, "/etc/wpa_supplicant.conf", root / "out"), b"stock-network-template")
            self.assertIsNone(toolkit.images._extract(
                stock, "/jibo/identity.json", root / "out", optional=True))
            self.assertEqual(toolkit.images._file_metadata(
                result, "/jibo/identity.json")["mode"], "0100600")
            for path in toolkit.images.FACTORY_CAMERA_PATHS:
                subprocess.run(["debugfs", "-w", "-R", "rm " + path, str(backup)],
                               check=True, capture_output=True)
                put(backup, path, b'{ "value" : "default" }')
            self.assertTrue(toolkit.images.factory_calibration_available(backup, stock))
            stock_matched = toolkit.images.build_factory_fresh_var(
                stock, backup, root / "stock-matched.ext4")
            self.assertTrue(stock_matched["camera_matches_stock"])

    def test_calibrated_flash_writes_edited_var_after_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "release" / "flash_jibo" / "output" / "images"
            folder.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (folder / filename).write_bytes(b"package placeholder")
            stock = root / "stock.img"
            stock.write_bytes(b"S" * 512)
            calibrated = root / "calibrated.img"
            calibrated.write_bytes(b"C" * 512)
            operation = root / "operation"
            operation.mkdir()
            capacities = dict(updates.KNOWN_CAPACITIES)
            capacities["skills"] = 10_991_139_328
            names = ["rootfsA", "rootfsB", "services", "skills", "var", "emmc-000"]
            events = []
            backup = {"image": "backup.img", "sha256": "a" * 64,
                      "manifest": "backup-manifest.json"}

            def save_backup(*_args, **_kwargs):
                events.append("backup")
                return backup

            def build(*_args):
                events.append("calibrate")
                return {"image": str(calibrated), "sha256": toolkit._sha256_file(calibrated),
                        "calibration_files": list(toolkit.images.FACTORY_CAMERA_PATHS)}

            def transfer(argv, **_kwargs):
                events.append("write-" + argv[argv.index("-a") + 1])
                if argv[argv.index("-a") + 1] == "var":
                    self.assertIn(str(calibrated), argv)

            with patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context", return_value=("1-1", names, "device", "")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", side_effect=save_backup), \
                    patch.object(toolkit, "_select_factory_calibration_backup", return_value=backup), \
                    patch.object(toolkit.images, "build_factory_fresh_var", side_effect=build), \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set({name: stock for name in toolkit.UPDATE_ORDER})), \
                    patch.object(toolkit, "run_with_progress", side_effect=transfer), \
                    patch.object(toolkit, "run", return_value=""):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    flash = toolkit.flash_update(root / "release", "keep-calibration", port="1-1",
                                                 dfu_util="dfu-util", confirmation="FLASH UPDATE")
            self.assertEqual(events[:2], ["backup", "calibrate"])
            self.assertEqual(events[-1], "write-var")
            manifest = json.loads(Path(flash["manifest"]).read_text())
            self.assertEqual(manifest["factory_calibration"]["candidate_sha256"],
                             toolkit._sha256_file(calibrated))
            self.assertEqual(flash["var_policy"], "keep-calibration")

    def test_calibrated_flash_refuses_missing_real_calibration_before_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "release" / "flash_jibo" / "output" / "images"
            folder.mkdir(parents=True)
            for filename in updates.IMAGE_NAMES:
                (folder / filename).write_bytes(b"package placeholder")
            stock = root / "stock.img"
            stock.write_bytes(b"S" * 512)
            operation = root / "operation"
            operation.mkdir()
            capacities = dict(updates.KNOWN_CAPACITIES)
            capacities["skills"] = 10_991_139_328
            names = ["rootfsA", "rootfsB", "services", "skills", "var", "emmc-000"]
            backup = {"image": "backup.img", "sha256": "a" * 64,
                      "manifest": "backup-manifest.json"}
            with patch.object(toolkit, "devices", return_value=[{"port": "1-1", "state": "dfu"}]), \
                    patch.object(toolkit, "_dfu_context", return_value=("1-1", names, "device", "")), \
                    patch.object(toolkit, "_read_gpt_capacities", return_value=capacities), \
                    patch.object(toolkit, "_new_operation_dir", return_value=operation), \
                    patch.object(toolkit, "backup_var", return_value=backup) as save, \
                    patch.object(toolkit, "_select_factory_calibration_backup",
                                 side_effect=toolkit.DfuError("no calibration")), \
                    patch.object(toolkit.updates, "prepare_compact_images",
                                 return_value=compact_set({name: stock for name in toolkit.UPDATE_ORDER})), \
                    patch.object(toolkit, "run_with_progress") as transfer:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(toolkit.DfuError, "calibration was not found in a local backup"):
                        toolkit.flash_update(root / "release", "keep-calibration", port="1-1",
                                             dfu_util="dfu-util", confirmation="FLASH UPDATE")
            self.assertEqual(save.call_count, 1)
            self.assertTrue(save.call_args.kwargs["refresh"])
            transfer.assert_not_called()

    @unittest.skipUnless(shutil.which("mke2fs"), "e2fsprogs is required for backup matching")
    def test_calibration_backup_must_match_connected_robot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tag = "serial-sha256:" + "a" * 64
            sources = []
            for index, owner in enumerate((tag, "serial-sha256:" + "b" * 64)):
                directory = root / ("var-backup-" + str(index))
                directory.mkdir()
                image = directory / "var.img"
                with image.open("wb") as stream:
                    stream.truncate(8 * 1024 * 1024)
                subprocess.run(["mke2fs", "-F", "-q", "-t", "ext4", str(image)],
                               check=True, capture_output=True)
                digest = toolkit._sha256_file(image)
                manifest = directory / "backup-manifest.json"
                manifest.write_text(json.dumps({"kind": "jibo-var-backup", "device_tag": owner,
                                                "sha256": digest, "image": "var.img"}))
                sources.append({"image": str(image), "sha256": digest,
                                "manifest": str(manifest)})
            with patch.object(toolkit, "BACKUP_ROOT", root), \
                    patch.object(toolkit, "EXPECTED_VAR_SIZE", 8 * 1024 * 1024), \
                    patch.object(toolkit.images, "factory_calibration_available",
                                 return_value=True):
                selected = toolkit._select_factory_calibration_backup(
                    tag, sources[1], root / "stock.ext4")
            self.assertEqual(selected["manifest"], sources[0]["manifest"])

            # A current backup with the required files takes precedence.
            newer_manifest = Path(sources[1]["manifest"])
            newer = json.loads(newer_manifest.read_text())
            newer["device_tag"] = tag
            newer_manifest.write_text(json.dumps(newer))
            with patch.object(toolkit, "BACKUP_ROOT", root), \
                    patch.object(toolkit, "EXPECTED_VAR_SIZE", 8 * 1024 * 1024), \
                    patch.object(toolkit.images, "factory_calibration_available",
                                 return_value=True):
                selected = toolkit._select_factory_calibration_backup(
                    tag, sources[1], root / "stock.ext4")
            self.assertEqual(selected["manifest"], sources[1]["manifest"])

            # If its files are missing or invalid, an older matching backup is usable.
            with patch.object(toolkit, "BACKUP_ROOT", root), \
                    patch.object(toolkit, "EXPECTED_VAR_SIZE", 8 * 1024 * 1024), \
                    patch.object(toolkit.images, "factory_calibration_available",
                                 side_effect=lambda image, _stock: image == sources[0]["image"]):
                selected = toolkit._select_factory_calibration_backup(
                    tag, sources[1], root / "stock.ext4")
            self.assertEqual(selected["manifest"], sources[0]["manifest"])


if __name__ == "__main__":
    unittest.main()

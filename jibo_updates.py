"""Validate and prepare official Jibo full-flash filesystem packages.

This module does not talk to USB or write to a robot.  It validates extracted
``flash_jibo/output/images`` trees and official tar archives, then makes
temporary ext4 images sized to the partition capacities reported by the
currently connected DFU flasher.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
from typing import Iterator, Mapping
import zlib


class UpdateError(RuntimeError):
    """An update package or image cannot be safely prepared."""


# The 5.4.2 and 13.0.0 T124 Buildroot GPT descriptions use MiB.  DFU reports
# capacities in bytes; requiring these known values also prevents accidentally
# applying a package to a different partition layout.
KNOWN_CAPACITIES = {
    "rootfsA": 1_048_576_000,
    "rootfsB": 1_048_576_000,
    "services": 2_097_152_000,
    "var": 524_288_000,
}
FILESYSTEMS = {
    "rootfsA": "rootfs.ext4",
    "rootfsB": "rootfs.ext4",
    "services": "services.ext4",
    "skills": "skills.ext4",
    "var": "var.ext4",
}
IMAGE_NAMES = tuple(dict.fromkeys(FILESYSTEMS.values()))
ARCHIVE_SUFFIXES = (".tar.bz2", ".tbz2", ".tar.gz", ".tgz", ".tar.xz", ".txz")


@dataclass(frozen=True)
class UpdatePackage:
    """A validated directory or archive containing official ext4 payloads."""

    source: Path
    name: str
    images: Mapping[str, Path]
    archive_members: Mapping[str, str]
    version: str | None = None

    @property
    def is_archive(self) -> bool:
        return bool(self.archive_members)


def _image_set(directory: Path) -> dict[str, Path] | None:
    if directory.is_symlink() or not directory.is_dir():
        return None
    paths: dict[str, Path] = {}
    for filename in IMAGE_NAMES:
        candidate = directory / filename
        if candidate.is_symlink() or not candidate.is_file():
            continue
        paths[filename] = candidate
    # A usable base flash contains all four vendor images.  The caller can
    # preserve var without sending var.ext4, but it must be present to support
    # an explicit fresh/OOBE install.
    if {"rootfs.ext4", "services.ext4", "skills.ext4"}.issubset(paths):
        return paths
    return None


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _find_images_directory(root: Path) -> tuple[Path, dict[str, Path]] | None:
    root = root.expanduser().resolve()
    candidates = (
        root / "flash_jibo" / "output" / "images",
        root / "output" / "images",
        root / "images",
    )
    for directory in candidates:
        if _has_symlink_component(root, directory):
            continue
        paths = _image_set(directory)
        if paths is not None:
            return directory.resolve(), paths
    # Accept a release directory with an extra versioned parent directory.
    # Limit descent to avoid wandering through a user's whole disk.
    if root.is_dir():
        for current, dirs, _ in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            if depth > 5:
                dirs[:] = []
                continue
            if current_path.name == "images":
                paths = _image_set(current_path)
                if paths is not None:
                    return current_path.resolve(), paths
    return None


def _version_from_name(name: str) -> str | None:
    match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)", name)
    return match.group(1) if match else None


def _safe_archive_member(name: str) -> str:
    # tar headers use POSIX paths even on Windows. Reject rather than normalize
    # absolute paths, parent traversals, and empty path components.
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise UpdateError("The update archive contains an unsafe path: " + name)
    normalized = str(path)
    if normalized in (".", ""):
        raise UpdateError("The update archive contains an empty path.")
    return normalized


def _archive_images(source: Path) -> dict[str, str]:
    try:
        with tarfile.open(source, mode="r:*") as archive:
            found: dict[str, str] = {}
            seen_names: set[str] = set()
            for member in archive.getmembers():
                normalized = _safe_archive_member(member.name)
                if normalized in seen_names:
                    raise UpdateError("The update archive has a duplicate path: " + normalized)
                seen_names.add(normalized)
                filename = PurePosixPath(normalized).name
                if filename not in IMAGE_NAMES:
                    continue
                if not normalized.endswith("/output/images/" + filename):
                    continue
                if not member.isfile():
                    raise UpdateError("Update image is not a regular file: " + normalized)
                if filename in found:
                    raise UpdateError("The update archive contains multiple " + filename + " files.")
                if member.size <= 0:
                    raise UpdateError("Update image is empty: " + normalized)
                found[filename] = member.name
            required = {"rootfs.ext4", "services.ext4", "skills.ext4"}
            if not required.issubset(found):
                missing = ", ".join(sorted(required - set(found)))
                raise UpdateError("The update archive is missing required images: " + missing)
            return found
    except UpdateError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError("Could not read update archive " + str(source) + ": " + str(exc)) from exc


def validate_package(path: str | os.PathLike[str]) -> UpdatePackage:
    """Validate one extracted package directory or official tar archive.

    Archives are indexed without extracting them. ``prepare_images`` later
    extracts only the four filesystem members into an automatically removed
    temporary directory.
    """

    selected = Path(path).expanduser()
    if selected.is_symlink():
        raise UpdateError("The selected update package cannot be a symbolic link.")
    source = selected.resolve()
    if source.is_file() and source.name.lower().endswith(ARCHIVE_SUFFIXES):
        members = _archive_images(source)
        return UpdatePackage(source, source.stem, {}, members, _version_from_name(source.name))
    if not source.is_dir():
        raise UpdateError("Update package does not exist or is not a directory/archive: " + str(source))
    found = _find_images_directory(source)
    if found is None:
        raise UpdateError("Could not find official flash_jibo/output/images payloads in " + str(source))
    images_dir, paths = found
    for filename, image in paths.items():
        if image.stat().st_size <= 0:
            raise UpdateError("Update image is empty: " + str(image))
    return UpdatePackage(source, source.name, paths, {}, _version_from_name(source.name))


def discover_packages(folder: str | os.PathLike[str]) -> list[UpdatePackage]:
    """Return valid package directories and supported archives in one folder."""

    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        return []
    packages = []
    for candidate in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if candidate.is_symlink():
            continue
        if candidate.is_dir() or (candidate.is_file() and candidate.name.lower().endswith(ARCHIVE_SUFFIXES)):
            try:
                packages.append(validate_package(candidate))
            except UpdateError:
                continue
    return packages


def parse_alt_capacities(dfu_list_output: str) -> dict[str, int]:
    """Read byte capacities from ``dfu-util -l`` output where exposed.

    DFU listings that name an alternative but do not report its byte size do
    not provide enough evidence for a whole-partition image write. Such entries
    are intentionally omitted; callers must refuse the flash unless every
    selected target is present in the returned mapping.
    """

    capacities: dict[str, int] = {}
    for line in dfu_list_output.splitlines():
        name_match = re.search(r'\bname="([^"\r\n]+)"', line)
        size_match = re.search(r"\bsize\s*=\s*(\d+)\b", line)
        if not name_match or not size_match:
            continue
        name = name_match.group(1)
        size = int(size_match.group(1))
        if size <= 0:
            raise UpdateError("DFU reported a non-positive capacity for " + name + ".")
        if name in capacities and capacities[name] != size:
            raise UpdateError("DFU reported conflicting capacities for " + name + ".")
        capacities[name] = size
    return capacities


def parse_gpt_layout_prefix(data: bytes, sector_size: int = 512) -> dict[str, dict[str, int]]:
    """Parse partition extents from a structurally valid primary-GPT prefix.

    The GPT header is at LBA 1 and the 128-byte partition entries start at LBA
    2. A full 32KiB prefix covers the observed 128-entry table. Both the header
    CRC and complete partition-entry array CRC are required. The result maps
    each partition name to its first/last LBA and byte capacity.
    """

    if sector_size < 512 or len(data) < sector_size + 92:
        raise UpdateError("GPT prefix is too short to contain a primary header.")
    header = data[sector_size:]
    if header[:8] != b"EFI PART":
        raise UpdateError("The eMMC prefix does not contain a primary GPT header at LBA 1.")
    header_size = struct.unpack_from("<I", header, 12)[0]
    if header_size < 92 or header_size > sector_size or len(header) < header_size:
        raise UpdateError("The primary GPT header has an invalid size.")
    stored_header_crc = struct.unpack_from("<I", header, 16)[0]
    header_copy = bytearray(header[:header_size])
    header_copy[16:20] = b"\x00\x00\x00\x00"
    if zlib.crc32(header_copy) & 0xFFFFFFFF != stored_header_crc:
        raise UpdateError("The primary GPT header CRC is invalid.")
    current_lba, _backup_lba, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    entries_lba, entry_count, entry_size, entries_crc = struct.unpack_from("<QIII", header, 72)
    if current_lba != 1 or entries_lba < 2:
        raise UpdateError("The GPT header does not describe the expected primary table.")
    if first_usable > last_usable or entry_count < 6 or entry_count > 128:
        raise UpdateError("The GPT header has an invalid usable range or partition count.")
    if entry_size != 128:
        raise UpdateError("The GPT partition entry size is not the supported 128 bytes.")
    table_offset = entries_lba * sector_size
    available = max(0, len(data) - table_offset)
    available_entries = min(entry_count, available // entry_size)
    if available_entries != entry_count:
        raise UpdateError("GPT prefix does not contain the complete partition-entry table.")
    table_bytes = data[table_offset:table_offset + entry_count * entry_size]
    if (zlib.crc32(table_bytes) & 0xFFFFFFFF) != entries_crc:
        raise UpdateError("The GPT partition-entry array CRC is invalid.")

    layout: dict[str, dict[str, int]] = {}
    extents: list[tuple[int, int, str]] = []
    for index in range(available_entries):
        offset = table_offset + index * entry_size
        entry = data[offset:offset + entry_size]
        if len(entry) != entry_size:
            break
        if entry[:16] == b"\x00" * 16:
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        if first_lba > last_lba or first_lba < first_usable or last_lba > last_usable:
            raise UpdateError("GPT entry " + str(index + 1) + " has an invalid LBA extent.")
        try:
            name = entry[56:128].decode("utf-16le").split("\x00", 1)[0]
        except UnicodeDecodeError as exc:
            raise UpdateError("GPT entry " + str(index + 1) + " has an invalid partition name.") from exc
        if not name:
            raise UpdateError("GPT entry " + str(index + 1) + " has no partition name.")
        if name in layout:
            raise UpdateError("GPT contains duplicate partition name " + name + ".")
        capacity = (last_lba - first_lba + 1) * sector_size
        layout[name] = {"first_lba": first_lba, "last_lba": last_lba,
                        "size_bytes": capacity}
        extents.append((first_lba, last_lba, name))
    extents.sort()
    for before, after in zip(extents, extents[1:]):
        if after[0] <= before[1]:
            raise UpdateError("GPT partition extents overlap: " + before[2] + " and " + after[2] + ".")

    return layout


def parse_gpt_prefix(data: bytes, sector_size: int = 512) -> dict[str, int]:
    """Parse and profile-check Jibo partition byte capacities.

    Update callers use this stricter wrapper so a structurally valid but
    unexpected GPT cannot be used to size an update image.
    """
    layout = parse_gpt_layout_prefix(data, sector_size)
    capacities = {name: partition["size_bytes"] for name, partition in layout.items()}
    required = {"rootfsA", "rootfsB", "services", "var", "skills"}
    missing = sorted(required - set(capacities))
    if missing:
        raise UpdateError("GPT prefix is missing Jibo partition names: " + ", ".join(missing) + ".")
    for name, expected in KNOWN_CAPACITIES.items():
        if capacities[name] != expected:
            raise UpdateError("GPT reports " + str(capacities[name]) + " bytes for " + name +
                              "; this package profile expects " + str(expected) + ".")
    return capacities


def _required_capacities(capacities: Mapping[str, int], preserve_var: bool) -> dict[str, int]:
    required = {"rootfsA", "rootfsB", "services", "skills"}
    if not preserve_var:
        required.add("var")
    missing = sorted(name for name in required if name not in capacities)
    if missing:
        raise UpdateError("DFU did not report byte capacities for: " + ", ".join(missing) + ".")
    result: dict[str, int] = {}
    for name in required:
        try:
            value = int(capacities[name])
        except (TypeError, ValueError) as exc:
            raise UpdateError("Invalid DFU capacity for " + name + ".") from exc
        if value <= 0:
            raise UpdateError("DFU reported an invalid capacity for " + name + ".")
        expected = KNOWN_CAPACITIES.get(name)
        if expected is not None and value != expected:
            raise UpdateError("DFU reports " + str(value) + " bytes for " + name +
                              "; this package profile expects " + str(expected) + ".")
        result[name] = value
    if result["rootfsA"] != result["rootfsB"]:
        raise UpdateError("rootfsA and rootfsB capacities differ; this package is not compatible with this layout.")
    return result


def _run_checked(argv: list[str], label: str) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise UpdateError("Could not run " + argv[0] + ": " + str(exc)) from exc
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        if len(output) > 2000:
            output = "…\n" + output[-2000:]
        raise UpdateError(label + " failed (exit " + str(result.returncode) + "): " + output)
    return result.stdout + result.stderr


def _filesystem_geometry(image: Path) -> tuple[int, int]:
    output = _run_checked(["dumpe2fs", "-h", str(image)], "Reading ext4 geometry")
    block_size_match = re.search(r"^Block size:\s*(\d+)\s*$", output, re.MULTILINE)
    block_count_match = re.search(r"^Block count:\s*(\d+)\s*$", output, re.MULTILINE)
    if not block_size_match or not block_count_match:
        raise UpdateError("Could not read ext4 block size and count from " + str(image))
    block_size = int(block_size_match.group(1))
    block_count = int(block_count_match.group(1))
    if block_size < 1024 or block_size > 65536 or block_size & (block_size - 1):
        raise UpdateError("Unsupported ext4 block size in " + str(image))
    if block_count <= 0 or block_count * block_size > image.stat().st_size:
        raise UpdateError("Ext4 filesystem geometry exceeds the image file: " + str(image))
    return block_size, block_count


def _restore_ext4_write_time(source: Path, output: Path) -> None:
    """Remove resize2fs's wall-clock stamp from classic 1 KiB ext4 images.

    Jibo's stock images have no metadata_csum feature. Their primary and backup
    superblocks receive a new s_wtime on every resize, making otherwise equal
    partition images hash differently across flash attempts. Preserve the
    source image's timestamp in each real superblock. Other ext4 layouts are
    left to their filesystem tools, which know how to update their checksums.
    """
    with source.open("rb") as original, output.open("r+b") as prepared:
        original.seek(1024)
        old = original.read(1024)
        prepared.seek(1024)
        new = prepared.read(1024)
        if len(old) != 1024 or len(new) != 1024 or old[56:58] != b"\x53\xef" or \
                new[56:58] != b"\x53\xef":
            raise UpdateError("The prepared ext4 superblock is missing or invalid.")
        if struct.unpack_from("<I", new, 24)[0] != 0 or \
                struct.unpack_from("<I", new, 100)[0] & 0x400:
            return
        block_size = 1024
        if struct.unpack_from("<I", old, 24)[0] != struct.unpack_from("<I", new, 24)[0]:
            raise UpdateError("The prepared ext4 block size changed unexpectedly.")
        blocks_per_group = struct.unpack_from("<I", new, 32)[0]
        block_count = struct.unpack_from("<I", new, 4)[0]
        if not blocks_per_group or not block_count:
            raise UpdateError("The prepared ext4 group geometry is invalid.")
        groups = (block_count + blocks_per_group - 1) // blocks_per_group
        for group in range(groups):
            offset = group * blocks_per_group * block_size + 1024
            if offset + 1024 > output.stat().st_size:
                raise UpdateError("An ext4 backup superblock extends beyond the prepared image.")
            prepared.seek(offset)
            header = prepared.read(128)
            if header[56:58] != b"\x53\xef":
                if group == 0:
                    raise UpdateError("The prepared ext4 primary superblock is missing.")
                continue
            if struct.unpack_from("<H", header, 90)[0] != group % 65536:
                raise UpdateError("An ext4 backup superblock has the wrong group number.")
            prepared.seek(offset + 48)
            prepared.write(old[48:52])
        prepared.flush()
        os.fsync(prepared.fileno())


def equivalent_except_ext4_write_time(expected: Path, actual: Path) -> bool:
    """Compare complete classic ext4 images, permitting only s_wtime changes.

    Older flash attempts used resize2fs's wall-clock timestamp. A full USB
    readback can be reused when every other byte matches the newly prepared
    image and its original full-image hash still matches the old update record.
    """
    expected, actual = Path(expected), Path(actual)
    if expected.stat().st_size != actual.stat().st_size:
        return False
    with expected.open("rb") as reference, actual.open("rb") as observed:
        reference.seek(1024)
        header = reference.read(1024)
        observed.seek(1024)
        live_header = observed.read(1024)
        if len(header) != 1024 or header[56:58] != b"\x53\xef" or \
                live_header[56:58] != b"\x53\xef" or \
                struct.unpack_from("<I", header, 24)[0] != 0 or \
                struct.unpack_from("<I", header, 100)[0] & 0x400:
            return False
        blocks_per_group = struct.unpack_from("<I", header, 32)[0]
        block_count = struct.unpack_from("<I", header, 4)[0]
        if not blocks_per_group or not block_count or \
                block_count * 1024 > expected.stat().st_size:
            return False
        allowed = []
        for group in range((block_count + blocks_per_group - 1) // blocks_per_group):
            offset = group * blocks_per_group * 1024 + 1024
            if offset + 128 > expected.stat().st_size:
                return False
            reference.seek(offset)
            stamp = reference.read(128)
            observed.seek(offset)
            live_stamp = observed.read(128)
            if stamp[56:58] != b"\x53\xef":
                if group == 0 or live_stamp[56:58] == b"\x53\xef":
                    return False
                continue
            if live_stamp[56:58] != b"\x53\xef" or \
                    struct.unpack_from("<H", stamp, 90)[0] != group % 65536 or \
                    struct.unpack_from("<H", live_stamp, 90)[0] != group % 65536:
                return False
            allowed.append(offset + 48)
        reference.seek(0)
        observed.seek(0)
        position = 0
        next_stamp = 0
        while True:
            left = reference.read(1024 * 1024)
            right = observed.read(1024 * 1024)
            if not left:
                return not right
            if len(left) != len(right):
                return False
            if left != right:
                adjusted = bytearray(right)
                while next_stamp < len(allowed) and allowed[next_stamp] < position + len(left):
                    offset = allowed[next_stamp] - position
                    adjusted[offset:offset + 4] = left[offset:offset + 4]
                    next_stamp += 1
                if left != adjusted:
                    return False
            else:
                while next_stamp < len(allowed) and allowed[next_stamp] < position + len(left):
                    next_stamp += 1
            position += len(left)


def _copy_sparse(source: Path, destination: Path) -> None:
    """Copy an image while preserving holes where SEEK_DATA/SEEK_HOLE exist."""

    source_size = source.stat().st_size
    with source.open("rb") as src, destination.open("xb") as dst:
        dst.truncate(source_size)
        if not (hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")):
            dst.seek(0)
            shutil.copyfileobj(src, dst, 1024 * 1024)
            return
        try:
            cursor = 0
            while cursor < source_size:
                try:
                    data = os.lseek(src.fileno(), cursor, os.SEEK_DATA)
                except OSError as exc:
                    if exc.errno == errno.ENXIO:
                        break
                    raise
                hole = min(os.lseek(src.fileno(), data, os.SEEK_HOLE), source_size)
                src.seek(data)
                dst.seek(data)
                remaining = hole - data
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise UpdateError("Unexpected end of update image: " + str(source))
                    dst.write(chunk)
                    remaining -= len(chunk)
                cursor = hole
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            dst.truncate(0)
            src.seek(0)
            dst.seek(0)
            shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def _extract_archive_images(package: UpdatePackage, destination: Path) -> dict[str, Path]:
    extracted: dict[str, Path] = {}
    try:
        # Stream through compressed tar once. Random extraction from a .bz2
        # tar repeatedly decompresses earlier members and can take minutes.
        requested = {member_name: filename for filename, member_name in package.archive_members.items()}
        with tarfile.open(package.source, mode="r|*") as archive:
            for member in archive:
                filename = requested.get(member.name)
                if filename is None:
                    continue
                if filename in extracted or not member.isfile() or member.size <= 0:
                    raise UpdateError("Archive image changed since validation: " + member.name)
                # Write to fixed basenames in our own temporary folder. No path
                # from the archive is used as a destination path.
                target = destination / filename
                stream = archive.extractfile(member)
                if stream is None:
                    raise UpdateError("Could not read image from update archive: " + filename)
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                if target.stat().st_size != member.size:
                    raise UpdateError("Extracted update image size changed: " + filename)
                extracted[filename] = target
                if len(extracted) == len(requested):
                    break
        if len(extracted) != len(requested):
            raise UpdateError("Archive images changed since validation; select the package again.")
    except UpdateError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise UpdateError("Could not extract selected update images: " + str(exc)) from exc
    return extracted


@contextmanager
def _package_images(package: UpdatePackage) -> Iterator[Mapping[str, Path]]:
    if not package.is_archive:
        yield package.images
        return
    with tempfile.TemporaryDirectory(prefix="jibo-update-package-") as temporary:
        yield _extract_archive_images(package, Path(temporary))


def prepare_images(
    package: UpdatePackage | str | os.PathLike[str],
    preserve_var: bool,
    capacities: Mapping[str, int],
    workdir: str | os.PathLike[str],
) -> dict[str, Path]:
    """Create exact-capacity ext4 images for a later DFU writer.

    The returned mapping is keyed by DFU alternate name. ``rootfs.ext4`` is
    prepared once and mapped to both rootfsA and rootfsB.  ``var`` is omitted
    entirely when ``preserve_var`` is true.  All writes occur under ``workdir``;
    the original package files are read-only inputs and never modified.
    """

    if not isinstance(package, UpdatePackage):
        package = validate_package(package)
    targets = _required_capacities(capacities, preserve_var)
    selected_destination = Path(workdir).expanduser()
    if selected_destination.is_symlink():
        raise UpdateError("Prepared image work directory cannot be a symbolic link.")
    destination = selected_destination.resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise UpdateError("Prepared image work directory must be a real directory.")

    selected_filenames = ["rootfs.ext4", "services.ext4", "skills.ext4"]
    if not preserve_var:
        selected_filenames.append("var.ext4")
    with _package_images(package) as image_paths:
        missing = [filename for filename in selected_filenames if filename not in image_paths]
        if missing:
            raise UpdateError("The package cannot perform this flash; missing: " + ", ".join(missing) + ".")
        prepared: dict[str, Path] = {}
        file_targets = {
            "rootfs.ext4": targets["rootfsA"],
            "services.ext4": targets["services"],
            "skills.ext4": targets["skills"],
        }
        if not preserve_var:
            file_targets["var.ext4"] = targets["var"]
        created: list[Path] = []
        try:
            for filename in selected_filenames:
                source = Path(image_paths[filename])
                if source.is_symlink() or not source.is_file():
                    raise UpdateError("Update image is missing or is a symbolic link: " + filename)
                capacity = file_targets[filename]
                source_size = source.stat().st_size
                if source_size <= 0 or source_size > capacity:
                    raise UpdateError(filename + " is " + str(source_size) + " bytes; target partition has " +
                                      str(capacity) + " bytes.")
                # Validate the official image before making any derived copies.
                _run_checked(["e2fsck", "-f", "-n", str(source)], "Read-only ext4 check for " + filename)
                block_size, block_count = _filesystem_geometry(source)
                # GPT partitions are sector-aligned, while ext4 grows in whole
                # filesystem blocks. The final skills partition can contain a
                # trailing 512-byte sector after its last 1024-byte ext4 block.
                target_blocks = capacity // block_size
                if block_count > target_blocks:
                    raise UpdateError(filename + " filesystem is already larger than its target partition.")
                output = destination / filename
                if output.exists() or output.is_symlink():
                    raise UpdateError("Prepared image already exists; use an empty work directory: " + str(output))
                created.append(output)
                _copy_sparse(source, output)
                with output.open("r+b") as stream:
                    stream.truncate(capacity)
                    stream.flush()
                    os.fsync(stream.fileno())
                _run_checked(["resize2fs", "-f", str(output), str(target_blocks)],
                             "Offline ext4 expansion for " + filename)
                # resize2fs may shorten the file to whole ext4 blocks. Restore
                # the exact DFU partition length, leaving any partial block as
                # zero padding outside the filesystem.
                with output.open("r+b") as stream:
                    stream.truncate(capacity)
                    stream.flush()
                    os.fsync(stream.fileno())
                _restore_ext4_write_time(source, output)
                if output.stat().st_size != capacity:
                    raise UpdateError("Prepared image has an unexpected file size: " + filename)
                final_block_size, final_block_count = _filesystem_geometry(output)
                if final_block_size != block_size or final_block_count != target_blocks:
                    raise UpdateError("Offline ext4 expansion did not fill the exact target partition: " + filename)
                _run_checked(["e2fsck", "-f", "-n", str(output)], "Read-only check for prepared " + filename)
                prepared[filename] = output
            result = {"rootfsA": prepared["rootfs.ext4"],
                      "rootfsB": prepared["rootfs.ext4"],
                      "services": prepared["services.ext4"],
                      "skills": prepared["skills.ext4"]}
            if not preserve_var:
                result["var"] = prepared["var.ext4"]
            return result
        except BaseException:
            for output in created:
                output.unlink(missing_ok=True)
            raise

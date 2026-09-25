#!/usr/bin/env python3
"""Guided terminal menu for Jibo recovery, var backups, and safe image edits."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time

import jibo_dfu_bounded as bounded
import jibo_images as images
import jibo_updates as updates


ROOT = Path(__file__).resolve().parent
RCM = ("0955", "7740")
DFU = ("0955", "701a")
MARKER = "jibo-dfu-v1"
EXPECTED_VAR_SIZE = 524_288_000
WRITE_CONFIRMATION = "WRITE VAR"
UPDATE_CONFIRMATION = "FLASH UPDATE"
UPDATE_ORDER = ("rootfsA", "rootfsB", "services", "skills", "var")
SKILLS_SECTOR_SIZE = 512
SKILLS_CHUNK_SECTORS = 0x200000
SKILLS_CHUNK_BYTES = SKILLS_SECTOR_SIZE * SKILLS_CHUNK_SECTORS
DEFAULT_LOADER = ROOT / "loader.bin"


def _invoking_user():
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
            gid = int(os.environ.get("SUDO_GID", "-1"))
            return uid, gid, Path(pwd.getpwuid(uid).pw_dir)
        except (ValueError, KeyError, OSError):
            pass
    return None


def _chown_to_invoking_user(path):
    owner = _invoking_user()
    if owner:
        os.chown(path, owner[0], owner[1])


_OWNER = _invoking_user()
BACKUP_ROOT = (_OWNER[2] if _OWNER else Path.home()) / "Jibo-Backups"


class DfuError(Exception):
    pass


def devices(root=Path("/sys/bus/usb/devices")):
    found = []
    for path in sorted(root.glob("*")):
        try:
            pair = tuple((path / name).read_text().strip().lower()
                         for name in ("idVendor", "idProduct"))
        except OSError:
            continue
        if pair in (RCM, DFU):
            found.append({"port": path.name, "state": "rcm" if pair == RCM else "dfu"})
    return found


def select_device(found, port=None):
    selected = [device for device in found if port is None or device["port"] == port]
    if len(selected) > 1:
        raise DfuError("Multiple robots connected; select one using --port.")
    return selected[0] if selected else None


def run(argv, timeout=30):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env=_runtime_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DfuError(str(exc)) from exc
    if result.returncode:
        detail = re.sub(r'(serial=")[^"]*(")', r"\1[redacted]\2", result.stdout + result.stderr)
        raise DfuError("Command failed: " + argv[0] + "\n" + detail)
    return result.stdout + result.stderr


def _runtime_env():
    env = os.environ.copy()
    if (ROOT / "lib").is_dir():
        env["LD_LIBRARY_PATH"] = str(ROOT / "lib") + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def _transfer_output_size(path):
    """Track a completed output or its private, in-progress temporary sibling."""
    path = Path(path)
    try:
        return path.stat().st_size
    except OSError:
        pass
    try:
        return max((candidate.stat().st_size for candidate in
                    path.parent.glob(path.name + ".tmp.*") if candidate.is_file()),
                   default=0)
    except OSError:
        return 0


def _byte_progress_from_log(log):
    """Read the latest `Staged N / M bytes` status without moving the writer offset."""
    try:
        size = os.fstat(log.fileno()).st_size
        if size <= 0:
            return None
        start = max(0, size - 512)
        tail = os.pread(log.fileno(), size - start, start)
    except (AttributeError, OSError, ValueError):
        return None
    matches = re.findall(rb"Staged\s+(\d+)\s*/\s*(\d+)\s+bytes", tail)
    if not matches:
        return None
    transferred, total = (int(value) for value in matches[-1])
    if total <= 0:
        return None
    return min(transferred, total), total


def run_with_progress(argv, timeout, label, cwd=None, progress_path=None, progress_size=None,
                      byte_progress=False):
    """Run a quiet transfer with a terminal spinner and retain output for errors."""
    started = time.monotonic()
    terminal = sys.stderr
    interactive = terminal.isatty()
    if not interactive:
        print(label + "...", file=terminal, flush=True)
    elif progress_path is not None and progress_size:
        print(label, file=terminal, flush=True)

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, env=_runtime_env(), cwd=cwd)
            timed_out = False
            spinner = "|/-\\"
            frames = 0
            last_status_length = 0
            last_report = started
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        process.kill()
                        process.wait()
                        timed_out = True
                        break
                    byte_transfer = _byte_progress_from_log(log) if byte_progress else None
                    if byte_transfer is not None:
                        transferred, total = byte_transfer
                        filled = int(20 * transferred / total)
                        status = "[{}{}] {:5.1f}% | {:,}/{:,} KiB | {:0.0f}s".format(
                            "#" * filled, "-" * (20 - filled),
                            100 * transferred / total,
                            transferred // 1024, total // 1024, elapsed)
                    elif progress_path is not None and progress_size:
                        transferred = min(_transfer_output_size(progress_path), progress_size)
                        mib = 1024 * 1024
                        filled = int(20 * transferred / progress_size)
                        status = "[{}{}] {:5.1f}% | {:.1f}/{:.1f} MiB | {:.2f} MiB/s | {:0.0f}s".format(
                            "#" * filled, "-" * (20 - filled),
                            100 * transferred / progress_size,
                            transferred / mib, progress_size / mib,
                            transferred / mib / max(elapsed, 0.001), elapsed)
                    else:
                        status = "{} {} {:0.0f}s".format(label, spinner[frames % len(spinner)], elapsed)
                    if interactive:
                        terminal.write("\r" + status)
                        terminal.flush()
                        last_status_length = len(status)
                        frames += 1
                    elif (progress_path is not None or byte_transfer is not None) and \
                            elapsed - (last_report - started) >= 5:
                        print(status, file=terminal, flush=True)
                        last_report = time.monotonic()
                    time.sleep(0.15)
            except KeyboardInterrupt as exc:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if interactive:
                    terminal.write("\r" + " " * last_status_length + "\r")
                    terminal.flush()
                raise DfuError("The partition read was interrupted; the partial dump was discarded.") from exc

            elapsed = time.monotonic() - started
            if interactive:
                terminal.write("\r" + " " * last_status_length + "\r")
            result_code = process.returncode
            log.seek(0)
            output = log.read()

    except OSError as exc:
        raise DfuError("Could not start transfer command: " + str(exc)) from exc

    successful = result_code == 0 and not timed_out
    if successful:
        completion = label.replace("Reading", "Read", 1)
        print("{} in {:.1f}s.".format(completion, elapsed), file=terminal, flush=True)
    elif not interactive:
        print("Transfer stopped after {:.1f}s.".format(elapsed), file=terminal, flush=True)
    else:
        terminal.flush()

    if timed_out:
        raise DfuError("Command timed out after {} seconds: {}\n{}".format(
            timeout, argv[0], _transfer_error_detail(output)))
    if result_code:
        raise DfuError("Command failed: {}\n{}".format(argv[0], _transfer_error_detail(output)))
    return output


def _call_with_progress(callback, label):
    """Show elapsed time while a local package check or image prep runs."""
    started = time.monotonic()
    terminal = sys.stderr
    interactive = terminal.isatty()
    if not interactive:
        print(label + "...", file=terminal, flush=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(callback)
        frames = 0
        spinner = "|/-\\"
        last_length = 0
        while not future.done():
            if interactive:
                status = "{} {} {:0.0f}s".format(label, spinner[frames % 4], time.monotonic() - started)
                terminal.write("\r" + status)
                terminal.flush()
                last_length = len(status)
                frames += 1
            time.sleep(0.15)
        if interactive:
            terminal.write("\r" + " " * last_length + "\r")
            terminal.flush()
        result = future.result()
    print("{} completed in {:.1f}s.".format(label, time.monotonic() - started),
          file=terminal, flush=True)
    return result


def _transfer_error_detail(output):
    detail = re.sub(r'(serial=")[^"]*(")', r"\1[redacted]\2", output).strip()
    detail = re.sub(r'(Chip ID:\s*)[^\r\n]*',
                    r"\1[redacted]", detail, flags=re.IGNORECASE)
    if len(detail) > 2000:
        detail = "…\n" + detail[-2000:]
    return detail


def tool(name, override=None):
    candidate = override or str(ROOT / "tools" / name)
    if Path(candidate).is_file():
        return str(Path(candidate).resolve())
    if override:
        raise DfuError("Tool does not exist: " + override)
    located = shutil.which(name)
    if not located:
        raise DfuError("Missing " + name + "; install it or provide --" + name + ".")
    return located


def _shofel_dfu_tool(override=None):
    """Find a matched ShofEL host and launch-enabled RAM payload."""
    candidate = override or str(ROOT / "tools" / "shofel2_t124")
    if not Path(candidate).is_file():
        if override:
            raise DfuError("ShofEL host tool does not exist: " + str(override))
        candidate = shutil.which("shofel2_t124")
    if not candidate:
        raise DfuError("Missing shofel2_t124; install it or provide --shofel.")
    executable = str(Path(candidate).resolve())
    if not os.access(executable, os.X_OK):
        raise DfuError("ShofEL host tool is not executable: " + executable)
    intermezzo = Path(executable).parent / "intermezzo.bin"
    if not intermezzo.is_file() or intermezzo.stat().st_size == 0:
        raise DfuError("Missing non-empty intermezzo.bin next to " + executable)
    payload = Path(executable).parent / "dfu_stage2.bin"
    if not payload.is_file() or payload.stat().st_size == 0:
        raise DfuError("Missing non-empty dfu_stage2.bin next to " + executable)
    capability = run([executable, "--dfu-stage-capability"], timeout=5).strip()
    if capability != "dfu-stage-launch=1":
        raise DfuError("This ShofEL host cannot start the RAM DFU loader. Build and package the launch-enabled host and matching stage payload.")
    return executable


def shofel_dfu_available():
    """Return whether the local ShofEL pair can start the RAM DFU loader."""
    try:
        _shofel_dfu_tool()
        return DEFAULT_LOADER.is_file()
    except (DfuError, OSError):
        return False


def enter_shofel_dfu(port=None, shofel=None, loader=None, dfu_util=None,
                     timeout=120, confirm_meerkat_rev02=False):
    """Start the pinned RAM loader through ShofEL, then confirm DFU on the same port."""
    if not confirm_meerkat_rev02:
        raise DfuError("Confirm the Meerkat Rev02 SDRAM profile before entering DFU through ShofEL.")
    if not 0 < timeout <= 600:
        raise DfuError("Timeout must be between 0 and 600 seconds.")
    executable = _shofel_dfu_tool(shofel)
    image = Path(loader) if loader is not None else DEFAULT_LOADER
    if image.is_symlink() or not image.is_file():
        raise DfuError("Missing or symlinked RAM DFU loader image: " + str(image))
    image = image.resolve()
    selected = select_device(devices(), port)
    if selected is None or selected["state"] != "rcm":
        raise DfuError("Connect the robot in RCM/APX before entering DFU through ShofEL.")
    port = selected["port"]
    dfu_util = tool("dfu-util", dfu_util)
    output = run_with_progress(
        [executable, "--usb-port-path", port, "DFU_STAGE", str(image),
         "--confirm-meerkat-rev02", "--launch"],
        timeout=180, label="Loading the RAM DFU program", cwd=str(Path(executable).parent),
        byte_progress=True)
    if "Starting verified ARM-state SPL at 0x80108000." not in output:
        raise DfuError("ShofEL did not confirm the loader launch.")

    print("Waiting for DFU on USB port " + port + "…", flush=True)
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        current = select_device(devices(), port)
        if current and current["state"] == "dfu":
            try:
                names, _ = dfu_alternatives(dfu_util, port)
            except DfuError as exc:
                last_error = str(exc)
            else:
                if MARKER not in names or "var" not in names:
                    missing = [name for name in (MARKER, "var") if name not in names]
                    raise DfuError("DFU appeared, but the RAM loader is missing: " + ", ".join(missing))
                return {"port": port, "state": "dfu", "already_running": False,
                        "loader_verified": True, "alternatives": names,
                        "entry_transport": "ShofEL"}
        time.sleep(0.25)
    detail = " Last DFU listing error: " + last_error if last_error else ""
    raise DfuError("The RAM loader started, but DFU did not become ready on USB port " +
                   port + ". Check USB forwarding and the robot's connection." + detail)


def dfu_alternatives(executable, port):
    output = run([executable, "-d", "0955:701a", "--path", port, "-l"])
    names = re.findall(r'name="([^"]+)"', output)
    return names, output


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_operation_dir(out=None, prefix="var-backup"):
    if out is not None:
        directory = Path(out).expanduser().resolve()
        if directory.exists():
            raise DfuError("Output directory already exists; choose a new directory: " + str(directory))
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
    else:
        BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        BACKUP_ROOT.chmod(0o700)
        _chown_to_invoking_user(BACKUP_ROOT)
        directory = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=BACKUP_ROOT))
    directory.chmod(0o700)
    _chown_to_invoking_user(directory)
    return directory


def _private_write(path, content):
    path = Path(path)
    path.write_text(json.dumps(content, indent=2) + "\n")
    path.chmod(0o600)
    _chown_to_invoking_user(path)


def _sha256_file(path):
    return images.sha256_file(path)


def _dfu_context(port, dfu_util, include_output=False):
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect the robot by USB first.")
    if selected["state"] != "dfu":
        raise DfuError("The robot is in RCM/APX. Enter DFU first, then run this command again.")
    names, output = dfu_alternatives(dfu_util, selected["port"])
    if MARKER not in names:
        raise DfuError("This DFU device does not show the Jibo recovery marker. No partition operation was attempted.")
    if "var" not in names:
        raise DfuError("This recovery profile does not expose a var partition.")
    reported = None
    for line in output.splitlines():
        if re.search(r'name="var"', line):
            match = re.search(r"\bsize\s*=\s*(\d+)", line)
            if match:
                reported = int(match.group(1))
                break
    if reported is not None and reported != EXPECTED_VAR_SIZE:
        raise DfuError("The device reports a var size of " + str(reported) +
                       " bytes; this candidate profile expects 524288000. No transfer was attempted.")
    serial = re.search(r'serial="([^"]*)"', output)
    if serial and serial.group(1).strip():
        device_tag = "serial-sha256:" + hashlib.sha256(serial.group(1).strip().encode("utf-8")).hexdigest()
    else:
        device_tag = "usb-port:" + selected["port"]
    context = (selected["port"], names, device_tag)
    return context + (output,) if include_output else context


def _upload_var(dfu_util, port, destination):
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    try:
        run_with_progress(
            [dfu_util, "-d", "0955:701a", "--path", port, "-a", "var", "-U", str(destination)],
            timeout=900, label="Reading the 500 MiB var partition from USB",
            progress_path=destination, progress_size=EXPECTED_VAR_SIZE)
        if not destination.is_file():
            raise DfuError("dfu-util completed without creating the var image.")
        destination.chmod(0o600)
        _chown_to_invoking_user(destination)
        size = destination.stat().st_size
        if size != EXPECTED_VAR_SIZE:
            raise DfuError("The uploaded var image is " + str(size) + " bytes; expected 524288000.")
        return _sha256_file(destination)
    except (DfuError, OSError):
        destination.unlink(missing_ok=True)
        raise


def _upload_partition(dfu_util, port, partition, size, destination):
    """Read exactly one named DFU partition into a private local file."""
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    try:
        run_with_progress(
            [dfu_util, "-d", "0955:701a", "--path", port, "-a", partition,
             "-U", str(destination), "-Z", str(size)],
            timeout=14400, label="Reading {} ({} bytes) from USB".format(partition, size),
            progress_path=destination, progress_size=size)
        if destination.stat().st_size != size:
            raise DfuError("Uploaded {} has {} bytes; expected {}.".format(
                partition, destination.stat().st_size, size))
        destination.chmod(0o600)
        _chown_to_invoking_user(destination)
        return _sha256_file(destination)
    except (DfuError, OSError):
        destination.unlink(missing_ok=True)
        raise


def _find_partition_backup(device_tag, partition, size):
    if not BACKUP_ROOT.is_dir():
        return None
    for path in sorted(BACKUP_ROOT.glob("partition-backup-*/backup-manifest.json")):
        try:
            if path.is_symlink():
                continue
            record = json.loads(path.read_text())
            if (record.get("kind") != "jibo-partition-backup" or
                    record.get("device_tag") != device_tag or
                    record.get("partition") != partition or record.get("size_bytes") != size):
                continue
            image = path.parent / "partition.img"
            if image.is_symlink() or not image.is_file() or image.stat().st_size != size:
                continue
            digest = _sha256_file(image)
            if digest == record.get("sha256"):
                return {"image": str(image), "sha256": digest, "manifest": str(path),
                        "status": "existing verified backup reused"}
        except (OSError, ValueError, TypeError):
            continue
    return None


def _backup_partition_once(dfu_util, port, device_tag, partition, size):
    existing = _find_partition_backup(device_tag, partition, size)
    if existing:
        return existing
    if shutil.disk_usage(BACKUP_ROOT.parent).free < size + 1024 ** 3:
        raise DfuError("Not enough free space to save the original {} partition.".format(partition))
    directory = _new_operation_dir(prefix="partition-backup")
    image = directory / "partition.img"
    try:
        digest = _upload_partition(dfu_util, port, partition, size, image)
        manifest = directory / "backup-manifest.json"
        _private_write(manifest, {"schema": 1, "kind": "jibo-partition-backup",
                                  "created_utc": _utc_now(), "device_tag": device_tag,
                                  "usb_port": port, "partition": partition,
                                  "size_bytes": size, "sha256": digest, "image": image.name})
        return {"image": str(image), "sha256": digest, "manifest": str(manifest),
                "status": "backup complete"}
    except (DfuError, OSError):
        image.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def _read_gpt_capacities(dfu_util, port, names):
    if MARKER not in names:
        raise DfuError("This DFU loader does not expose the read-only GPT marker.")
    try:
        return updates.parse_gpt_prefix(bounded.read_dfu_alt_prefix(port))
    except (bounded.BoundedDfuError, updates.UpdateError) as exc:
        raise DfuError("Could not verify the robot's GPT partition sizes: " + str(exc)) from exc


def probe_dfu_gpt(port=None, dfu_util=None):
    """Validate the live GPT through DFU without saving a partition backup."""
    dfu_util = dfu_util or tool("dfu-util")
    port, names, _ = _dfu_context(port, dfu_util)
    capacities = _read_gpt_capacities(dfu_util, port, names)
    required = ("rootfsA", "rootfsB", "services", "skills", "var")
    missing = [name for name in required if name not in capacities]
    if missing:
        raise DfuError("The live GPT is missing Jibo partitions: " + ", ".join(missing))
    if capacities["var"] != EXPECTED_VAR_SIZE:
        raise DfuError("The live GPT reports an unexpected var size: " + str(capacities["var"]))
    return {"status": "partition table read and checked", "port": port,
            "partition_sizes_bytes": {name: capacities[name] for name in required},
            "bytes_read": bounded.MARKER_BYTES, "backup_created": False}


def _expected_skills_chunks(capacity):
    """Return the exact byte ranges for the loader's GPT-bounded skills alternatives."""
    try:
        capacity = int(capacity)
    except (TypeError, ValueError) as exc:
        raise DfuError("The GPT skills capacity is invalid.") from exc
    if capacity <= 0 or capacity % SKILLS_SECTOR_SIZE:
        raise DfuError("The GPT skills capacity is not a positive whole number of sectors.")
    count = (capacity + SKILLS_CHUNK_BYTES - 1) // SKILLS_CHUNK_BYTES
    chunks = []
    for index in range(count):
        offset = index * SKILLS_CHUNK_BYTES
        chunks.append({"name": "skills-{:03d}".format(index),
                       "offset_bytes": offset,
                       "size_bytes": min(SKILLS_CHUNK_BYTES, capacity - offset)})
    return chunks


def _dfu_alt_sizes(output):
    sizes = {}
    for line in output.splitlines():
        name = re.search(r'\bname\s*=\s*"([^"]+)"', line)
        size = re.search(r"\bsize\s*=\s*(\d+)", line)
        if name and size:
            sizes[name.group(1)] = int(size.group(1))
    return sizes


def _validate_skills_chunk_alternatives(capacity, names, output):
    """Require a complete, GPT-sized skills chunk map before an update can start."""
    found = [name for name in names if name.startswith("skills-")]
    if not found:
        return None
    if "skills" in names:
        raise DfuError("The DFU loader lists both a full skills alternative and skills chunks.")
    expected = _expected_skills_chunks(capacity)
    expected_names = [chunk["name"] for chunk in expected]
    if len(found) != len(set(found)) or set(found) != set(expected_names):
        missing = sorted(set(expected_names) - set(found))
        unexpected = sorted(set(found) - set(expected_names))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise DfuError("The skills chunk alternatives do not match the GPT skills capacity: " +
                       "; ".join(details) + ".")
    # dfu-util's standard -l output does not report raw-alternative capacities.
    # The loader binds each writable alias to an exact GPT slice; when a build
    # does report sizes, use them as an additional consistency check.
    sizes = _dfu_alt_sizes(output)
    for chunk in expected:
        reported = sizes.get(chunk["name"])
        if reported is not None and reported != chunk["size_bytes"]:
            raise DfuError("The DFU loader reports {} bytes for {}; GPT requires {} bytes.".format(
                reported, chunk["name"], chunk["size_bytes"]))
    return expected


def _copy_exact(source, destination, size, digest=None):
    """Copy exactly size bytes from source to a binary destination and update digest."""
    remaining = size
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        if not block:
            raise DfuError("A partition image ended before its expected byte range.")
        destination.write(block)
        if digest is not None:
            digest.update(block)
        remaining -= len(block)


def _copy_file_slice(source_path, offset, size, destination_path):
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise DfuError("Prepared skills image is missing or is a symbolic link.")
    if offset < 0 or size <= 0 or offset + size > source_path.stat().st_size:
        raise DfuError("A skills chunk range falls outside the prepared image.")
    digest = hashlib.sha256()
    try:
        with source_path.open("rb") as source, destination_path.open("wb") as destination:
            source.seek(offset)
            _copy_exact(source, destination, size, digest)
            destination.flush()
            os.fsync(destination.fileno())
        if destination_path.stat().st_size != size:
            raise DfuError("A prepared skills chunk has an unexpected byte length.")
    except (DfuError, OSError):
        destination_path.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _upload_skills_partition(dfu_util, port, capacity, chunks, destination=None,
                             workdir=None):
    """Read bounded skills alternatives and optionally assemble one partition image."""
    destination = Path(destination) if destination is not None else None
    if destination is not None:
        destination.unlink(missing_ok=True)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = Path(workdir or (destination.parent if destination else tempfile.gettempdir()))
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    whole_hash = hashlib.sha256()
    chunk_records = []
    try:
        output_stream = destination.open("w+b") if destination is not None else None
        try:
            if output_stream is not None:
                output_stream.truncate(capacity)
            with tempfile.TemporaryDirectory(prefix="skills-chunk-", dir=workdir) as temporary:
                chunk_path = Path(temporary) / "partition.img"
                for index, chunk in enumerate(chunks, 1):
                    chunk_hash = _upload_partition(dfu_util, port, chunk["name"],
                                                   chunk["size_bytes"], chunk_path)
                    with chunk_path.open("rb") as source:
                        if output_stream is not None:
                            output_stream.seek(chunk["offset_bytes"])
                            _copy_exact(source, output_stream, chunk["size_bytes"], whole_hash)
                        else:
                            _copy_exact(source, _NullWriter(), chunk["size_bytes"], whole_hash)
                    chunk_records.append({"alternative": chunk["name"],
                                          "offset_bytes": chunk["offset_bytes"],
                                          "size_bytes": chunk["size_bytes"],
                                          "sha256": chunk_hash,
                                          "status": "read"})
                    print("Read skills chunk {}/{} ({}).".format(index, len(chunks), chunk["name"]))
            if output_stream is not None:
                output_stream.flush()
                os.fsync(output_stream.fileno())
        finally:
            if output_stream is not None:
                output_stream.close()
        if destination is not None:
            if destination.stat().st_size != capacity:
                raise DfuError("The assembled skills backup has an unexpected byte length.")
            destination.chmod(0o600)
            _chown_to_invoking_user(destination)
        return {"sha256": whole_hash.hexdigest(), "chunks": chunk_records}
    except (DfuError, OSError):
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise


class _NullWriter:
    def write(self, data):
        return len(data)


def _backup_skills_partition_once(dfu_util, port, device_tag, capacity, chunks):
    existing = _find_partition_backup(device_tag, "skills", capacity)
    if existing:
        return existing
    if shutil.disk_usage(BACKUP_ROOT.parent).free < capacity + SKILLS_CHUNK_BYTES:
        raise DfuError("Not enough free space to save the original skills partition.")
    directory = _new_operation_dir(prefix="partition-backup")
    image = directory / "partition.img"
    try:
        uploaded = _upload_skills_partition(dfu_util, port, capacity, chunks,
                                            destination=image, workdir=directory)
        manifest = directory / "backup-manifest.json"
        _private_write(manifest, {"schema": 1, "kind": "jibo-partition-backup",
                                  "created_utc": _utc_now(), "device_tag": device_tag,
                                  "usb_port": port, "partition": "skills",
                                  "size_bytes": capacity, "sha256": uploaded["sha256"],
                                  "image": image.name, "chunks": uploaded["chunks"]})
        return {"image": str(image), "sha256": uploaded["sha256"],
                "manifest": str(manifest), "status": "backup complete"}
    except (DfuError, OSError):
        image.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


def _write_skills_chunks(dfu_util, port, candidate, capacity, chunks,
                         operation_directory, record_path, record, write_entry):
    candidate = Path(candidate)
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size != capacity:
        raise DfuError("The prepared skills image does not match the GPT partition size.")
    candidate_hash = _sha256_file(candidate)
    readback_hash = hashlib.sha256()
    write_entry["chunks"] = []
    write_entry["status"] = "writing bounded skills chunks"
    _private_write(record_path, record)
    try:
        with tempfile.TemporaryDirectory(prefix="skills-write-", dir=operation_directory) as temporary:
            temporary = Path(temporary)
            for index, chunk in enumerate(chunks, 1):
                candidate_piece = temporary / "candidate.img"
                expected_hash = _copy_file_slice(candidate, chunk["offset_bytes"],
                                                 chunk["size_bytes"], candidate_piece)
                chunk_record = {"alternative": chunk["name"],
                                "offset_bytes": chunk["offset_bytes"],
                                "size_bytes": chunk["size_bytes"],
                                "candidate_sha256": expected_hash,
                                "status": "write started"}
                write_entry["chunks"].append(chunk_record)
                _private_write(record_path, record)
                run_with_progress(
                    [dfu_util, "-d", "0955:701a", "--path", port,
                     "-a", chunk["name"], "-D", str(candidate_piece)],
                    timeout=14400,
                    label="Writing skills chunk {}/{} ({})".format(
                        index, len(chunks), chunk["name"]))
                readback = temporary / "readback.img"
                actual_hash = _upload_partition(dfu_util, port, chunk["name"],
                                                chunk["size_bytes"], readback)
                chunk_record["readback_sha256"] = actual_hash
                if actual_hash != expected_hash:
                    chunk_record["status"] = "readback mismatch"
                    _private_write(record_path, record)
                    raise DfuError("{} did not match its readback. DFU was left active; use the saved backups.".format(
                        chunk["name"]))
                with readback.open("rb") as stream:
                    _copy_exact(stream, _NullWriter(), chunk["size_bytes"], readback_hash)
                chunk_record["status"] = "verified"
                _private_write(record_path, record)
        if readback_hash.hexdigest() != candidate_hash:
            raise DfuError("The assembled skills readback did not match the prepared image.")
        write_entry["readback_sha256"] = readback_hash.hexdigest()
        write_entry["status"] = "verified"
        _private_write(record_path, record)
        return candidate_hash
    except (DfuError, OSError):
        write_entry["status"] = "failed"
        _private_write(record_path, record)
        raise


def _update_candidates(folder):
    """List local choices quickly; full archive validation follows selection."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold())
            if not path.is_symlink() and
            (path.is_dir() or (path.is_file() and path.name.lower().endswith(updates.ARCHIVE_SUFFIXES)))]


def _backup_manifest(directory, port, image_path, digest, operations=None, device_tag=None,
                     transport="USB DFU upload", usb_state="DFU (0955:701a)",
                     profile="Jibo RAM DFU loader"):
    record = {"schema": 1, "kind": "jibo-var-backup", "created_utc": _utc_now(),
              "partition": "var", "size_bytes": EXPECTED_VAR_SIZE, "sha256": digest,
              "profile": profile, "transport": transport, "usb_state": usb_state,
              "usb_port": port, "device_tag": device_tag or "usb-port:" + port,
              "image": Path(image_path).name,
              "operations": operations or ["read var partition"]}
    _private_write(Path(directory) / "backup-manifest.json", record)
    return record


def _find_verified_backup(device_tag=None):
    if not BACKUP_ROOT.is_dir():
        return None
    manifests = sorted(BACKUP_ROOT.rglob("backup-manifest.json"),
                       key=lambda path: path.stat().st_mtime if path.exists() else 0,
                       reverse=True)
    for manifest_path in manifests:
        if manifest_path.is_symlink():
            continue
        try:
            record = json.loads(manifest_path.read_text())
            if record.get("kind") != "jibo-var-backup" or record.get("status") == "failed":
                continue
            if device_tag is not None and record.get("device_tag") != device_tag:
                continue
            filename = Path(record.get("image", "var.img")).name
            image_path = manifest_path.parent / filename
            if image_path.is_symlink() or not image_path.is_file() or image_path.stat().st_size != EXPECTED_VAR_SIZE:
                continue
            digest = _sha256_file(image_path)
            if digest != record.get("sha256"):
                continue
            return {"image": image_path, "sha256": digest,
                    "created_utc": record.get("created_utc", "unknown"),
                    "manifest": manifest_path}
        except (OSError, ValueError, TypeError):
            continue
    return None


def _prepare_current_and_baseline(dfu_util, port, device_tag, directory):
    current = Path(directory) / "current-var.img"
    current_hash = _upload_var(dfu_util, port, current)
    baseline = _find_verified_backup(device_tag)
    if baseline:
        return {"before": current, "before_sha256": current_hash,
                "baseline": baseline["image"], "baseline_sha256": baseline["sha256"],
                "baseline_matches_current": current_hash == baseline["sha256"]}

    baseline_directory = _new_operation_dir(prefix="var-backup")
    baseline_image = baseline_directory / "var.img"
    try:
        shutil.move(str(current), str(baseline_image))
        baseline_image.chmod(0o600)
        _backup_manifest(baseline_directory, port, baseline_image, current_hash,
                         ["initial automatic backup before first var write"], device_tag)
    except (OSError, DfuError) as exc:
        raise DfuError("Could not preserve the initial var backup in " + str(baseline_directory) + ": " + str(exc)) from exc
    return {"before": baseline_image, "before_sha256": current_hash,
            "baseline": baseline_image, "baseline_sha256": current_hash,
            "baseline_matches_current": True}


def _remove_if_temporary(path, directory, protected=()):
    path = Path(path).resolve()
    if path.parent == Path(directory).resolve() and path not in {Path(item).resolve() for item in protected}:
        path.unlink(missing_ok=True)


def _record_prewrite_failure(directory, operation, error, state=None):
    """Keep a small record of a stopped operation without retaining another image."""
    directory = Path(directory)
    record_path = directory / "write-manifest.json"
    if record_path.exists():
        return record_path
    record = {"schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
              "partition": "var", "status": "failed before write", "write_started": False,
              "operation": operation, "error": str(error),
              "usb_state": "DFU (0955:701a)", "operation_directory": str(directory)}
    if state:
        record.update(baseline_backup=str(state["baseline"]),
                      baseline_sha256=state["baseline_sha256"],
                      current_sha256=state["before_sha256"])
    _private_write(record_path, record)
    return record_path


def _raise_live_operation_error(directory, operation, state, error):
    manifest = Path(directory) / "write-manifest.json"
    try:
        status = json.loads(manifest.read_text()).get("status") if manifest.is_file() else None
    except (OSError, ValueError):
        status = "unknown"
    write_started = status in ("write started", "failed", "verification failed")
    if write_started:
        raise DfuError(str(error) + "\nBackup and operation files are preserved in " + str(directory)) from error

    if state:
        _remove_if_temporary(state["before"], directory, (state["baseline"],))
        _remove_if_temporary(Path(directory) / "edited-var.img", directory, (state["baseline"],))
    record_path = _record_prewrite_failure(directory, operation, error, state)
    details = str(error) + "\nNo partition write was attempted."
    if state:
        details += " The saved rollback backup remains at " + str(state["baseline"]) + "."
    details += " Operation record: " + str(record_path)
    raise DfuError(details) from error


def backup_var(port=None, dfu_util=None, out=None, refresh=False):
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    existing = _find_verified_backup(device_tag) if out is None else None
    if existing and not refresh:
        return {"status": "existing verified backup reused", "image": str(existing["image"]),
                "sha256": existing["sha256"], "manifest": str(existing["manifest"]),
                "created_utc": existing["created_utc"],
                "message": "Use --refresh to capture the currently connected var state."}
    directory = _new_operation_dir(out, "var-backup")
    image_path = directory / "var.img"
    try:
        digest = _upload_var(dfu_util, port, image_path)
        record = _backup_manifest(directory, port, image_path, digest, device_tag=device_tag)
    except DfuError as exc:
        image_path.unlink(missing_ok=True)
        _private_write(directory / "backup-manifest.json", {
            "schema": 1, "kind": "jibo-var-backup", "created_utc": _utc_now(),
            "partition": "var", "status": "failed", "usb_state": "DFU (0955:701a)",
            "usb_port": port, "error": str(exc), "operation_directory": str(directory)})
        raise
    if out is None:
        if existing and existing["sha256"] == digest:
            image_path.unlink(missing_ok=True)
            (directory / "backup-manifest.json").unlink(missing_ok=True)
            directory.rmdir()
            return {"status": "existing verified backup reused", "image": str(existing["image"]),
                    "sha256": digest, "manifest": str(existing["manifest"]),
                    "created_utc": existing["created_utc"]}
    return {"status": "backup complete", "image": str(image_path), "size_bytes": EXPECTED_VAR_SIZE,
            "sha256": digest, "manifest": str(directory / "backup-manifest.json"),
            "operation_directory": str(directory), "profile": record["profile"]}


def _confirm_write(supplied=None, plan=None):
    if callable(supplied):
        try:
            return bool(supplied(plan))
        except Exception as exc:
            raise DfuError("The write confirmation screen could not be completed: " + str(exc)) from exc
    if supplied is not None:
        if supplied != WRITE_CONFIRMATION:
            raise DfuError("For a write, --confirm must be exactly 'WRITE VAR'.")
        return True
    prompt = "Type WRITE VAR to write the edited var partition: "
    try:
        if sys.stdin.isatty():
            response = input(prompt)
        else:
            with open("/dev/tty", "r+", encoding="utf-8") as terminal:
                terminal.write(prompt)
                terminal.flush()
                response = terminal.readline().rstrip("\r\n")
    except OSError as exc:
        raise DfuError("A typed confirmation is required. Re-run with --confirm 'WRITE VAR'.") from exc
    if response != WRITE_CONFIRMATION:
        return False
    return True


def _write_candidate(candidate, before, directory, port, dfu_util, confirmation=None,
                     operation="write var", baseline=None, before_hash=None,
                     baseline_hash=None):
    candidate = Path(candidate).expanduser().resolve()
    before = Path(before).expanduser().resolve()
    candidate_hash = _sha256_file(candidate)
    before_hash = before_hash or _sha256_file(before)
    baseline = Path(baseline or before).resolve()
    baseline_hash = baseline_hash or _sha256_file(baseline)
    record = {"schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
              "partition": "var", "size_bytes": EXPECTED_VAR_SIZE,
              "before_sha256": before_hash, "candidate_sha256": candidate_hash,
              "baseline_backup": str(baseline), "baseline_sha256": baseline_hash,
              "transport": "USB DFU", "usb_state": "DFU (0955:701a)", "usb_port": port,
              "operation": operation, "status": "prepared", "reset_after_write": False}
    record_path = Path(directory) / "write-manifest.json"
    if candidate.stat().st_size != EXPECTED_VAR_SIZE or before.stat().st_size != EXPECTED_VAR_SIZE:
        raise DfuError("Edited var image must be exactly 524288000 bytes.")
    if before_hash == candidate_hash:
        record.update(status="unchanged", readback_sha256=before_hash)
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        return {"status": "unchanged", "message": "The edited image matches the current var partition; no write was needed.",
                "backup": str(baseline), "operation_directory": str(directory), "sha256": candidate_hash}
    print("\nWrite plan: var partition, 524288000 bytes, USB port " + port)
    print("Change: " + operation)
    print("Original rollback backup: " + str(baseline))
    if not record["baseline_sha256"] == before_hash:
        print("The current var state differs from the original backup; no additional persistent backup will be made.")
    print("Current var SHA-256: " + before_hash)
    print("Edited image: " + str(candidate))
    print("Edited image SHA-256: " + candidate_hash)
    print("A successful write will be read back and compared. The robot will not be reset.")
    try:
        confirmed = _confirm_write(confirmation, record)
    except DfuError:
        record["status"] = "confirmation rejected"
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        raise
    if not confirmed:
        record["status"] = "cancelled"
        _private_write(record_path, record)
        _remove_if_temporary(before, directory, (baseline,))
        _remove_if_temporary(candidate, directory, (baseline,))
        return {"status": "cancelled", "message": "The robot was not changed.",
                "backup": str(baseline), "operation_directory": str(directory)}
    record["status"] = "write started"
    _private_write(record_path, record)
    try:
        run_with_progress(
            [dfu_util, "-d", "0955:701a", "--path", port, "-a", "var", "-D", str(candidate)],
            timeout=900, label="Writing the edited 500 MiB var partition over USB")
        readback = Path(directory) / "readback-var.img"
        readback_hash = _upload_var(dfu_util, port, readback)
        record["readback_sha256"] = readback_hash
        record["status"] = "verified" if readback_hash == candidate_hash else "verification failed"
        _private_write(record_path, record)
        if readback_hash != candidate_hash:
            raise DfuError("Var write readback did not match the edited image. The robot was not reset. "
                           "Backup and readback are preserved in " + str(directory))
    except DfuError as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _private_write(record_path, record)
        raise DfuError(str(exc) + "\nBackup and operation files are preserved in " + str(directory)) from exc
    _remove_if_temporary(readback, directory, (baseline,))
    _remove_if_temporary(before, directory, (baseline,))
    _remove_if_temporary(candidate, directory, (baseline,))
    return {"status": "verified", "message": "Var was written and read back successfully. The robot was left in DFU; it was not reset.",
            "backup": str(baseline), "readback_sha256": candidate_hash,
            "operation_directory": str(directory), "sha256": candidate_hash}


def write_var(image, port=None, dfu_util=None, out=None, confirmation=None):
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise DfuError("Edited var image does not exist: " + str(image))
    if image.stat().st_size != EXPECTED_VAR_SIZE:
        raise DfuError("Edited var image must be exactly 524288000 bytes.")
    images.inspect_var(image)
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "write-var")
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
    except DfuError as exc:
        (directory / "current-var.img").unlink(missing_ok=True)
        _private_write(directory / "write-manifest.json", {
            "schema": 1, "kind": "jibo-var-write", "created_utc": _utc_now(),
            "status": "failed before write", "usb_state": "DFU (0955:701a)",
            "usb_port": port, "error": str(exc), "operation_directory": str(directory)})
        raise
    return _write_candidate(image, state["before"], directory, port, dfu_util, confirmation,
                            baseline=state["baseline"], before_hash=state["before_sha256"],
                            baseline_hash=state["baseline_sha256"])


def flash_update(package_path, preserve_var, port=None, dfu_util=None, out=None,
                 confirmation=None, dry_run=False):
    """Install an official full-flash package using named DFU alternatives."""
    dfu_util = dfu_util or tool("dfu-util")
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect the robot by USB first.")
    if selected["state"] == "rcm":
        raise DfuError("The robot is in RCM/APX. Enter DFU with ShofEL before installing an update.")
    package = _call_with_progress(lambda: updates.validate_package(package_path),
                                  "Checking the selected update package")
    port, names, device_tag, alt_output = _dfu_context(
        selected["port"], dfu_util, include_output=True)
    partitions = [name for name in UPDATE_ORDER if name != "var" or not preserve_var]
    skills_aliases = [name for name in names if name.startswith("skills-")]
    if "skills" not in names and not skills_aliases:
        raise DfuError("This DFU loader does not expose the skills partition or its bounded skills-### alternatives.")
    missing = [name for name in partitions if name != "skills" and name not in names]
    if missing:
        raise DfuError("This DFU profile does not expose the required partitions: " + ", ".join(missing))
    capacities = _read_gpt_capacities(dfu_util, port, names)
    skills_chunks = _validate_skills_chunk_alternatives(
        capacities["skills"], names, alt_output)
    plan = {"package": str(package.source), "version": package.version,
            "usb_port": port, "var_policy": "preserve current configuration" if preserve_var else
            "replace with package var image (fresh setup and lost local settings)",
            "partitions": [{"name": name, "bytes": capacities[name]} for name in partitions],
            "skills_transfer": ("{} GPT-bounded DFU chunks".format(len(skills_chunks))
                                if skills_chunks else "single named DFU alternative"),
            "status": "plan only" if dry_run else "awaiting confirmation"}
    if dry_run:
        return plan
    print("\nOfficial full-flash update plan:")
    print(json.dumps(plan, indent=2))
    print("This writes the listed partitions, verifies each by a full USB readback, then requests a reset.")
    print("A preserved var keeps its current mode, identity, network settings, and first-boot resize marker.")
    print("A fresh var replaces those settings with the package image; a rollback backup is saved first.")
    if callable(confirmation):
        if not confirmation(plan):
            return {**plan, "status": "cancelled"}
    elif confirmation is None:
        if not _ask_confirmation("Flash this package?", UPDATE_CONFIRMATION):
            return {**plan, "status": "cancelled"}
    elif confirmation != UPDATE_CONFIRMATION:
        raise DfuError("For an update write, --confirm must be exactly 'FLASH UPDATE'.")

    directory = _new_operation_dir(out, "flash-update")
    record_path = directory / "update-manifest.json"
    record = {"schema": 1, "kind": "jibo-full-flash-update", "created_utc": _utc_now(),
              **plan, "status": "preparing", "backups": {}, "writes": []}
    _private_write(record_path, record)
    try:
        with tempfile.TemporaryDirectory(prefix="prepared-", dir=directory) as prepared_dir:
            prepared = _call_with_progress(
                lambda: updates.prepare_images(package, preserve_var, capacities, prepared_dir),
                "Preparing partition images on this computer")
            record["status"] = "backing up original partitions"
            _private_write(record_path, record)
            # Even a preserve-var update gets one reusable rollback image of var.
            var_backup = backup_var(port, dfu_util)
            record["backups"]["var"] = var_backup
            _private_write(record_path, record)
            for name in partitions:
                if name == "var":
                    continue
                if name == "skills" and skills_chunks:
                    record["backups"][name] = _backup_skills_partition_once(
                        dfu_util, port, device_tag, capacities[name], skills_chunks)
                else:
                    record["backups"][name] = _backup_partition_once(
                        dfu_util, port, device_tag, name, capacities[name])
                _private_write(record_path, record)
            for name in partitions:
                candidate = prepared[name]
                digest = _sha256_file(candidate)
                entry = {"partition": name, "candidate_sha256": digest,
                         "size_bytes": capacities[name], "status": "write started"}
                record["writes"].append(entry)
                record["status"] = "writing"
                _private_write(record_path, record)
                if name == "skills" and skills_chunks:
                    actual = _write_skills_chunks(
                        dfu_util, port, candidate, capacities[name], skills_chunks,
                        directory, record_path, record, entry)
                else:
                    run_with_progress(
                        [dfu_util, "-d", "0955:701a", "--path", port, "-a", name,
                         "-D", str(candidate)], timeout=14400,
                        label="Writing {} ({} bytes)".format(name, capacities[name]))
                    with tempfile.TemporaryDirectory(prefix="readback-", dir=directory) as readback_dir:
                        readback = Path(readback_dir) / "partition.img"
                        actual = _upload_partition(dfu_util, port, name, capacities[name], readback)
                entry["readback_sha256"] = actual
                if actual != digest:
                    entry["status"] = "verification failed"
                    _private_write(record_path, record)
                    raise DfuError("{} did not match its readback. DFU was left active; use the saved backups.".format(name))
                entry["status"] = "verified"
                _private_write(record_path, record)
        record["status"] = "verified; reset pending"
        _private_write(record_path, record)
        try:
            run([dfu_util, "-d", "0955:701a", "--path", port, "-e", "-R"], timeout=60)
            record["status"] = "verified; reset requested"
        except DfuError as exc:
            record["status"] = "verified; reset not confirmed"
            record["reset_error"] = str(exc)
        _private_write(record_path, record)
        return {"status": record["status"], "package": str(package.source),
                "var_policy": plan["var_policy"], "verified_partitions": partitions,
                "manifest": str(record_path), "backups": record["backups"]}
    except (DfuError, updates.UpdateError, OSError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        _private_write(record_path, record)
        raise DfuError(str(exc) + "\nUpdate record: " + str(record_path)) from exc


def set_mode_live(mode, port=None, dfu_util=None, out=None, confirmation=None):
    if mode not in images.MODE_VALUES:
        raise DfuError("Mode must be one of: " + ", ".join(images.MODE_VALUES))
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "set-mode")
    state = None
    candidate = directory / "edited-var.img"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        before = state["before"]
        edit = images.edit_mode(before, candidate, mode)
        if edit.get("journal_replayed_on_temporary_copy"):
            print("Replayed the ext4 journal on the temporary working image before editing. The saved backup was not changed.")
        print("Mode change: " + edit["previous_mode"] + " → " + mode)
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, "set mode to " + mode,
                                  state["baseline"], state["before_sha256"], state["baseline_sha256"])
        result.update(current_mode=edit["previous_mode"], new_mode=mode,
                      journal_replayed_on_temporary_copy=edit.get("journal_replayed_on_temporary_copy", False))
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, "set mode to " + mode, state, exc)


def _password_from_args(args):
    if getattr(args, "open_network", False):
        return None
    if getattr(args, "password_stdin", False):
        value = sys.stdin.readline()
        if value.endswith("\n"):
            value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
        return value
    return getpass.getpass("Wi-Fi password (hidden): ")


def configure_wifi_live(ssid, password=None, open_network=False, port=None,
                        dfu_util=None, out=None, confirmation=None):
    dfu_util = dfu_util or tool("dfu-util")
    port, _, device_tag = _dfu_context(port, dfu_util)
    directory = _new_operation_dir(out, "configure-wifi")
    state = None
    candidate = directory / "edited-var.img"
    try:
        state = _prepare_current_and_baseline(dfu_util, port, device_tag, directory)
        before = state["before"]
        edit = images.edit_wifi(before, candidate, ssid, password, open_network)
        print("Planned Wi-Fi change: add SSID " + ssid + "; existing networks are preserved.")
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, "add Wi-Fi network",
                                  state["baseline"], state["before_sha256"], state["baseline_sha256"])
        result["wifi_network_count"] = edit["network_count"]
        return result
    except (DfuError, images.ImageError) as exc:
        _raise_live_operation_error(directory, "add Wi-Fi network", state, exc)
    finally:
        password = None


def _default_edit_path(image):
    image = Path(image).expanduser().resolve()
    return image.with_name(image.stem + "-edited" + (image.suffix or ".img"))


def _display_devices():
    found = devices()
    if not found:
        print("No Jibo recovery USB device detected.")
        print("Connect the robot by USB and enter recovery mode, or check USB forwarding.")
        return found
    for device in found:
        if device["state"] == "rcm":
            explanation = "RCM/APX recovery; no partition access yet"
        else:
            explanation = "DFU; partition access depends on the recovery profile"
        print("USB port {port}: {state} — {explanation}".format(explanation=explanation, **device))
    return found


def _ask_confirmation(prompt, phrase=WRITE_CONFIRMATION):
    print(prompt)
    return input("Type " + phrase + " to continue: ").strip() == phrase


def _add_device_arguments(parser):
    parser.add_argument("--port", help="Linux USB topology path, for example 1-2")


def _add_dfu_argument(parser):
    parser.add_argument("--dfu-util", help="Path to dfu-util; defaults to bundled tool or PATH")


def _add_operation_argument(parser):
    parser.add_argument("--operation-dir", help="New private directory for backup and operation files")


def _add_confirmation_argument(parser):
    parser.add_argument("--confirm", help="Type WRITE VAR to authorize a partition write")


def _launch_menu():
    import jibo_tui
    result = jibo_tui.run(sys.modules[__name__])
    if result is None:
        print("The guided menu needs an interactive terminal. Use a CLI subcommand when running without one.",
              file=sys.stderr)
        return 2
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _launch_menu()
    parser = argparse.ArgumentParser(description=__doc__ + " Run without arguments to open the menu.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interactive", help="Open the guided text menu")
    sub.add_parser("detect", help="Show matching USB devices as JSON")
    sub.add_parser("list", help="Show matching USB devices in plain language")
    shofel_entry = sub.add_parser("enter-dfu-shofel",
                                  help="Start the RAM DFU loader from RCM/APX through ShofEL")
    _add_device_arguments(shofel_entry)
    shofel_entry.add_argument("--shofel", help="Path to the launch-enabled ShofEL host and adjacent payloads")
    shofel_entry.add_argument("--loader", type=Path,
                              help="RAM DFU image (defaults to the packaged pinned loader)")
    _add_dfu_argument(shofel_entry)
    shofel_entry.add_argument("--timeout", type=float, default=120,
                              help="Seconds to wait for DFU after the loader starts")
    shofel_entry.add_argument("--confirm-meerkat-rev02", action="store_true", required=True,
                              help="Confirm the Meerkat Rev02 SDRAM profile for this robot")
    gpt_probe = sub.add_parser("probe-dfu-gpt",
                               help="Read and validate the live GPT marker through DFU without saving a backup")
    _add_device_arguments(gpt_probe)
    _add_dfu_argument(gpt_probe)
    backup = sub.add_parser("backup-var", help="Save a private var image and SHA-256 manifest")
    _add_device_arguments(backup)
    _add_dfu_argument(backup)
    backup.add_argument("--out", type=Path, help="New output directory; otherwise ~/Jibo-Backups")
    backup.add_argument("--refresh", action="store_true", help="Capture the current var state instead of reusing the saved baseline")
    inspect = sub.add_parser("inspect-var", help="Inspect a local var image without displaying credentials")
    inspect.add_argument("image", type=Path)
    mode_edit = sub.add_parser("edit-mode", help="Create a new offline image with a changed Jibo mode")
    mode_edit.add_argument("image", type=Path)
    mode_edit.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    mode_edit.add_argument("--out", type=Path)
    wifi_edit = sub.add_parser("edit-wifi", help="Add a Wi-Fi network to a local var image")
    wifi_edit.add_argument("image", type=Path)
    wifi_edit.add_argument("--ssid", required=True)
    wifi_edit.add_argument("--open-network", action="store_true")
    wifi_edit.add_argument("--password-stdin", action="store_true", help="Read the protected network password from stdin")
    wifi_edit.add_argument("--out", type=Path)
    mode_live = sub.add_parser("set-mode", help="Back up, set one mode, write var, then verify by readback")
    mode_live.add_argument("--mode", required=True, choices=images.MODE_VALUES)
    _add_device_arguments(mode_live)
    _add_dfu_argument(mode_live)
    _add_operation_argument(mode_live)
    _add_confirmation_argument(mode_live)
    wifi_live = sub.add_parser("configure-wifi", help="Back up, add Wi-Fi, write var, then verify by readback")
    wifi_live.add_argument("--ssid", required=True)
    wifi_live.add_argument("--open-network", action="store_true")
    wifi_live.add_argument("--password-stdin", action="store_true", help="Read the protected network password from stdin")
    _add_device_arguments(wifi_live)
    _add_dfu_argument(wifi_live)
    _add_operation_argument(wifi_live)
    _add_confirmation_argument(wifi_live)
    write = sub.add_parser("write-var", help="Back up var, write an edited image, and verify its readback")
    write.add_argument("image", type=Path)
    _add_device_arguments(write)
    _add_dfu_argument(write)
    _add_operation_argument(write)
    _add_confirmation_argument(write)
    list_updates = sub.add_parser("list-updates", help="List official full-flash packages in an updates folder")
    list_updates.add_argument("--directory", type=Path, default=Path.cwd() / "updates")
    flash = sub.add_parser("flash-update", help="Back up, install, and verify an official full-flash package")
    flash.add_argument("package", type=Path, help="Extracted package directory or official .tar.bz2 archive")
    policy = flash.add_mutually_exclusive_group(required=True)
    policy.add_argument("--preserve-var", action="store_true", help="Keep current identity, mode, Wi-Fi, and user settings")
    policy.add_argument("--fresh-var", action="store_true", help="Replace var with the package image for fresh setup")
    _add_device_arguments(flash)
    _add_dfu_argument(flash)
    _add_operation_argument(flash)
    flash.add_argument("--dry-run", action="store_true", help="Verify package and live partition sizes without writing")
    flash.add_argument("--confirm", help="Exactly FLASH UPDATE, for scripted writes")
    args = parser.parse_args(argv)
    try:
        if args.command == "interactive":
            return _launch_menu()
        if args.command == "detect":
            result = devices()
        elif args.command == "list":
            _display_devices()
            return 0
        elif args.command == "enter-dfu-shofel":
            result = enter_shofel_dfu(args.port, args.shofel, args.loader,
                                      args.dfu_util, args.timeout,
                                      args.confirm_meerkat_rev02)
        elif args.command == "probe-dfu-gpt":
            result = probe_dfu_gpt(args.port, args.dfu_util)
        elif args.command == "backup-var":
            result = backup_var(args.port, tool("dfu-util", args.dfu_util), args.out, args.refresh)
        elif args.command == "inspect-var":
            result = images.inspect_var(args.image)
        elif args.command == "edit-mode":
            output = args.out or _default_edit_path(args.image)
            result = images.edit_mode(args.image, output, args.mode)
        elif args.command == "edit-wifi":
            password = _password_from_args(args)
            result = images.edit_wifi(args.image, args.out or _default_edit_path(args.image),
                                      args.ssid, password, args.open_network)
            password = None
        elif args.command == "set-mode":
            result = set_mode_live(args.mode, args.port, tool("dfu-util", args.dfu_util),
                                   args.operation_dir, args.confirm)
        elif args.command == "configure-wifi":
            password = _password_from_args(args)
            result = configure_wifi_live(args.ssid, password, args.open_network, args.port,
                                         tool("dfu-util", args.dfu_util), args.operation_dir, args.confirm)
            password = None
        elif args.command == "list-updates":
            result = [{"path": str(path), "name": path.name}
                      for path in _update_candidates(args.directory)]
        elif args.command == "flash-update":
            result = flash_update(args.package, args.preserve_var, args.port,
                                  tool("dfu-util", args.dfu_util), args.operation_dir,
                                  args.confirm, args.dry_run)
        else:
            result = write_var(args.image, args.port, tool("dfu-util", args.dfu_util),
                               args.operation_dir, args.confirm)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (DfuError, images.ImageError, updates.UpdateError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

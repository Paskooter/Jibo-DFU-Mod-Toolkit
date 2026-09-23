#!/usr/bin/env python3
"""Jibo recovery, backup, and var editing utility."""
import argparse
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

import jibo_images as images


ROOT = Path(__file__).resolve().parent
RCM = ("0955", "7740")
DFU = ("0955", "701a")
MARKER = "jibo-dfu-v1"
FILES = ("loader.bin", "rcm.bct", "rcm.qry", "rcm.ml", "rcm.bl")
EXPECTED_VAR_SIZE = 524_288_000
WRITE_CONFIRMATION = "WRITE VAR"


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


def load_bundle(directory):
    directory = Path(directory).resolve()
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["schema"] != 1 or manifest["entry"] != "jibo-ram-dfu-v1":
            raise DfuError("Unsupported loader manifest.")
        if manifest["persistent_writes_on_entry"] is not False:
            raise DfuError("This tool requires a loader that preserves persistent storage.")
        if manifest["soc"] != 124 or manifest["load_address"] != "0x80108000":
            raise DfuError("Unsupported RCM target or load address.")
        if set(manifest["files"]) != set(FILES):
            raise DfuError("Bundle must contain precisely the required five artifact records.")
        for name in FILES:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise DfuError("Missing or symlinked bundle artifact: " + name)
            data = path.read_bytes()
            record = manifest["files"][name]
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise DfuError("Bundle integrity check failed: " + name)
        if (directory / "rcm.bct").stat().st_size != 8192:
            raise DfuError("T124 BCT must be 8192 bytes.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DfuError("Invalid bundle: " + str(exc)) from exc
    return manifest


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


def run_with_progress(argv, timeout, label):
    """Run a quiet transfer with a terminal spinner and retain output for errors."""
    started = time.monotonic()
    terminal = sys.stderr
    interactive = terminal.isatty()
    if not interactive:
        print(label + "...", file=terminal, flush=True)

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, env=_runtime_env())
            timed_out = False
            spinner = "|/-\\"
            frames = 0
            last_status_length = 0
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        process.kill()
                        process.wait()
                        timed_out = True
                        break
                    if interactive:
                        status = "{} {} {:0.0f}s".format(label, spinner[frames % len(spinner)], elapsed)
                        terminal.write("\r" + status)
                        terminal.flush()
                        last_status_length = len(status)
                        frames += 1
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


def _transfer_error_detail(output):
    detail = re.sub(r'(serial=")[^"]*(")', r"\1[redacted]\2", output).strip()
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


def dfu_alternatives(executable, port):
    output = run([executable, "-d", "0955:701a", "--path", port, "-l"])
    names = re.findall(r'name="([^"]+)"', output)
    return names, output


def enter(bundle, port, tegrarcm, dfu_util, timeout=30, allow_untested=False):
    selected = select_device(devices(), port)
    if selected is None:
        raise DfuError("No Jibo RCM/DFU device detected. Connect USB and hold recovery while resetting the robot.")
    port = selected["port"]
    if selected["state"] == "dfu":
        names, _ = dfu_alternatives(dfu_util, port)
        if not names:
            raise DfuError("DFU device has no accessible alternatives; check USB permissions.")
        return {"port": port, "state": "dfu", "already_running": True,
                "loader_verified": MARKER in names, "alternatives": names}
    manifest = load_bundle(bundle)
    if not manifest.get("hardware_verified", False) and not allow_untested:
        raise DfuError("This bundle is an untested hardware candidate. Use --allow-untested for its first hardware test.")
    root = Path(bundle).resolve()
    argv = [tegrarcm, "--usb-port-path=" + port, "--download-signed-msgs",
            "--signed-msgs-file=" + str(root / "rcm"), "--bct=" + str(root / "rcm.bct"),
            "--bootloader=" + str(root / "loader.bin"), "--loadaddr=0x80108000",
            "--usb-timeout=5000"]
    run(argv, timeout=max(timeout, 30))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = select_device(devices(), port)
        if current and current["state"] == "dfu":
            names, _ = dfu_alternatives(dfu_util, port)
            if MARKER not in names:
                raise DfuError("USB entered DFU but the expected loader marker is absent; no partition transfer was attempted.")
            return {"port": port, "state": "dfu", "already_running": False,
                    "loader_verified": True, "alternatives": names}
        time.sleep(0.25)
    raise DfuError("RCM transfer completed but DFU did not appear on port " + port +
                   ". Check USB passthrough and reset into RCM before retrying.")


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


def _dfu_context(port, dfu_util):
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
    return selected["port"], names, device_tag


def _upload_var(dfu_util, port, destination):
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    try:
        run_with_progress(
            [dfu_util, "-d", "0955:701a", "--path", port, "-a", "var", "-U", str(destination)],
            timeout=900, label="Reading the 500 MiB var partition from USB")
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


def _backup_manifest(directory, port, image_path, digest, operations=None, device_tag=None):
    record = {"schema": 1, "kind": "jibo-var-backup", "created_utc": _utc_now(),
              "partition": "var", "size_bytes": EXPECTED_VAR_SIZE, "sha256": digest,
              "profile": "Jibo RAM DFU candidate; firmware revision unknown",
              "transport": "USB DFU upload", "usb_state": "DFU (0955:701a)",
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


def _confirm_write(supplied=None):
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
        confirmed = _confirm_write(confirmation)
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
        print("Mode change: " + edit["previous_mode"] + " → " + mode)
        result = _write_candidate(candidate, before, directory, port, dfu_util,
                                  confirmation, "set mode to " + mode,
                                  state["baseline"], state["before_sha256"], state["baseline_sha256"])
        result.update(current_mode=edit["previous_mode"], new_mode=mode)
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


def _read_password_interactively(open_network=False):
    if open_network:
        return None
    return getpass.getpass("Wi-Fi password (hidden): ")


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


def _interactive_inspect():
    path = input("Path to var backup image: ").strip()
    result = images.inspect_var(path)
    print("Mode: " + result["mode"])
    if result["wifi_configured"]:
        print("Wi-Fi config: present (" + str(result["wifi_network_count"]) + " saved network(s); details hidden)")
    else:
        print("Wi-Fi config: not present")


def _interactive_edit_mode():
    source = input("Path to var backup image: ").strip()
    print("normal: standard use; developer: selected development services;")
    print("int-developer: broader internal development mode; oobe: setup/onboarding.")
    print("Mode options: 1 normal, 2 developer, 3 int-developer, 4 oobe")
    choice = input("Choose a mode: ").strip()
    modes = {"1": "normal", "2": "developer", "3": "int-developer", "4": "oobe"}
    if choice not in modes:
        print("Cancelled.")
        return
    mode = modes[choice]
    result = images.edit_mode(source, _default_edit_path(source), mode)
    print("Created " + result["image"])
    print("Mode: " + result["previous_mode"] + " → " + mode)
    print("This only edits a local image. Use Write an edited var image to transfer it.")


def _interactive_edit_wifi():
    source = input("Path to var backup image: ").strip()
    ssid = input("Wi-Fi network name (SSID): ")
    kind = input("Network type: [1] protected WPA/WPA2  [2] open: ").strip()
    if kind not in ("1", "2"):
        print("Cancelled.")
        return
    open_network = kind == "2"
    password = _read_password_interactively(open_network)
    result = images.edit_wifi(source, _default_edit_path(source), ssid, password, open_network)
    print("Created " + result["image"])
    print("Added one Wi-Fi network. Password and network details were not displayed.")
    print("Existing saved networks were preserved.")
    password = None


def interactive():
    print("Jibo DFU Mod Toolkit")
    print("Backups and image edits stay on this computer. One rollback backup is reused for each robot; temporary dumps are cleaned up.")
    while True:
        print("\nCurrent USB state:")
        _display_devices()
        print("\n  1  Back up var (read only; reuses a saved backup when available)")
        print("  2  Inspect a var backup")
        print("  3  Set mode on a connected robot (backup, write, readback)")
        print("  4  Configure Wi-Fi on a connected robot (backup, write, readback)")
        print("  5  Edit a backup image offline")
        print("  6  Write an edited var image (backup, write, readback)")
        print("  7  Enter DFU recovery (untested hardware candidate)")
        print("  8  What is supported / still being built")
        print("  q  Quit")
        choice = input("Choose an option: ").strip().lower()
        try:
            if choice == "1":
                print(json.dumps(backup_var(), indent=2))
            elif choice == "2":
                _interactive_inspect()
            elif choice == "3":
                print("Developer modes expose more system access. Choose int-developer only if you need its broader behavior.")
                print("normal = standard use; developer = selected services; int-developer = broader internal mode; oobe = setup.")
                print("  1 normal  2 developer  3 int-developer  4 oobe")
                modes = {"1": "normal", "2": "developer", "3": "int-developer", "4": "oobe"}
                selected = input("Choose a mode: ").strip()
                if selected in modes:
                    result = set_mode_live(modes[selected], confirmation=None)
                    print(json.dumps(result, indent=2))
                else:
                    print("Cancelled.")
            elif choice == "4":
                ssid = input("Wi-Fi network name (SSID): ")
                kind = input("Network type: [1] protected WPA/WPA2  [2] open: ").strip()
                if kind not in ("1", "2"):
                    print("Cancelled.")
                    continue
                is_open = kind == "2"
                password = _read_password_interactively(is_open)
                result = configure_wifi_live(ssid, password, is_open)
                password = None
                print(json.dumps(result, indent=2))
            elif choice == "5":
                print("  1 change mode  2 add Wi-Fi network  3 inspect image")
                subchoice = input("Choose an offline edit: ").strip()
                if subchoice == "1":
                    _interactive_edit_mode()
                elif subchoice == "2":
                    _interactive_edit_wifi()
                elif subchoice == "3":
                    _interactive_inspect()
                else:
                    print("Cancelled.")
            elif choice == "6":
                path = input("Path to edited 500 MiB var image: ").strip()
                print("An existing rollback backup is reused, or one is created if needed. Temporary images are removed after a verified write.")
                print(json.dumps(write_var(path), indent=2))
            elif choice == "7":
                print("The current recovery bundle has not been tested on a real robot. RCM entry is also profile specific.")
                if not _ask_confirmation("Load this candidate into RAM and wait for DFU re-enumeration?", "ENTER RCM"):
                    print("Cancelled.")
                    continue
                device = select_device(devices())
                if device is None:
                    raise DfuError("No Jibo RCM/DFU device detected.")
                dfu_util = tool("dfu-util")
                tegrarcm = tool("tegrarcm") if device["state"] == "rcm" else "tegrarcm"
                result = enter(ROOT / "bundles" / "default", device["port"], tegrarcm,
                               dfu_util, allow_untested=True)
                print(json.dumps(result, indent=2))
            elif choice == "8":
                print("Available now: detect RCM/DFU, validate the local bundle, enter the RAM DFU candidate,")
                print("back up var, inspect mode/Wi-Fi state, edit mode/Wi-Fi offline, and write var with readback.")
                print("Still being built: version-gated SSH/firewall changes, full user-area eMMC backup,")
                print("ShofEL transport, automatic hardware profile selection, and support for more board populations.")
                print("This candidate has not been validated on a physical robot.")
            elif choice in ("q", "quit", "exit"):
                return 0
            else:
                print("Choose one of the listed numbers, or q to quit.")
        except (DfuError, images.ImageError, OSError, ValueError) as exc:
            print("\n" + str(exc), file=sys.stderr)
    return 0


def _add_device_arguments(parser):
    parser.add_argument("--port", help="Linux USB topology path, for example 1-2")


def _add_dfu_argument(parser):
    parser.add_argument("--dfu-util", help="Path to dfu-util; defaults to bundled tool or PATH")


def _add_operation_argument(parser):
    parser.add_argument("--operation-dir", help="New private directory for backup and operation files")


def _add_confirmation_argument(parser):
    parser.add_argument("--confirm", help="Type WRITE VAR to authorize a partition write")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return interactive()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interactive", help="Open the guided text menu")
    sub.add_parser("detect", help="Show matching USB devices as JSON")
    sub.add_parser("list", help="Show matching USB devices in plain language")
    check = sub.add_parser("verify-bundle", help="Check local recovery artifacts without touching USB")
    check.add_argument("bundle", type=Path)
    entry = sub.add_parser("enter", help="Load recovery into RAM and leave the robot in DFU")
    entry.add_argument("--bundle", type=Path, default=ROOT / "bundles" / "default")
    _add_device_arguments(entry)
    entry.add_argument("--tegrarcm")
    _add_dfu_argument(entry)
    entry.add_argument("--timeout", type=float, default=30)
    entry.add_argument("--allow-untested", action="store_true")
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
    args = parser.parse_args(argv)
    try:
        if args.command == "interactive":
            return interactive()
        if args.command == "detect":
            result = devices()
        elif args.command == "list":
            _display_devices()
            return 0
        elif args.command == "verify-bundle":
            result = load_bundle(args.bundle)
        elif args.command == "enter":
            if not 0 < args.timeout <= 600:
                raise DfuError("Timeout must be between 0 and 600 seconds.")
            device = select_device(devices(), args.port)
            rcm = tool("tegrarcm", args.tegrarcm) if device and device["state"] == "rcm" else "tegrarcm"
            result = enter(args.bundle, args.port, rcm, tool("dfu-util", args.dfu_util),
                           args.timeout, args.allow_untested)
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
        else:
            result = write_var(args.image, args.port, tool("dfu-util", args.dfu_util),
                               args.operation_dir, args.confirm)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (DfuError, images.ImageError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

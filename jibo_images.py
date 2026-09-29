#!/usr/bin/env python3
"""Offline, secret-conscious editing helpers for Jibo var images."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


MODE_VALUES = ("normal", "int-developer", "developer", "oobe")
VAR_MODE_PATH = "/jibo/mode.json"
VAR_WIFI_PATH = "/etc/wpa_supplicant.conf"
FACTORY_CAMERA_PATHS = (
    "/jibo/lps/CameraModelParamsL.json",
    "/jibo/lps/CameraModelParamsR.json",
    "/jibo/lps/InterCameraTransform.json",
)
FACTORY_IDENTITY_PATH = "/jibo/identity.json"


class ImageError(Exception):
    pass


def _chown_to_invoking_user(path):
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        try:
            uid = int(os.environ["SUDO_UID"])
            gid = int(os.environ.get("SUDO_GID", "-1"))
            os.chown(path, uid, gid)
        except (ValueError, OSError):
            pass


def _run_debugfs(image, command, writable=False, allow_missing=False):
    executable = shutil.which("debugfs")
    if not executable:
        raise ImageError("debugfs is required for offline ext4 image editing (install e2fsprogs).")
    argv = [executable]
    if writable:
        argv.append("-w")
    argv.extend(("-R", command, str(image)))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not run debugfs: " + str(exc)) from exc
    output = result.stdout + result.stderr
    missing = re.search(r"File not found|ext2_lookup", output, re.IGNORECASE)
    if allow_missing and missing:
        return output
    if result.returncode or re.search(
            r"(?:File not found|Command not found|Could not|Permission denied|Invalid argument)",
            output, re.IGNORECASE):
        raise ImageError("Could not safely edit the ext4 image using debugfs.")
    return output


def _check_ext4(image):
    executable = shutil.which("e2fsck")
    if not executable:
        raise ImageError("e2fsck is required to check edited images (install e2fsprogs).")
    try:
        result = subprocess.run([executable, "-f", "-n", str(image)], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not check the ext4 image: " + str(exc)) from exc
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        if len(detail) > 1600:
            detail = "…\n" + detail[-1600:]
        message = ("The ext4 image did not pass its read-only filesystem check "
                   "(e2fsck exit code {}). It was not accepted.".format(result.returncode))
        if detail:
            message += "\ne2fsck report:\n" + detail
        raise ImageError(message)


def _replay_journal_on_copy(image):
    """Let e2fsck perform only its automatic fixes on a disposable image."""
    executable = shutil.which("e2fsck")
    if not executable:
        raise ImageError("e2fsck is required to prepare ext4 images (install e2fsprogs).")
    try:
        result = subprocess.run([executable, "-f", "-p", str(image)], capture_output=True,
                                text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError("Could not check the working ext4 copy: " + str(exc)) from exc
    if result.returncode not in (0, 1):
        detail = (result.stdout + result.stderr).strip()[-1600:]
        raise ImageError("The working ext4 copy needs repairs beyond automatic journal replay. "
                         "The original image was not changed.\n" + detail)
    _check_ext4(image)
    return result.returncode == 1


def _extract(image, image_path, destination, optional=False):
    destination = Path(destination)
    if destination.exists():
        destination.unlink()
    output = _run_debugfs(image, "dump " + image_path + " " + str(destination), allow_missing=optional)
    if not destination.is_file():
        if optional and re.search(r"File not found|ext2_lookup", output, re.IGNORECASE):
            return None
        raise ImageError("Expected file was not found in the var image: " + image_path)
    return destination.read_bytes()


def _json_mode(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageError("/jibo/mode.json is not valid UTF-8 JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mode"), str):
        raise ImageError("/jibo/mode.json must be a JSON object with a string 'mode' field.")
    return data


def _file_metadata(image, image_path):
    output = _run_debugfs(image, "stat " + image_path)
    mode = re.search(r"\bMode:\s+(?:0)?([0-7]{3,4})\b", output)
    file_type = re.search(r"\bType:\s+([A-Za-z]+)\b", output)
    uid = re.search(r"\bUser:\s*(\d+)\b", output)
    gid = re.search(r"\bGroup:\s*(\d+)\b", output)
    if not (mode and file_type and uid and gid):
        raise ImageError("Could not read original file permissions from the var image.")
    if file_type.group(1).lower() != "regular":
        raise ImageError("Only regular files can be replaced in a var image.")
    # debugfs's Mode display contains permission bits only. set_inode_field needs
    # the ext4 regular-file type bits as well, or it creates an invalid inode.
    full_mode = 0o100000 | int(mode.group(1), 8)
    return {"mode": format(full_mode, "07o"),
            "uid": uid.group(1), "gid": gid.group(1)}


def _replace_file(image, image_path, content, workdir):
    metadata = _file_metadata(image, image_path)
    replacement = Path(workdir) / "replacement"
    replacement.write_bytes(content)
    replacement.chmod(0o600)
    _run_debugfs(image, "rm " + image_path, writable=True)
    _run_debugfs(image, "write " + str(replacement) + " " + image_path, writable=True)
    for field in ("mode", "uid", "gid"):
        _run_debugfs(image, "set_inode_field " + image_path + " " + field + " " + metadata[field],
                     writable=True)
    verified = _extract(image, image_path, Path(workdir) / "verified", optional=False)
    if verified != content:
        raise ImageError("The edited file did not match its offline readback.")


def _put_factory_file(image, image_path, content, workdir, mode):
    """Replace a stock file or add a missing per-robot file on a disposable image."""
    existing = _extract(image, image_path, Path(workdir) / "existing", optional=True)
    if existing is not None:
        metadata = _file_metadata(image, image_path)
        _run_debugfs(image, "rm " + image_path, writable=True)
    else:
        metadata = {"mode": format(0o100000 | mode, "07o"), "uid": "0", "gid": "0"}
    replacement = Path(workdir) / "replacement"
    replacement.write_bytes(content)
    replacement.chmod(0o600)
    _run_debugfs(image, "write " + str(replacement) + " " + image_path, writable=True)
    for field in ("mode", "uid", "gid"):
        _run_debugfs(image, "set_inode_field " + image_path + " " + field + " " + metadata[field],
                     writable=True)
    if _extract(image, image_path, Path(workdir) / "verified") != content:
        raise ImageError("The factory file did not match its offline readback: " + image_path)


def _factory_inputs(backup, stock):
    """Read identity and camera files from one robot's verified var."""
    with tempfile.TemporaryDirectory(prefix="jibo-factory-input-") as temporary:
        raw = {}
        parsed = {}
        for index, path in enumerate((FACTORY_IDENTITY_PATH, *FACTORY_CAMERA_PATHS)):
            content = _extract(backup, path, Path(temporary) / str(index))
            if not content or len(content) > 65536:
                raise ImageError("Missing or oversized factory file in the var backup: " + path)
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageError("The var backup has invalid factory JSON at " + path) from exc
            if not isinstance(value, dict):
                raise ImageError("The var backup has invalid factory data at " + path)
            raw[path] = content
            parsed[path] = value
        identity = parsed[FACTORY_IDENTITY_PATH]
        name = identity.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+){2,3}", name) or \
                not all(isinstance(identity.get(key), str) and identity[key]
                        for key in ("serial_number", "cpuid", "wifi_mac")):
            raise ImageError("The var backup does not contain a complete robot identity.")
        defaults = []
        for index, path in enumerate(FACTORY_CAMERA_PATHS):
            content = _extract(stock, path, Path(temporary) / ("default-" + str(index)))
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageError("The package has invalid default calibration at " + path) from exc
            if not isinstance(value, dict) or not value or not set(value).issubset(parsed[path]):
                raise ImageError("The saved camera calibration does not match the package schema.")
            defaults.append(value)
        camera_matches_stock = all(parsed[path] == default
                                   for path, default in zip(FACTORY_CAMERA_PATHS, defaults))
        return raw, name, camera_matches_stock


def factory_calibration_available(backup, stock):
    """Whether this var backup can supply a factory-like fresh stock image."""
    try:
        _factory_inputs(backup, stock)
    except ImageError:
        return False
    return True


def build_factory_fresh_var(stock, backup, destination):
    """Make stock var fresh for OOBE while retaining this robot's factory files."""
    stock, backup, destination = Path(stock), Path(backup), Path(destination)
    if stock.is_symlink() or backup.is_symlink() or destination.exists() or destination.is_symlink():
        raise ImageError("Factory var inputs must be regular files and the output must be new.")
    raw, name, camera_matches_stock = _factory_inputs(backup, stock)
    try:
        shutil.copyfile(stock, destination)
        destination.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix="jibo-factory-edit-") as temporary:
            for path in FACTORY_CAMERA_PATHS:
                _put_factory_file(destination, path, raw[path], temporary, 0o644)
            _put_factory_file(destination, FACTORY_IDENTITY_PATH,
                              raw[FACTORY_IDENTITY_PATH], temporary, 0o600)
            _put_factory_file(destination, "/jibo/mode.json", b'{"mode":"oobe"}',
                              temporary, 0o600)
            _put_factory_file(destination, "/etc/hostname", (name + "\n").encode("ascii"),
                              temporary, 0o644)
            hosts = ("127.0.0.1\tlocalhost\n127.0.1.1\t" + name + "\n").encode("ascii")
            _put_factory_file(destination, "/etc/hosts", hosts, temporary, 0o644)
        _check_ext4(destination)
        if destination.stat().st_size != stock.stat().st_size:
            raise ImageError("The calibrated var image changed length.")
        return {"image": str(destination), "calibration_files": list(FACTORY_CAMERA_PATHS),
                "identity_restored": True, "mode": "oobe",
                "camera_matches_stock": camera_matches_stock,
                "sha256": sha256_file(destination)}
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _copy_for_edit(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise ImageError("Var image does not exist: " + str(source))
    if source == destination:
        raise ImageError("Choose a new output path; the source backup is kept unchanged.")
    if destination.exists():
        raise ImageError("Output already exists; choose a new filename: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ImageError("Could not create a working copy of the var image: " + str(exc)) from exc
    destination.chmod(0o600)
    _chown_to_invoking_user(destination)
    try:
        repaired = _replay_journal_on_copy(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return source, destination, repaired


def inspect_var(image):
    image = Path(image).expanduser().resolve()
    if not image.is_file():
        raise ImageError("Var image does not exist: " + str(image))
    with tempfile.TemporaryDirectory(prefix="jibo-inspect-", dir="/tmp") as temp:
        working = Path(temp) / "var.img"
        shutil.copyfile(image, working)
        repaired = _replay_journal_on_copy(working)
        mode_raw = _extract(working, VAR_MODE_PATH, Path(temp) / "mode.json")
        mode = _json_mode(mode_raw)["mode"]
        wifi_raw = _extract(working, VAR_WIFI_PATH, Path(temp) / "wifi.conf", optional=True)
    try:
        networks = _network_blocks(wifi_raw.decode("utf-8")) if wifi_raw is not None else []
    except UnicodeDecodeError as exc:
        raise ImageError("The Wi-Fi config is not valid UTF-8; no content was displayed.") from exc
    return {"mode": mode, "wifi_configured": wifi_raw is not None,
            "wifi_network_count": len(networks), "journal_replayed_on_temporary_copy": repaired}


def edit_mode(source, destination, mode):
    if mode not in MODE_VALUES:
        raise ImageError("Mode must be one of: " + ", ".join(MODE_VALUES))
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-mode-", dir="/tmp") as temp:
            current_raw = _extract(destination, VAR_MODE_PATH, Path(temp) / "mode.json")
            data = _json_mode(current_raw)
            previous = data["mode"]
            data["mode"] = mode
            replacement = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
            _replace_file(destination, VAR_MODE_PATH, replacement, temp)
            _replay_journal_on_copy(destination)
            final = _json_mode(_extract(destination, VAR_MODE_PATH, Path(temp) / "final.json"))
            if final["mode"] != mode:
                raise ImageError("The edited mode did not survive image readback.")
        return {"image": str(destination), "previous_mode": previous,
                "mode": mode, "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _network_blocks(text):
    """Return balanced network={...} blocks without interpreting their secrets."""
    blocks = []
    depth = 0
    start = None
    quoted = False
    escaped = False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "#":
            comment = True
        elif char == "{":
            if depth == 0:
                before = text[:index]
                match = re.search(r"\bnetwork\s*=\s*$", before)
                if match:
                    start = match.start()
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ImageError("Wi-Fi configuration has unmatched braces.")
            if depth == 0 and start is not None:
                blocks.append(text[start:index + 1])
                start = None
    if quoted or depth != 0:
        raise ImageError("Wi-Fi configuration has an unfinished string or network block.")
    return blocks


def _decode_ssid(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        result = bytearray()
        index = 0
        while index < len(value):
            char = value[index]
            if char != "\\":
                result.extend(char.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index >= len(value):
                return None
            escaped = value[index]
            if escaped == "x" and index + 2 < len(value):
                try:
                    result.append(int(value[index + 1:index + 3], 16))
                except ValueError:
                    return None
                index += 3
            elif escaped in ('\\', '"'):
                result.extend(escaped.encode("utf-8"))
                index += 1
            else:
                result.extend(escaped.encode("utf-8"))
                index += 1
        return bytes(result)
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,32}", value):
        return bytes.fromhex(value)
    return None


def _existing_ssids(blocks):
    values = set()
    for block in blocks:
        match = re.search(r"^\s*ssid\s*=\s*(.*)$", block, re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value.startswith('"'):
                escaped = False
                closing = None
                for index in range(1, len(value)):
                    if escaped:
                        escaped = False
                    elif value[index] == "\\":
                        escaped = True
                    elif value[index] == '"':
                        closing = index
                        break
                value = value[:closing + 1] if closing is not None else value
            else:
                value = value.split("#", 1)[0].strip()
            decoded = _decode_ssid(value)
            if decoded is not None:
                values.add(decoded)
    return values


def build_wifi_config(existing, ssid, password=None, open_network=False,
                      compact=False):
    try:
        ssid_bytes = ssid.encode("utf-8")
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ImageError("SSID must be valid UTF-8 text.") from exc
    if not 1 <= len(ssid_bytes) <= 32 or b"\x00" in ssid_bytes:
        raise ImageError("SSID must be 1 to 32 UTF-8 bytes.")
    try:
        text = existing.decode("utf-8") if existing is not None else "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\n"
    except UnicodeDecodeError as exc:
        raise ImageError("The existing Wi-Fi config is not valid UTF-8; it was left unchanged.") from exc
    if compact:
        # The stock file is mostly documentation and occupies four 1 KiB
        # blocks. Keep active directives and networks so later file-RPC edits
        # can use its one-block writer when the filesystem is clean.
        text = "\n".join(line for line in text.splitlines()
                         if line.strip() and not line.lstrip().startswith("#")) + "\n"
    blocks = _network_blocks(text)
    if ssid_bytes in _existing_ssids(blocks):
        raise ImageError("That SSID already exists in the config; remove or replace its saved network before adding it again.")
    if open_network:
        if password not in (None, ""):
            raise ImageError("An open network cannot have a password.")
        settings = "    key_mgmt=NONE\n"
    else:
        if not isinstance(password, str):
            raise ImageError("A Wi-Fi password is required for a protected network.")
        try:
            password_bytes = password.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ImageError("Password must be valid UTF-8 text.") from exc
        if not 8 <= len(password_bytes) <= 63 or any(byte < 0x20 or byte > 0x7e for byte in password_bytes):
            raise ImageError("WPA password must be 8 to 63 printable ASCII characters.")
        psk = hashlib.pbkdf2_hmac("sha1", password_bytes, ssid_bytes, 4096, dklen=32).hex()
        settings = "    key_mgmt=WPA-PSK\n    psk=" + psk + "\n"
    addition = "network={\n    ssid=" + ssid_bytes.hex() + "\n" + settings + "}\n"
    if text and not text.endswith("\n"):
        text += "\n"
    result = (text + addition).encode("utf-8", errors="surrogateescape")
    if len(_network_blocks(result.decode("utf-8", errors="strict"))) != len(blocks) + 1:
        raise ImageError("Generated Wi-Fi configuration failed structural validation.")
    return result, len(blocks) + 1


def edit_wifi(source, destination, ssid, password=None, open_network=False,
              compact=False):
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-wifi-", dir="/tmp") as temp:
            existing = _extract(destination, VAR_WIFI_PATH, Path(temp) / "wifi.conf", optional=True)
            replacement, count = build_wifi_config(existing, ssid, password,
                                                   open_network, compact=compact)
            if existing is None:
                local = Path(temp) / "replacement"
                local.write_bytes(replacement)
                local.chmod(0o600)
                _run_debugfs(destination, "write " + str(local) + " " + VAR_WIFI_PATH, writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " mode 0644", writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " uid 0", writable=True)
                _run_debugfs(destination, "set_inode_field " + VAR_WIFI_PATH + " gid 0", writable=True)
            else:
                _replace_file(destination, VAR_WIFI_PATH, replacement, temp)
            _replay_journal_on_copy(destination)
            verified = _extract(destination, VAR_WIFI_PATH, Path(temp) / "verified")
            if verified != replacement:
                raise ImageError("The edited Wi-Fi file did not survive image readback.")
        return {"image": str(destination), "network_count": count,
                "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def edit_mode_wifi(source, destination, mode, ssid, password=None,
                   open_network=False):
    """Apply mode and Wi-Fi to one disposable var copy and check both files."""
    if mode not in MODE_VALUES:
        raise ImageError("Mode must be one of: " + ", ".join(MODE_VALUES))
    source, destination, repaired = _copy_for_edit(source, destination)
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-mode-wifi-", dir="/tmp") as temp:
            work = Path(temp)
            mode_data = _json_mode(_extract(destination, VAR_MODE_PATH, work / "mode.json"))
            previous = mode_data["mode"]
            mode_data["mode"] = mode
            mode_bytes = (json.dumps(mode_data, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
            wifi_before = _extract(destination, VAR_WIFI_PATH, work / "wifi.conf", optional=True)
            wifi_bytes, count = build_wifi_config(wifi_before, ssid, password,
                                                  open_network, compact=True)
            _replace_file(destination, VAR_MODE_PATH, mode_bytes, temp)
            if wifi_before is None:
                _put_factory_file(destination, VAR_WIFI_PATH, wifi_bytes, temp, 0o644)
            else:
                _replace_file(destination, VAR_WIFI_PATH, wifi_bytes, temp)
            _replay_journal_on_copy(destination)
            final_mode = _json_mode(_extract(destination, VAR_MODE_PATH, work / "final-mode"))
            final_wifi = _extract(destination, VAR_WIFI_PATH, work / "final-wifi")
            if final_mode["mode"] != mode or final_wifi != wifi_bytes:
                raise ImageError("The combined mode and Wi-Fi edit did not survive image readback.")
        return {"image": str(destination), "previous_mode": previous,
                "mode": mode, "network_count": count,
                "sha256": sha256_file(destination),
                "journal_replayed_on_temporary_copy": repaired}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

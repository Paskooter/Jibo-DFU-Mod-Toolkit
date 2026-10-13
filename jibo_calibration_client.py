"""Optional calibration v3 client. No robot/private-backup bytes cross this boundary."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import tempfile
import time
from urllib.parse import urlsplit

ORIGIN = "https://calibration.jibo.io"
# TODO(root): ship reviewed production Ed25519 public key pins before release.
# Empty intentionally: no production download can verify until root sets this.
PINNED_PUBLIC_KEYS = {}
PROFILE = "jibo-factory-calibration-v3"
ID_KIND = "emmc-cid-sha256-v1"
MAX_BODY = 16384
MAX_BUNDLE = 8192
MAX_MANIFEST = 2048
CONNECT_TIMEOUT = 5
REQUEST_TIMEOUT = 15
UNAVAILABLE = "Calibration sharing/retrieval unavailable on this install (requires cryptography and calibration core)."
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REVIEW_STATES = frozenset(("pending", "approved", "conflict-pending-review", "stock-equivalent-pending", "blocked"))

class ClientError(Exception):
    """Fixed local message only; never echo server bodies or private input."""

class Unavailable(ClientError):
    pass

class HttpError(ClientError):
    def __init__(self, status):
        self.status = status
        super().__init__("Calibration service request failed (HTTP {}).".format(status))


def _crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        from cryptography.hazmat.primitives import serialization
        return Ed25519PrivateKey, Ed25519PublicKey, serialization
    except (ImportError, OSError) as exc:
        raise Unavailable(UNAVAILABLE) from exc


def load_core():
    try:
        return importlib.import_module("jibo_calibration")
    except (ImportError, OSError) as exc:
        raise Unavailable(UNAVAILABLE) from exc


def _hex(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ClientError("Invalid calibration identifier.")
    return value


def _json(data):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            raise ValueError("BOM")
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ClientError("Invalid calibration service response.") from exc


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ClientError("Invalid calibration service response fields.")


def _b64(value, cap):
    try:
        if not isinstance(value, str) or len(value) > ((cap + 2) // 3) * 4:
            raise ValueError("size")
        result = base64.b64decode(value, validate=True)
        if len(result) > cap or base64.b64encode(result).decode("ascii") != value:
            raise ValueError("canonical")
        return result
    except (ValueError, UnicodeError) as exc:
        raise ClientError("Invalid or oversized calibration encoding.") from exc


def _utc(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
            raise ValueError("UTC")
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ClientError("Invalid calibration validity period.") from exc


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ClientError("Invalid calibration size or revision.")
    return value


def _private_write(path, value, chown=None):
    path = Path(path)
    created = []
    parent = path.parent
    while not parent.exists():
        if parent.is_symlink():
            raise ClientError("Unsafe calibration data path.")
        created.append(parent)
        parent = parent.parent
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ClientError("Unsafe calibration data path.")
    path.parent.chmod(0o700)
    for directory in created:
        directory.chmod(0o700)
        if chown:
            chown(directory)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            stream.flush(); os.fsync(stream.fileno())
        if chown:
            chown(path.parent); chown(temporary)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class SubmissionResult:
    message: str
    receipt: str
    removal_authority: bool
    detail: str = ""

@dataclass(frozen=True)
class Download:
    files: dict
    manifest: dict
    envelope: dict


class CalibrationClient:
    def __init__(self, data_dir, core=None, chown=None):
        self.core = core if core is not None else load_core()
        if self.core.PROFILE != PROFILE:
            raise Unavailable("Calibration sharing/retrieval unavailable on this install (unsupported calibration core).")
        self.data_dir = Path(data_dir)
        if self.data_dir.is_symlink() or self.data_dir.parent.is_symlink():
            raise ClientError("Unsafe calibration data directory.")
        self.chown = chown
        self._crypto = _crypto()
        origin = os.environ.get("JIBO_CALIBRATION_ORIGIN", ORIGIN)
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or
                (parsed.scheme == "http" and parsed.hostname != "127.0.0.1")):
            raise ClientError("Calibration origin must use HTTPS (loopback HTTP is for tests only).")
        try:
            self.port = parsed.port
        except ValueError as exc:
            raise ClientError("Invalid calibration origin.") from exc
        self.scheme, self.host = parsed.scheme, parsed.hostname

    def request(self, method, path, body=None):
        encoded = None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if encoded is not None and len(encoded) > MAX_BODY:
            raise ClientError("Calibration submission exceeds the size limit.")
        if self.scheme == "https":
            connection = http.client.HTTPSConnection(self.host, self.port, timeout=CONNECT_TIMEOUT,
                                                       context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(self.host, self.port, timeout=CONNECT_TIMEOUT)
        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            connection.connect()
            transport_socket = connection.sock
            transport_socket.settimeout(max(0.001, deadline - time.monotonic()))
            connection.request(method, path, body=encoded, headers={"Accept": "application/json", "Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ClientError("Calibration redirects are not allowed.")
            if response.status not in (200, 202):
                raise HttpError(response.status)
            if response.getheader("Content-Encoding", "identity").lower() != "identity" or response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise ClientError("Invalid calibration response encoding.")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
                raise ClientError("Calibration response exceeds the size limit.")
            chunks, size = [], 0
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClientError("Calibration request timed out.")
                # The response owns the socket after a Connection: close header.
                transport_socket.settimeout(remaining)
                chunk = response.read1(min(4096, MAX_BODY + 1 - size))
                size += len(chunk)
                if size > MAX_BODY:
                    raise ClientError("Calibration response exceeds the size limit.")
                if not chunk:
                    break
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise ClientError("Incomplete calibration response.")
            return response.status, _json(b"".join(chunks))
        except (OSError, http.client.HTTPException) as exc:
            raise ClientError("Calibration service could not be reached; your local backup is safe.") from exc
        finally:
            connection.close()

    def _validated(self, files):
        specs = {s.key: s for s in self.core.ITEMS}
        if not isinstance(files, dict) or not {"left", "right", "inter_camera"}.issubset(files) or not set(files).issubset(specs):
            raise ClientError("Calibration requires supported camera files; unsupported files stay local.")
        result = {}
        try:
            for key, data in files.items():
                if not isinstance(data, bytes) or not 0 < len(data) <= specs[key].cap:
                    raise ClientError("Calibration item exceeds its size limit.")
                validated = self.core.validate_item(key, data)
                if validated != data:
                    raise ClientError("Calibration validator changed original bytes.")
                result[key] = data
        except self.core.CalibrationError as exc:
            raise ClientError("Calibration kept local (unsupported format).") from exc
        if len(self.core.canonical_bundle(result)) > MAX_BUNDLE:
            raise ClientError("Calibration bundle exceeds its size limit.")
        return result

    def candidate_status(self, robot_key, candidate):
        _, data = self.request("GET", "/v3/robots/{}/candidates/{}/status".format(_hex(robot_key), _hex(candidate)))
        _fields(data, ("schema", "robot_key", "candidate_sha256", "review_state", "calibration_schema", "deletion_public_key"))
        if (data["schema"] != "jibo-calibration-candidate-status-v3" or data["robot_key"] != robot_key or
                data["candidate_sha256"] != candidate or data["calibration_schema"] != PROFILE or
                data["review_state"] not in REVIEW_STATES):
            raise ClientError("Calibration submission readback did not match.")
        _hex(data["deletion_public_key"])
        return data

    def _receipt(self, robot_key, candidate):
        private_type, _, serialization = self._crypto
        path = self.data_dir / "receipts" / (robot_key + "-" + candidate + ".json")
        if path.exists() or path.is_symlink():
            receipt = self.read_receipt(path)
            if receipt["robot_key"] != robot_key or receipt["candidate_sha256"] != candidate:
                raise ClientError("Removal receipt does not match.")
            return path, receipt
        private = private_type.generate()
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        receipt = {"schema": "jibo-calibration-removal-receipt-v3", "robot_key": robot_key, "candidate_sha256": candidate,
                   "deletion_public_key": public, "private_key_b64": base64.b64encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode("ascii")}
        _private_write(path, receipt, self.chown)  # Durable BEFORE POST, including ambiguous failure.
        return path, receipt

    def read_receipt(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BODY or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ClientError("Removal receipt must be a private regular file (0600).")
        data = _json(path.read_bytes())
        _fields(data, ("schema", "robot_key", "candidate_sha256", "deletion_public_key", "private_key_b64"))
        if data["schema"] != "jibo-calibration-removal-receipt-v3":
            raise ClientError("Unsupported removal receipt.")
        for key in ("robot_key", "candidate_sha256", "deletion_public_key"):
            _hex(data[key])
        private_type, _, serialization = self._crypto
        private = private_type.from_private_bytes(_b64(data["private_key_b64"], 32))
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        if public != data["deletion_public_key"]:
            raise ClientError("Invalid removal receipt.")
        return data

    def submit(self, robot_key, files):
        _hex(robot_key)
        files = self._validated(files)
        candidate = self.core.candidate_sha256(files)
        path, receipt = self._receipt(robot_key, candidate)
        body = {"schema": "jibo-calibration-submission-v3", "robot_key": robot_key, "id_kind": ID_KIND,
                "calibration_schema": PROFILE, "files": {k: base64.b64encode(v).decode("ascii") for k, v in files.items()},
                "source": "dfu-backup", "consent_version": "calibration-v3", "deletion_public_key": receipt["deletion_public_key"]}
        try:
            status, data = self.request("POST", "/v3/submissions", body)
            _fields(data, ("schema", "status", "robot_key", "candidate_sha256", "consent_version", "removal_instructions_url", "deletion_public_key"))
            # Response schema/status labels are not restore authority; readback is.
            if (not isinstance(data["schema"], str) or not isinstance(data["status"], str) or
                    data["robot_key"] != robot_key or data["candidate_sha256"] != candidate or
                    data["consent_version"] != "calibration-v3"):
                raise ClientError("Calibration submission response did not match.")
            saved = self.candidate_status(robot_key, candidate)
            if saved["deletion_public_key"] != data["deletion_public_key"]:
                raise ClientError("Calibration removal authority readback did not match.")
            authority = saved["deletion_public_key"] == receipt["deletion_public_key"]
            return SubmissionResult("Already stored" if status == 200 else "Submitted for review", str(path), authority,
                                    "" if authority else "This computer does not hold the original removal receipt.")
        except ClientError:
            return SubmissionResult("Submission outcome unknown; your local backup is safe", str(path), False)

    def lookup(self, robot_key):
        _hex(robot_key)
        _, envelope = self.request("GET", "/v3/robots/" + robot_key)
        _fields(envelope, ("manifest_b64", "signature_b64", "bundle_b64"))
        raw = _b64(envelope["manifest_b64"], MAX_MANIFEST)
        signature = _b64(envelope["signature_b64"], 64)
        # Verify exact bytes against pins BEFORE trusting/parsing the manifest.
        _, public_type, _ = self._crypto
        verified_ids = []
        for key_id, public in PINNED_PUBLIC_KEYS.items():
            try:
                public_type.from_public_bytes(bytes.fromhex(public)).verify(signature, b"jibo-calibration-download-v3\0" + raw)
                verified_ids.append(key_id)
            except Exception:
                continue
        if not verified_ids:
            raise ClientError("Calibration signature cannot be verified with this release's pinned keys.")
        manifest = _json(raw)
        _fields(manifest, ("schema", "robot_key", "id_kind", "calibration_schema", "review_state", "assurance", "has_conflicts", "revision", "issued_utc", "expires_utc", "bundle_sha256", "bundle_bytes", "files", "stock_profile_ids", "key_id"))
        if (manifest["key_id"] not in verified_ids or manifest["schema"] != "jibo-calibration-download-v3" or
                manifest["robot_key"] != robot_key or manifest["id_kind"] != ID_KIND or manifest["calibration_schema"] != PROFILE or
                manifest["review_state"] != "approved" or manifest["assurance"] not in ("community-reviewed", "reviewed-stock-equivalent") or type(manifest["has_conflicts"]) is not bool):
            raise ClientError("Calibration signature/profile/robot did not match.")
        revision = _integer(manifest["revision"], 1, 2**53 - 1)
        issued, expires = _utc(manifest["issued_utc"]), _utc(manifest["expires_utc"])
        now = datetime.now(timezone.utc)
        if not issued <= now < expires or not 0 < (expires - issued).total_seconds() <= 7 * 86400:
            raise ClientError("Calibration expired or the computer clock is incorrect.")
        profiles = manifest["stock_profile_ids"]
        if not isinstance(profiles, list) or not profiles or len(profiles) > 16 or any(not isinstance(p, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", p) for p in profiles):
            raise ClientError("Unsupported calibration compatibility profile.")
        floor_path = self.data_dir / "revisions" / (robot_key + ".json")
        if floor_path.exists():
            if floor_path.is_symlink() or floor_path.stat().st_size > MAX_BODY:
                raise ClientError("Unsafe calibration revision state.")
            floor = _json(floor_path.read_bytes()).get("revision")
            if revision < _integer(floor, 1, 2**53 - 1):
                raise ClientError("Calibration revision downgrade refused.")
        bundle = _b64(envelope["bundle_b64"], MAX_BUNDLE)
        if len(bundle) != _integer(manifest["bundle_bytes"], 1, MAX_BUNDLE) or hashlib.sha256(bundle).hexdigest() != _hex(manifest["bundle_sha256"]):
            raise ClientError("Calibration bundle hash/size did not match.")
        decoded = _json(bundle)
        _fields(decoded, ("files",))
        if not isinstance(decoded["files"], dict):
            raise ClientError("Invalid calibration files.")
        specs = {s.key: s for s in self.core.ITEMS}
        if not set(decoded["files"]).issubset(specs):
            raise ClientError("Unsupported calibration files.")
        files = {k: _b64(v, specs[k].cap) for k, v in decoded["files"].items()}
        entries = manifest["files"]
        if not isinstance(entries, list) or len(entries) != len(files):
            raise ClientError("Calibration file manifest did not match.")
        for entry, key in zip(entries, sorted(files)):
            _fields(entry, ("name", "sha256", "bytes"))
            if entry["name"] != key or len(files[key]) != _integer(entry["bytes"], 1, specs[key].cap) or hashlib.sha256(files[key]).hexdigest() != _hex(entry["sha256"]):
                raise ClientError("Calibration file hash/size did not match.")
        if self.core.candidate_sha256(files) != manifest["bundle_sha256"]:
            raise ClientError("Calibration canonical bundle did not match.")
        files = self._validated(files)
        _private_write(floor_path, {"revision": revision}, self.chown)
        return Download(files, manifest, envelope)

    def remove(self, receipt_path):
        data = self.read_receipt(receipt_path)
        robot_key, candidate = data["robot_key"], data["candidate_sha256"]
        _, challenge = self.request("POST", "/v3/deletions/challenge", {"robot_key": robot_key, "candidate_sha256": candidate})
        _fields(challenge, ("schema", "challenge_id", "nonce", "expires_utc"))
        challenge_id = challenge["challenge_id"]
        if (not isinstance(challenge_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", challenge_id) or
                not isinstance(challenge["nonce"], str) or not 1 <= len(challenge["nonce"]) <= 256 or
                not 0 < (_utc(challenge["expires_utc"]) - datetime.now(timezone.utc)).total_seconds() <= 120):
            raise ClientError("Invalid or expired calibration deletion challenge.")
        message = b"jibo-calibration-delete-v3\0" + challenge_id.encode("ascii") + b"\0" + robot_key.encode("ascii") + b"\0" + candidate.encode("ascii")
        private_type, _, _ = self._crypto
        signature = private_type.from_private_bytes(_b64(data["private_key_b64"], 32)).sign(message)
        self.request("POST", "/v3/deletions", {"schema": "jibo-calibration-delete-request-v3", "robot_key": robot_key, "candidate_sha256": candidate, "challenge_id": challenge_id, "signature_b64": base64.b64encode(signature).decode("ascii")})
        try:
            self.candidate_status(robot_key, candidate)
        except HttpError as exc:
            if exc.status not in (404, 410):
                raise
        else:
            raise ClientError("Removal outcome unknown; candidate is still stored.")
        try:
            _, remaining = self.request("GET", "/v3/robots/" + robot_key)
        except HttpError as exc:
            if exc.status not in (404, 410):
                raise
        else:
            _fields(remaining, ("manifest_b64", "signature_b64", "bundle_b64"))
            # A different contributor's approved candidate may remain.
            if self.lookup(robot_key).manifest["bundle_sha256"] == candidate:
                raise ClientError("Removal outcome unknown; approved candidate remains.")
        return "Removed from the live database"

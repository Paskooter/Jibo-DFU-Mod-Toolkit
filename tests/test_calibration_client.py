"""Loopback-only v3 wire oracle, synthetic measurements and ephemeral keys."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import calibration_core_fake as core
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:
    Ed25519PrivateKey = None


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


class Server:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.requests = []
        self.candidate = None
        self.public = None
        self.removed = False
        self.mode = "good"
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                if owner.mode == "redirect":
                    self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/leak"); self.end_headers(); return
                if owner.mode == "oversize":
                    self.reply(200, b" " * 16385); return
                if owner.removed:
                    self.reply(410, {"code": "removed"}); return
                if self.path.endswith("/status"):
                    if owner.mode == "status-failure":
                        self.reply(503, {"code": "unavailable"}); return
                    self.reply(200, {"schema": "jibo-calibration-candidate-status-v3", "robot_key": core.VECTOR["robot_key"], "candidate_sha256": owner.candidate, "review_state": "pending", "calibration_schema": core.PROFILE, "deletion_public_key": owner.public}); return
                self.reply(200, owner.envelope())
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, data))
                if self.path == "/v3/submissions":
                    duplicate = owner.candidate is not None
                    self.server.testcase.assertEqual(set(data), {"schema", "robot_key", "id_kind", "calibration_schema", "files", "source", "consent_version", "deletion_public_key"})
                    self.server.testcase.assertEqual(data["schema"], "jibo-calibration-submission-v3")
                    self.server.testcase.assertEqual(data["source"], "dfu-backup")
                    self.server.testcase.assertEqual(data["files"], core.VECTOR["files_b64"])
                    owner.candidate = core.VECTOR["candidate_sha256"]
                    owner.public = owner.public or data["deletion_public_key"]
                    self.reply(200 if duplicate else 202, {"schema": "jibo-calibration-submission-result-v3", "status": "already-stored" if duplicate else "pending", "robot_key": data["robot_key"], "candidate_sha256": owner.candidate, "consent_version": "calibration-v3", "removal_instructions_url": "https://jibo.io/privacy", "deletion_public_key": owner.public})
                elif self.path == "/v3/deletions/challenge":
                    self.reply(200, {"schema": "jibo-calibration-delete-challenge-v3", "challenge_id": "synthetic-challenge", "nonce": "synthetic-nonce", "expires_utc": (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")})
                elif self.path == "/v3/deletions":
                    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
                    message = b"jibo-calibration-delete-v3\0synthetic-challenge\0" + data["robot_key"].encode() + b"\0" + data["candidate_sha256"].encode()
                    Ed25519PublicKey.from_public_bytes(bytes.fromhex(owner.public)).verify(base64.b64decode(data["signature_b64"]), message)
                    owner.removed = True
                    self.reply(200, {"status": "removed"})
                else:
                    self.reply(404, {"code": "not-found"})
            def reply(self, status, data):
                payload = data if isinstance(data, bytes) else compact(data)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
    def envelope(self):
        bundle = core.canonical_bundle(core.FILES)
        now = datetime.now(timezone.utc)
        manifest = {"schema": "jibo-calibration-download-v3", "robot_key": core.VECTOR["robot_key"], "id_kind": "emmc-cid-sha256-v1", "calibration_schema": core.PROFILE, "review_state": "approved", "assurance": "community-reviewed", "has_conflicts": False, "revision": 1, "issued_utc": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "expires_utc": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "bundle_sha256": core.VECTOR["candidate_sha256"], "bundle_bytes": len(bundle), "files": [{"name": k, "sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in sorted(core.FILES.items())], "stock_profile_ids": [core.PROFILE], "key_id": "test-key"}
        if self.mode == "wrong-robot": manifest["robot_key"] = "a" * 64
        if self.mode == "expired": manifest["expires_utc"] = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.mode == "file-hash": manifest["files"][0]["sha256"] = "0" * 64
        if self.mode == "unknown-key": manifest["key_id"] = "unknown"
        if self.mode == "revision": manifest["revision"] = 0
        raw = compact(manifest)
        signature = self.key.sign(b"jibo-calibration-download-v3\0" + raw)
        if self.mode == "signature": signature = b"0" * 64
        if self.mode == "bundle-hash": bundle += b" "
        return {"manifest_b64": base64.b64encode(raw).decode(), "signature_b64": base64.b64encode(signature).decode(), "bundle_b64": base64.b64encode(bundle).decode()}
    def __enter__(self):
        self.thread.start(); return self
    def __exit__(self, *args):
        self.http.shutdown(); self.http.server_close(); self.thread.join()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = importlib.import_module("jibo_calibration_client")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
    def test_vectors_exact_bytes(self):
        self.assertEqual(core.candidate_sha256(core.FILES), core.VECTOR["candidate_sha256"])
        self.assertEqual(core.robot_key_from_cid(core.VECTOR["cid_serial"]), core.VECTOR["robot_key"])
        self.assertEqual(core.canonical_bundle(core.FILES), core.VECTOR["canonical_bundle_utf8"].encode())
    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_signed_lookup_and_rejections(self):
        with Server() as server:
            public = server.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)), patch.dict(self.client.PINNED_PUBLIC_KEYS, {"test-key": public}, clear=True):
                c = self.client.CalibrationClient(self.temp.name, core=core)
                downloaded = c.lookup(core.VECTOR["robot_key"])
                self.assertEqual(downloaded.files, core.FILES)
                for mode in ("signature", "wrong-robot", "expired", "file-hash", "unknown-key", "bundle-hash", "oversize", "redirect", "revision"):
                    with self.subTest(mode=mode):
                        server.mode = mode
                        with self.assertRaises(self.client.ClientError):
                            c.lookup(core.VECTOR["robot_key"])
                self.assertEqual(len(server.requests), 10)

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_unpinned_and_core_invalid_bytes_are_rejected(self):
        with Server() as server:
            public = server.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)):
                c = self.client.CalibrationClient(self.temp.name, core=core)
                with patch.dict(self.client.PINNED_PUBLIC_KEYS, {}, clear=True), self.assertRaises(self.client.ClientError):
                    c.lookup(core.VECTOR["robot_key"])
                with patch.dict(self.client.PINNED_PUBLIC_KEYS, {"test-key": public}, clear=True), patch.object(core, "validate_item", side_effect=core.CalibrationError("synthetic unsupported")), self.assertRaises(self.client.ClientError):
                    c.lookup(core.VECTOR["robot_key"])

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_status_failure_is_unknown_receipt_survives(self):
        with Server() as server:
            server.http.testcase = self
            server.mode = "status-failure"
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)):
                c = self.client.CalibrationClient(self.temp.name, core=core)
                result = c.submit(core.VECTOR["robot_key"], core.FILES)
                self.assertEqual(result.message, "Submission outcome unknown; your local backup is safe")
                self.assertTrue(Path(result.receipt).is_file())
                server.mode = "good"
                self.assertEqual(c.submit(core.VECTOR["robot_key"], core.FILES).message, "Already stored")

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_duplicate_does_not_replace_original_removal_authority(self):
        with Server() as server:
            server.http.testcase = self
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)):
                first = self.client.CalibrationClient(self.temp.name, core=core).submit(core.VECTOR["robot_key"], core.FILES)
                second = self.client.CalibrationClient(Path(self.temp.name) / "second", core=core).submit(core.VECTOR["robot_key"], core.FILES)
                self.assertTrue(first.removal_authority)
                self.assertEqual(second.message, "Already stored")
                self.assertFalse(second.removal_authority)
                self.assertIn("original removal receipt", second.detail)

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_https_rejects_untrusted_local_certificate_before_http_request(self):
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        import ipaddress
        import ssl
        with Server() as server:
            key = Ed25519PrivateKey.generate()
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-loopback")])
            now = datetime.now(timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, None))
            root = Path(self.temp.name)
            cert_path, key_path = root / "synthetic-cert.pem", root / "synthetic-key.pem"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key_path.chmod(0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            server.http.socket = context.wrap_socket(server.http.socket, server_side=True)
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="https://127.0.0.1:" + str(server.http.server_port)):
                c = self.client.CalibrationClient(self.temp.name, core=core)
                with self.assertRaises(self.client.ClientError):
                    c.lookup(core.VECTOR["robot_key"])
                self.assertEqual(server.requests, [])

    def test_missing_crypto_is_clear_and_local_import_works(self):
        with patch.dict(sys.modules, {"cryptography.hazmat.primitives.asymmetric.ed25519": None}):
            with self.assertRaisesRegex(self.client.Unavailable, "unavailable on this install"):
                self.client.CalibrationClient(self.temp.name, core=core)

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_origin_and_identifier_validation_before_requests(self):
        for origin in ("http://example.com", "http://localhost", "https://user:pass@example.com", "https://example.com/path", "https://example.com?robot=secret"):
            with self.subTest(origin=origin), patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN=origin), self.assertRaises(self.client.ClientError):
                self.client.CalibrationClient(self.temp.name, core=core)
        c = self.client.CalibrationClient(self.temp.name, core=core)
        with self.assertRaises(self.client.ClientError):
            c.submit("A" * 64, core.FILES)

    @unittest.skipUnless(Ed25519PrivateKey, "optional cryptography unavailable")
    def test_submit_readback_receipt_and_signed_removal(self):
        with Server() as server:
            server.http.testcase = self
            with patch.dict(os.environ, JIBO_CALIBRATION_ORIGIN="http://127.0.0.1:" + str(server.http.server_port)):
                c = self.client.CalibrationClient(self.temp.name, core=core)
                result = c.submit(core.VECTOR["robot_key"], core.FILES)
                self.assertEqual(result.message, "Submitted for review")
                self.assertTrue(result.removal_authority)
                self.assertEqual(Path(result.receipt).stat().st_mode & 0o777, 0o600)
                self.assertEqual(c.submit(core.VECTOR["robot_key"], core.FILES).message, "Already stored")
                self.assertEqual(c.remove(result.receipt), "Removed from the live database")
                self.assertTrue(server.removed)
                self.assertEqual([x[1] for x in server.requests][-2:], ["/v3/robots/" + core.VECTOR["robot_key"] + "/candidates/" + core.VECTOR["candidate_sha256"] + "/status", "/v3/robots/" + core.VECTOR["robot_key"]])

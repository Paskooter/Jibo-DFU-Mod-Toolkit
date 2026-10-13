"""One optional GUI/TUI calibration policy per user operation; offline by default."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path

from jibo_calibration_client import CalibrationClient, ClientError, Unavailable, _private_write

CONSENT_TITLE = "Share this Jibo's factory calibration?"
CONSENT_BODY = ("Recommended. If his calibration is ever lost, by you or a future owner, he can be restored from our free "
                "database. We send only the measurements listed below and a lookup code made from his hardware ID. Never your name, "
                "account, Wi-Fi, photos or recordings. It's anonymous about you, but linked to this robot so it can be found again. "
                "Keep this receipt to remove it later.")
ITEM_WORDS = {"left": "left camera lens", "right": "right camera lens", "inter_camera": "camera-to-camera alignment",
              "imu": "motion sensor corrections", "audio": "microphone levels", "touchscreen": "touchscreen calibration",
              "factory_record": "factory calibration record"}
SHARE_CHOICES = (("share", "Share calibration (recommended)"), ("local", "Keep local only"))
ASSURANCE = "Community reviewed; not factory-authenticated"
_current = ContextVar("calibration_operation", default=None)


@contextmanager
def operation(session):
    """Nested More tools/actions use the same one-operation session."""
    if _current.get() is not None:
        yield _current.get()
        return
    token = _current.set(session)
    try:
        yield session
    finally:
        _current.reset(token)


def active():
    return _current.get()


def on_verified_var_backup_ready(backup, connected_identity):
    session = active()
    if session is None:
        return None
    # Policy/UI errors must NEVER invalidate a verified rollback backup.
    try:
        return session.on_verified_var_backup_ready(backup, connected_identity)
    except Exception:
        session.outcome = "Submission outcome unknown; your local backup is safe"
        return session.outcome


class SharingFlow:
    def __init__(self, api, select):
        self.api = api
        self.select = select
        root = getattr(api, "BACKUP_ROOT", None)
        self.data_dir = Path(root) / "calibration" if root is not None else None
        self.outcome = None
        self.detail = ""
        self.seen = False

    def client(self):
        if self.data_dir is None:
            raise Unavailable("Calibration sharing/retrieval unavailable on this install (no toolkit data directory).")
        factory = getattr(self.api, "calibration_client_factory", None)
        return factory() if factory else CalibrationClient(self.data_dir, chown=getattr(self.api, "_chown_to_invoking_user", None))

    def _preference_path(self, robot_key):
        return self.data_dir / "preferences" / (robot_key + ".json")

    def preference(self, robot_key):
        path = self._preference_path(robot_key)
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                return {"choice": "local"}  # Corrupt state cannot reset a decline to ON.
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"choice": "local"}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {"choice": "local"}

    def save_preference(self, robot_key, **values):
        _private_write(self._preference_path(robot_key), values, getattr(self.api, "_chown_to_invoking_user", None))

    def on_verified_var_backup_ready(self, backup, connected_identity):
        if self.seen:
            return self.outcome
        if backup.get("status") not in ("backup complete", "existing verified backup reused"):
            return "Not shared"
        self.seen = True
        try:
            client = self.client()
            key = client.core.robot_key_from_cid(connected_identity["cid_serial"])
            preference = self.preference(key)
            if preference.get("choice") == "local":
                self.outcome = "Not shared"
                return self.outcome
            result = client.core.extract(backup["image"])
            files = result.files
            if not {"left", "right", "inter_camera"}.issubset(files):
                self.detail = "Backup saved; calibration sharing unavailable (required camera measurements not found)."
                self.outcome = "Not shared"
                return self.outcome
            digest = client.core.candidate_sha256(files)
            if (preference.get("choice") == "share" and preference.get("candidate") == digest and
                    preference.get("consent_version") == "calibration-v3" and preference.get("stored") is True):
                self.outcome = "Already stored"
                return self.outcome
            words = [ITEM_WORDS[k] for k in ITEM_WORDS if k in files]
            detail = CONSENT_BODY + "\n\n" + "\n".join("• " + word for word in words)
            if any(client.core.stock_equivalent(files).values()):
                detail += "\n\nThese files match known package defaults; they may not be this robot's factory calibration."
            report = [ITEM_WORDS[k] + ": kept local (unsupported format)" for k, status in result.status.items() if status == "kept_local_unsupported" and k in ITEM_WORDS]
            if report:
                detail += "\n\n" + "\n".join(report)
            answer = self.select(CONSENT_TITLE, SHARE_CHOICES, detail=detail, initial="share", cancel_label="Keep local only")
            if answer != "share":
                self.save_preference(key, choice="local", consent_version="calibration-v3")
                self.outcome = "Not shared"
                return self.outcome
            # Recheck the selected hardware before any upload; never key an old result to a new robot.
            current = self.api.calibration_identity(port=connected_identity["usb_port"])
            if current != connected_identity:
                raise ClientError("Connected robot changed; calibration not shared.")
            self.save_preference(key, choice="share", candidate=digest, consent_version="calibration-v3", stored=False)
            submitted = client.submit(key, files)
            self.outcome = submitted.message
            self.detail = submitted.detail
            if submitted.receipt:
                self.detail += ("\n" if self.detail else "") + "Removal receipt: " + submitted.receipt
            self.save_preference(key, choice="share", candidate=digest, consent_version="calibration-v3",
                                 stored=self.outcome in ("Already stored", "Submitted for review"))
        except Unavailable as exc:
            self.outcome, self.detail = "Not shared", str(exc)
        except Exception:
            self.outcome = "Submission outcome unknown; your local backup is safe"
        return self.outcome

    def settings(self, identity):
        client = self.client()
        key = client.core.robot_key_from_cid(identity["cid_serial"])
        selected = self.select("Calibration sharing", (("ask", "Offer sharing after a verified backup"), ("local", "Keep local only")),
                               initial="local" if self.preference(key).get("choice") == "local" else "ask",
                               detail="Applies only to this connected robot. Enabling prompts does not upload anything.")
        if selected in ("ask", "local"):
            self.save_preference(key, choice=selected, consent_version="calibration-v3")
        return {"status": "complete", "message": "Calibration sharing preference saved." if selected else "No preference changed."}

    def remove(self, identity):
        client = self.client()
        key = client.core.robot_key_from_cid(identity["cid_serial"])
        receipts = sorted((self.data_dir / "receipts").glob(key + "-*.json"))
        if not receipts:
            return {"status": "complete", "message": "No removal receipt on this computer. Contact support@jibo.io; never send a backup or private key."}
        selected = self.select("Remove my shared calibration", tuple((str(i), "Submission " + p.stem[-12:]) for i, p in enumerate(receipts)),
                               detail="Choose the submission to remove. Copies already downloaded by others cannot be recalled.")
        if selected is None:
            return {"status": "cancelled", "message": "No removal requested."}
        if self.select("Confirm calibration removal", (("cancel", "Cancel"), ("remove", "Remove my shared calibration")), initial="cancel") != "remove":
            return {"status": "cancelled", "message": "No removal requested."}
        if self.api.calibration_identity(port=identity["usb_port"]) != identity:
            raise ClientError("Connected robot changed; removal cancelled.")
        message = client.remove(receipts[int(selected)])
        self.save_preference(key, choice="local", consent_version="calibration-v3")
        return {"status": "complete", "message": message}

    def lookup_for_fresh_setup(self, identity):
        if self.select("Look up this Jibo's calibration", (("lookup", "Look up this Jibo's calibration"), ("local", "Keep local only")),
                       initial="local", detail="No suitable local calibration was found. Lookup sends only a code made from this robot's hardware ID, not your backup.") != "lookup":
            return None
        client = self.client()
        key = client.core.robot_key_from_cid(identity["cid_serial"])
        result = client.lookup(key)
        detail = ASSURANCE + "\n" + "\n".join("• " + ITEM_WORDS[k] for k in ITEM_WORDS if k in result.files)
        detail += "\nRevision: {}\nCalibration digest: {}".format(result.manifest["revision"], result.manifest["bundle_sha256"])
        if result.manifest["has_conflicts"]:
            detail += "\nOther submissions conflict with this reviewed set."
        if result.manifest["assurance"] == "reviewed-stock-equivalent":
            detail += "\nReviewed stock-equivalent measurements; not proven unique factory data."
        detail += "\nIdentity is required separately from this robot's own verified local backup. A download cannot reconstruct lost identity."
        if self.select("Use reviewed calibration?", (("cancel", "Cancel"), ("use", "Use this calibration")), initial="cancel", detail=detail) != "use":
            return None
        if self.api.calibration_identity(port=identity["usb_port"]) != identity:
            raise ClientError("Connected robot changed; downloaded calibration was not used.")
        return result

"""Fake device boundary shared by headless and real-Tk tests; never touches USB."""
from pathlib import Path
from types import SimpleNamespace


class FileLayoutError(Exception):
    def __init__(self, status=3):
        self.status = status
        super().__init__("Fake unsupported file layout" if status == 3 else "Fake dirty journal")


class FakeApi:
    MARKER = "jibo-dfu-v1"
    BACKUP_ROOT = Path("/tmp/fake-jibo-backups")
    FileRpcStatusError = FileLayoutError

    def __init__(self, state="dfu", quick=True):
        self.state = state
        self.alts = [self.MARKER, "var", "rootfsA", "rootfsB", "services", "skills", "emmc-000"]
        if quick:
            self.alts += ["jibo-file-v1", "jibo-file-var-in", "jibo-file-var-out"]
        self.packages = [Path("updates/official-test.tar.bz2")]
        self.calls = []
        self.writes = []
        self.fail = None
        self.images = SimpleNamespace(inspect_var=self.inspect_var, edit_mode=self.edit_mode,
                                      edit_wifi=self.edit_wifi)
        self.plan = {"operation": "fake var transaction", "usb_port": "1-1", "partition": "var",
                     "size_bytes": 524288000, "before_sha256": "a" * 64,
                     "candidate_sha256": "b" * 64, "baseline_backup": "/tmp/fake-jibo-backups/var.img",
                     "baseline_sha256": "a" * 64}

    def devices(self):
        if self.state == "multiple":
            return [{"port": "1-1", "state": "dfu"}, {"port": "1-2", "state": "rcm"}]
        return [] if self.state == "none" else [{"port": "1-1", "state": self.state}]

    def tool(self, name):
        return "fake-" + name

    def dfu_alternatives(self, *args):
        return self.alts, ""

    def shofel_dfu_available(self):
        return True

    def _update_candidates(self, folder):
        return self.packages

    def _default_edit_path(self, source):
        return str(Path(source).with_name("edited-var.img"))

    def _edit(self, name, args, kwargs):
        self.calls.append((name, args, {k: v for k, v in kwargs.items() if k != "confirmation"}))
        if self.fail:
            raise self.fail
        assert callable(kwargs.get("confirmation")), "GUI must pass a review callback, not True/--yes"
        plan = dict(self.plan)
        if name.endswith("file_live"):
            plan = {"usb_port": "1-1", "changes": [
                {"partition": "var", "path": "/jibo/mode.json", "description": "Fake file change",
                 "before_size_bytes": 17, "candidate_size_bytes": 26}]}
        elif name == "flash_update":
            plan = {"package": str(args[0]), "version": "13.0.0", "usb_port": "1-1",
                    "var_policy": kwargs["var_policy"],
                    "rollback_backup": "var only; other partitions are restored from the package",
                    "partitions": [{"name": n, "bytes": 524288000} for n in ("rootfsA", "rootfsB", "services", "skills")]}
        elif name == "restore_partitions":
            plan = {"operation": "restore selected partitions", "usb_port": "1-1",
                    "source_manifest": str(args[0]), "partitions": [{"name": n, "bytes": 524288000} for n in args[1]]}
        if not kwargs["confirmation"](plan):
            return {"status": "cancelled", "message": "No fake write was made."}
        self.writes.append(name)
        result = {"status": "verified", "operation_directory": "/tmp/fake-operation"}
        if "mode" in name:
            result["mode"] = args[0]
        if "wifi" in name:
            result["ssid"] = args[1] if name.startswith("set_mode") else args[0]
        if name == "restore_partitions":
            result.update(partitions=list(args[1]), manifest="/tmp/fake-restore.json")
        return result

    def set_mode_file_live(self, *args, **kwargs):
        return self._edit("set_mode_file_live", args, kwargs)

    def set_mode_live(self, *args, **kwargs):
        return self._edit("set_mode_live", args, kwargs)

    def configure_wifi_file_live(self, *args, **kwargs):
        # Deliberately do not retain passwords in the fake's records.
        return self._edit("configure_wifi_file_live", (args[0], None, args[2]), kwargs)

    def configure_wifi_live(self, *args, **kwargs):
        return self._edit("configure_wifi_live", (args[0], None, args[2]), kwargs)

    def set_mode_wifi_file_live(self, *args, **kwargs):
        return self._edit("set_mode_wifi_file_live", (args[0], args[1], None, args[3]), kwargs)

    def set_mode_wifi_live(self, *args, **kwargs):
        return self._edit("set_mode_wifi_live", (args[0], args[1], None, args[3]), kwargs)

    def boot_network_status_live(self, *args, **kwargs):
        return self._edit("boot_network_status_live", args, kwargs)

    def flash_update(self, *args, **kwargs):
        return self._edit("flash_update", args, kwargs)

    def restore_partitions(self, *args, **kwargs):
        return self._edit("restore_partitions", args, kwargs)

    def write_var(self, *args, **kwargs):
        return self._edit("write_var", args, kwargs)

    def enter_shofel_dfu(self, **kwargs):
        assert kwargs == {"port": "1-1", "confirm_meerkat_rev02": True}
        self.calls.append(("enter_shofel_dfu", (), kwargs))
        self.state = "dfu"
        return {"state": "dfu"}

    def backup_var(self, **kwargs):
        self.calls.append(("backup_var", (), kwargs))
        if self.fail:
            raise self.fail
        return {"status": "backup complete", "image": "/tmp/fake-jibo-backups/var.img"}

    def available_backup_partitions(self, **kwargs):
        return "1-1", "fake-identity", {n: {"size_bytes": 524288000} for n in ("var", "services")}

    def backup_partitions(self, selected, **kwargs):
        self.calls.append(("backup_partitions", (selected,), kwargs))
        return {"status": "backup complete", "partitions": list(selected), "manifest": "/tmp/fake-set.json"}

    def saved_backups(self):
        return []

    def describe_backup_set(self, manifest):
        import json
        entries = json.loads(Path(manifest).read_text())["partitions"]
        return {"manifest": str(manifest), "state": "complete", "restorable": True,
                "partitions": [dict(e, present=True) for e in entries]}

    def probe_dfu_gpt(self, **kwargs):
        self.calls.append(("probe_dfu_gpt", (), kwargs))
        return {"partition_sizes_bytes": {"var": 524288000}}

    def inspect_var(self, source):
        self.calls.append(("inspect_var", (source,), {}))
        return {"mode": "developer", "wifi_configured": True, "wifi_network_count": 1}

    def edit_mode(self, *args):
        self.calls.append(("edit_mode", args, {}))
        return {"image": args[1]}

    def edit_wifi(self, *args):
        self.calls.append(("edit_wifi", args[:3], {}))
        return {"image": args[1]}


# -- hosted Phoenix images (synthetic stand-in for jibo_hosted_updates) -------
# Field names mirror jibo_hosted_updates' data classes so a renamed field fails here.
import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostedImage:
    id: str
    version: str
    base_release: str = "13.0.0"
    components: dict = field(default_factory=dict)
    size: int = 4000
    sha256: str = "c" * 64
    profile: str = "phoenix"
    strategy: str = "preserve"
    released_at: str = "2026-10-01"
    notes: str = ""
    offered: bool = True
    is_latest: bool = False
    compatible: bool = True
    incompatible_reason: object = None
    downloaded_path: object = None


@dataclass(frozen=True)
class Catalog:
    images: tuple
    latest_id: object
    fetched_at: str = "2026-10-12T12:00:00Z"
    from_cache: bool = False
    error: object = None


@dataclass(frozen=True)
class Progress:
    phase: str
    bytes: int
    total: object
    percent: object
    rate: object
    eta: object
    message: str = ""


class HostedError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CancelToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()


def sample_catalog(**changes):
    images = (HostedImage("phx-2", "2.0.0", is_latest=True, sha256="2" * 64),
              HostedImage("phx-1", "1.0.0", sha256="1" * 64, released_at="2026-09-01"))
    values = dict(images=images, latest_id="phx-2")
    values.update(changes)
    return Catalog(**values)


class FakeHosted:
    """Synthetic hosted backend: no network, no files, no USB."""
    HostedError = HostedError
    CancelToken = CancelToken

    def __init__(self, catalog=None, error=None, local=(), path="updates/phoenix-phx-2.tar.bz2"):
        self.catalog = sample_catalog() if catalog is None and error is None else catalog
        self.error = error
        self.local = list(local)
        self.path = path
        self.cached = None                # Catalog returned by cached_catalog, else offline
        self.download_error = None
        self.gate = None                  # threading.Event: hold the download until set/cancelled
        self.started = threading.Event()
        self.calls = []
        self.threads = []
        self.progress = None
        self.cancel = None

    def fetch_catalog(self, *, cache_dir=None, timeout=15.0, cancel=None):
        self.calls.append(("fetch_catalog", str(cache_dir), timeout))
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error
        return self.catalog

    def cached_catalog(self, *, cache_dir=None, error=None):
        self.calls.append(("cached_catalog", str(cache_dir)))
        if self.cached is not None:
            return Catalog(self.cached.images, self.cached.latest_id, from_cache=True, error=error)
        raise HostedError("offline", "No saved list")

    def local_hosted_images(self, dest_dir=None):
        self.calls.append(("local_hosted_images", str(dest_dir)))
        return list(self.local)

    def download_image(self, image_id, *, dest_dir=None, progress=None, cancel=None):
        self.calls.append(("download_image", image_id, str(dest_dir)))
        self.threads.append(threading.current_thread())
        self.progress, self.cancel = progress, cancel
        progress(Progress("download", 1000, 4000, 25.0, 500.0, 6.0))
        self.started.set()
        if self.gate is not None:
            deadline = time.monotonic() + 15.0   # a failing test must never strand this thread
            while not self.gate.wait(0.01):
                if cancel is not None and cancel.cancelled:
                    progress(Progress("canceled", 1000, 4000, 25.0, None, None, "canceled"))
                    raise HostedError("canceled", "Hosted operation canceled; no robot action performed")
                if time.monotonic() > deadline:
                    raise HostedError("http", "synthetic gate was never released")
        if self.download_error is not None:
            raise self.download_error
        progress(Progress("verify", 4000, 4000, 100.0, None, None))
        progress(Progress("done", 4000, 4000, 100.0, None, None))
        return self.path

    def hosted_var_policies(self, image):
        return [("keep-calibration", True, ""), ("without-calibration", True, "")]

    def flash_download_session(self, image_id, *, dest_dir=None):
        from contextlib import contextmanager
        from types import SimpleNamespace
        @contextmanager
        def session():
            yield SimpleNamespace(directory=Path(dest_dir) / '.hosted-flash' / image_id,
                                  complete=False, interrupted=False)
        return session()


class FakeHostedApi(FakeApi):
    """Device fake with a hosted backend and the public flash_hosted_update seam."""

    def __init__(self, hosted=None, state="dfu", packages=()):
        super().__init__(state=state)
        self.hosted = hosted if hosted is not None else FakeHosted()
        self.packages = [Path(p) for p in packages]
        self.fail_flashes = 0             # raise a recorded failure before this many flashes

    def flash_hosted_update(self, package_path, *, var_policy="keep-calibration", port=None, dfu_util=None,
                            out=None, confirmation=None, dry_run=False, resume_from=None, unsafe_without_calibration=False, local_work=None,
                            verify_readback=False):
        self.calls.append(("flash_hosted_update", (str(package_path),),
                           {"var_policy": var_policy, "port": port, "resume_from": resume_from}))
        assert callable(confirmation), "hosted installs must pass a review callback"
        if self.fail_flashes:
            self.fail_flashes -= 1
            # Same shape as jibo_dfu.flash_update's failure (B 5755f3f jibo_dfu.py:2376-2380).
            raise RuntimeError("USB transfer stopped.\nUpdate record: updates/op/update-manifest.json")
        plan = {"package": str(package_path), "version": "13.0.0", "usb_port": "1-1",
                "var_policy": var_policy,
                "partitions": [{"name": n, "bytes": 524288000} for n in ("rootfsA", "rootfsB")],
                "phoenix": {"id": "phx-2", "profile": "phoenix", "base_release": "13.0.0",
                            "scope": "Userspace filesystems only; no boot0/boot1, BCT, GPT",
                            "var_policy": var_policy,
                            "archive_sha256": "2" * 64, "var_write_bytes": 524288000,
                            "var_candidate_sha256": "d" * 64}}
        if not confirmation(plan):
            return {**plan, "status": "cancelled", "manifest": "updates/op/flash-update.json"}
        self.writes.append("flash_hosted_update")
        # Same keys as jibo_dfu.flash_update's success return (B 5755f3f jibo_dfu.py:2369-2375).
        return {"status": "verified", "package": str(package_path), "var_policy": plan["var_policy"],
                "transferred_partitions": ["rootfsA", "rootfsB", "var"],
                "verified_partitions": ["rootfsA", "rootfsB", "var"],
                "manifest": "updates/op/flash-update.json", "backups": {}, "resumed_from": None}

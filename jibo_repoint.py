"""Hash-pinned, in-place first-OTA bridge for stock Jibo Release 13.0.0.

This is intentionally an experimental DFU workflow, not a complete firmware
repoint. The OTA image supplies the permanent trust store and all services.
"""

import hashlib
import json
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request

import jibo_dfu as dfu


CA_PATH = "/usr/share/ca-certificates/mozilla/DST_Root_CA_X3.crt"
CA_ASSET = Path(__file__).resolve().parent / "assets/isrg-root-x1.pem"
CA_SHA256 = "22b557a27055b33606b6559f37703928d3e4ad79f110b407d04986e1843543d1"
STOCK_SHA256 = {
    "ca": "139a5e4a4e0fa505378c72c5f700934ce8333f4e6b1b508886c4b0eb14f4be99",
    "region": "f1514e59a030b87da7aac8ac5e9b56f5fc0e4dd034e12ad2f1eadb1cca904bcb",
    "client": "c3511dbc55c8a9ec3ac74a675a1245306b55c67fab65a3ecfe896ed01689997a",
    "downloader": "33f6db1496baa3abd506a2ba9dad9b5cdf7567341e3e292e42cb3ed6f016003c",
    "backup": "d17fbf4150dee58a988fe5ee72071d4515ef74f29876215bf66de2601e33e522",
    "restore": "b5e7ec06c4ea72b641b8738b789a389575e250b152b3b6ecddd952d593e05ee6",
}

# Each path is inside its named partition, not its runtime mount point.
ROOT_CLIENTS = (
    "/usr/lib/node_modules/@jibo/jibo-server-client",
    "/usr/lib/node_modules/@jibo/jibo-log-client/node_modules/@jibo/jibo-server-client",
    "/usr/lib/node_modules/@jibo/jibo-ota-updater/node_modules/@jibo/jibo-server-client",
)
SERVICE_CLIENT = "/bin/jibo-ssm/node_modules/@jibo/jibo-server-client"
OOBE_CLIENT = "/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client"


def patch_manifest():
    """Return all existing files that must be patched on both rootfs slots."""
    files = []
    for partition in ("rootfsA", "rootfsB"):
        files.append((partition, CA_PATH, "ca"))
        for base in ROOT_CLIENTS:
            files.append((partition, base + "/lib/region_config.json", "region"))
            files.append((partition, base + "/lib/http/node.js", "client"))
        files.append((partition, "/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js", "downloader"))
    for partition, base in (("services", SERVICE_CLIENT), ("skills", OOBE_CLIENT)):
        files.append((partition, base + "/lib/region_config.json", "region"))
        files.append((partition, base + "/lib/http/node.js", "client"))
    files.extend((("services", "/bin/jibo-system-backup", "backup"),
                  ("services", "/bin/jibo-system-restore", "restore")))
    return tuple(files)


def _replace_once(source, before, after):
    if source.count(before) != 1:
        raise dfu.DfuError("Stock file does not contain the expected patch anchor exactly once.")
    return source.replace(before, after)


def patched_bytes(kind, source):
    """Transform one exact stock file; never disable TLS verification."""
    if kind == "ca":
        certificate = CA_ASSET.read_bytes()
        if hashlib.sha256(certificate).hexdigest() != CA_SHA256:
            raise dfu.DfuError("The bundled ISRG Root X1 certificate failed its checksum.")
        return certificate
    if kind == "region":
        if source.count(b"jibo.com") != 5:
            raise dfu.DfuError("The stock region config has an unexpected endpoint count.")
        return source.replace(b"jibo.com", b"jibo.io")
    if kind == "client":
        return _replace_once(source,
            b"new https.Agent({rejectUnauthorized: true});",
            b'new https.Agent({rejectUnauthorized: true, ca: fs.readFileSync("' +
            CA_PATH.encode() + b'")});')
    if kind == "downloader":
        return _replace_once(source,
            b"http.get(argv.url, function(res) {",
            b'http.get(argv.url.startsWith("https:") ? Object.assign(require("url").parse(argv.url), {ca: fs.readFileSync("' +
            CA_PATH.encode() + b'")}) : argv.url, function(res) {')
    if kind == "backup":
        source = _replace_once(source, b"            method: 'PUT',\n",
            b"            method: 'PUT',\n            ca: fs.readFileSync('" + CA_PATH.encode() + b"'),\n")
        return _replace_once(source,
            b'        throw new Error("Missing argument: keydir not specified");\n    }',
            b'        throw new Error("Missing argument: keydir not specified");\n    }\n'
            b'    if (!fs.existsSync(argv.keydir)) fs.mkdirSync(argv.keydir, 0o700);')
    if kind == "restore":
        return _replace_once(source,
            b"https.get(downloadUrl, callbackDownload)",
            b"https.get(Object.assign(require('url').parse(downloadUrl), {ca: fs.readFileSync('" +
            CA_PATH.encode() + b"')}), callbackDownload)")
    raise dfu.DfuError("Unknown repoint patch kind: " + kind)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def plan(dfu_util, port, names):
    """Read all targets and reject an unknown build before any device write."""
    if dfu.FILE_LEVEL_MARKER_V2 not in names:
        raise dfu.DfuError("Stock 13.0.0 uses multi-block direct pointers; repoint requires the opt-in jibo-file-v2 loader.")
    changes = []
    with tempfile.TemporaryDirectory(prefix="jibo-repoint-read-") as directory:
        for partition, path, kind in patch_manifest():
            source = dfu._read_partition_file_rpc(dfu_util, port, partition, path, directory)
            current_sha = _sha(source)
            if current_sha == STOCK_SHA256[kind]:
                candidate = patched_bytes(kind, source)
                if _sha(candidate) != PATCHED_SHA256[kind]:
                    raise dfu.DfuError("The {} transform did not match its pinned output at {}:{}; no writes attempted."
                                       .format(kind, partition, path))
                stat = dfu._stat_partition_file_rpc(dfu_util, port, partition, path, directory)
                if len(candidate) > stat["allocated_bytes"]:
                    raise dfu.DfuError("{} would need {} bytes but has only {} allocated; no writes attempted."
                                       .format(path, len(candidate), stat["allocated_bytes"]))
                changes.append((partition, path, candidate))
            elif current_sha == PATCHED_SHA256[kind]:
                continue
            else:
                raise dfu.DfuError("Unsupported {} file at {}:{} (SHA-256 {}). No writes attempted."
                                   .format(kind, partition, path, current_sha))
    return changes


# Exact output hashes were verified offline against the official 13.0.0 images.
PATCHED_SHA256 = {
    "ca": CA_SHA256,
    "region": "d0a5b081b05a0ff717e4e57623505d88b83fce053b438f5b5e364a3e534d278e",
    "client": "fa2ae92ca9b2113129017c29b592e13e3376db326cb2e5d083642fd60633d935",
    "downloader": "e71832017a349e898bb32739b645ce8ccfdbc753bac086e8dfc2d71731ef0ee5",
    "backup": "77a8bca57d3c4a70ee15ef4d874cef3ec1329219eed4485f3509d3ee1b790a2f",
    "restore": "3b210563f128a9c8f8834be717b4b5605bd3aa61f39a0d1c2b47073f2629c5ed",
}


def _existing_credentials(dfu_util, port):
    """Read and validate private credentials before any device write."""
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-repoint-credentials-") as directory:
            payload = dfu._read_partition_file_rpc(dfu_util, port, "var",
                                                   "/jibo/credentials.json", directory)
    except dfu.DfuError as exc:
        if "status 2" in str(exc):
            return None
        raise
    try:
        credentials = json.loads(payload)
        access_key = credentials["accessKeyId"]
        secret_key = credentials["secretAccessKey"]
        region = credentials["region"]
    except (ValueError, KeyError, TypeError) as exc:
        raise dfu.DfuError("Existing robot credentials are incomplete; adoption was not attempted.") from exc
    if (not isinstance(access_key, str) or not re.fullmatch(r"[A-Za-z0-9]{20}", access_key) or
            not isinstance(secret_key, str) or not re.fullmatch(r"[A-Za-z0-9]{40}", secret_key)):
        raise dfu.DfuError("Existing robot credentials are not in the format accepted by jibo.io.")
    if region not in {"api", "stg-entrypoint", "alpha-entrypoint", "dev-entrypoint", "preprod-entrypoint"}:
        raise dfu.DfuError("The existing credential region is not an approved jibo.io host.")
    return {"accessKeyId": access_key, "secretAccessKey": secret_key, "region": region}


def _adopt_existing(credentials, claim_code=None):
    """Register existing credentials without printing or saving either secret."""
    if credentials is None:
        return "No existing credentials: complete QR setup in the jibo.io portal, then install the OTA."
    region = credentials["region"]
    body = {"accessKeyId": credentials["accessKeyId"],
            "secretAccessKey": credentials["secretAccessKey"]}
    if claim_code:
        body["claimCode"] = claim_code
    request = urllib.request.Request("https://{}.jibo.io/api/adopt-robot".format(region),
        data=json.dumps(body).encode(), headers={"content-type": "application/json",
                                                 "x-phoenix-api-client": "jibo-dfu-repoint"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    except (urllib.error.URLError, ValueError) as exc:
        raise dfu.DfuError("The robot was repointed but server adoption failed; keep it in DFU and retry. "
                           "No credentials were printed or saved.") from exc
    if not isinstance(result, dict) or not result.get("adopted"):
        raise dfu.DfuError("The robot was repointed but the server did not confirm adoption; keep it in DFU.")
    if claim_code and not (result.get("linked") or result.get("alreadyLinked")):
        return "Robot adopted, but account linking was not confirmed; retry with a fresh portal claim code."
    return "Existing robot credentials adopted" + (" and linked to your account." if claim_code else "; claim it in the portal if needed.")


def repoint_jibo_io(port=None, dfu_util=None, out=None, confirmation=None, claim_code=None, dry_run=False):
    dfu_util = dfu_util or dfu.tool("dfu-util")
    partitions = ("rootfsA", "rootfsB", "services", "skills", "var")
    port, names, _, _ = dfu._file_loader_context(port, dfu_util, partitions, not dry_run)
    changes = plan(dfu_util, port, names)
    credentials = _existing_credentials(dfu_util, port)
    if claim_code and credentials is None:
        raise dfu.DfuError("This robot has no existing credentials; use the portal QR/OOBE flow instead of a claim code.")
    summary = {"status": "plan" if dry_run else "pending", "files_to_change": len(changes),
               "existing_credentials": credentials is not None,
               "paths": [{"partition": part, "path": path, "sha256": _sha(content)}
                         for part, path, content in changes]}
    if dry_run:
        return summary
    if changes:
        transaction = dfu.FileTransaction(tuple(dict.fromkeys(part for part, _, _ in changes)),
                                          port, dfu_util, out)
        for part, path, content in changes:
            transaction.replace(part, path, content)
        result = transaction.commit(confirmation)
        if result["status"] != "verified":
            return result
        summary["transaction"] = result["operation_directory"]
    summary["status"] = "repointed-for-ota"
    try:
        summary["adoption"] = _adopt_existing(credentials, claim_code)
    except dfu.DfuError as exc:
        raise dfu.DfuError("{} Transaction record: {}".format(
            exc, summary.get("transaction", "no new writes"))) from exc
    summary["next_step"] = "Exit DFU, complete QR setup if needed, then install the jibo.io OTA immediately; this bridge alone is not a complete migration."
    return summary

"""Hash-pinned, in-place first-OTA bridge for recognized stock Jibo images.

This is intentionally an experimental DFU workflow, not a complete firmware
repoint. The OTA image supplies the permanent trust store and all services.
"""

import hashlib
import json
from pathlib import Path
import re
import sys
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
}

# Hashes are from the official 5.4.0 EFT and 5.4.2 production images. The
# common client, region and CA files are byte-identical to Release 13.0.0.
STOCK_54_SHA256 = {
    "downloader": "447a2a5598ec13ea46367207ea594efd6774d785c97d5322200e5809d6d9acb2",
}
STOCK_33_SHA256 = {
    "client": "81533de391dfba88fc40bedfc63ea30a77f8d032f9a8c23196db4cb3a44fa89b",
}

# Each path is inside its named partition, not its runtime mount point.
ROOT_CLIENTS = (
    "/usr/lib/node_modules/@jibo/jibo-server-client",
    "/usr/lib/node_modules/@jibo/jibo-log-client/node_modules/@jibo/jibo-server-client",
    "/usr/lib/node_modules/@jibo/jibo-ota-updater/node_modules/@jibo/jibo-server-client",
)
SERVICE_CLIENT = "/bin/jibo-ssm/node_modules/@jibo/jibo-server-client"
OOBE_CLIENT = "/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client"
EARLY_SKILL_CLIENTS = tuple(
    "/jibo/Jibo/Skills/@be/be/node_modules/" + nested + "@jibo/jibo-server-client"
    for nested in ("", "@be/ifttt/node_modules/", "@be/settings/node_modules/",
                   "@be/surprises-ota/node_modules/")
)


def patch_manifest(rootfs_profiles=None):
    """Return the exact paths required by the detected rootfs layouts."""
    rootfs_profiles = rootfs_profiles or {"rootfsA": "13.0", "rootfsB": "13.0"}
    files = []
    for partition in ("rootfsA", "rootfsB"):
        files.append((partition, CA_PATH, "ca"))
        clients = ROOT_CLIENTS if rootfs_profiles[partition] == "13.0" else ROOT_CLIENTS[:1]
        for base in clients:
            files.append((partition, base + "/lib/region_config.json", "region"))
            files.append((partition, base + "/lib/http/node.js", "client"))
        files.append((partition, "/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js", "downloader"))
    for partition, base in (("services", SERVICE_CLIENT), ("skills", OOBE_CLIENT)):
        files.append((partition, base + "/lib/region_config.json", "region"))
        files.append((partition, base + "/lib/http/node.js", "client"))
    for base in EARLY_SKILL_CLIENTS:
        files.append(("skills", base + "/lib/region_config.json", "region"))
        files.append(("skills", base + "/lib/http/node.js", "client"))
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
            b"options.agent = this.sslAgent();",
            b'options.agent = /(^|\\.)jibo\\.io$/.test(endpoint.hostname || "") ? '
            b'(AWS.NodeHttpClient.jiboIoSslAgent || '
            b'(AWS.NodeHttpClient.jiboIoSslAgent = new (require("https").Agent)'
            b'({rejectUnauthorized: true, ca: require("fs").readFileSync("' +
            CA_PATH.encode() + b'")}))) : this.sslAgent();')
    if kind == "downloader":
        return _replace_once(source,
            b"http.get(argv.url, function(res) {",
            b'http.get(argv.url.startsWith("https:") && /(^|\\.)jibo\\.io$/.test(require("url").parse(argv.url).hostname || "") ? Object.assign(require("url").parse(argv.url), {ca: fs.readFileSync("' +
            CA_PATH.encode() + b'")}) : argv.url, function(res) {')
    raise dfu.DfuError("Unknown repoint patch kind: " + kind)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def plan(dfu_util, port, names, quiet=False):
    """Detect known layouts and reject an unknown build before any device write."""
    if dfu.FILE_LEVEL_MARKER_V2 not in names:
        raise dfu.DfuError("Repoint requires the opt-in jibo-file-v2 loader for stock client scripts.")
    changes = []
    with tempfile.TemporaryDirectory(prefix="jibo-repoint-read-") as directory:
        rootfs_profiles = {}
        observed = {}
        for partition in ("rootfsA", "rootfsB"):
            profile, source, client = _rootfs_profile(dfu_util, port, partition, directory, quiet)
            rootfs_profiles[partition] = profile
            observed[(partition, "/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js")] = source
            observed[(partition, ROOT_CLIENTS[0] + "/lib/http/node.js")] = client
        optional = set()
        for base in EARLY_SKILL_CLIENTS:
            optional.add(("skills", base + "/lib/region_config.json"))
            optional.add(("skills", base + "/lib/http/node.js"))
        optional_found = set()
        targets = patch_manifest(rootfs_profiles)
        for index, (partition, path, kind) in enumerate(targets, 1):
            if quiet:
                filled = int(20 * (index - 1) / len(targets))
                print("\rChecking OTA files [{}{}] {}/{}".format(
                    "#" * filled, "-" * (20 - filled), index - 1, len(targets)),
                    end="", file=sys.stderr, flush=True)
            source = observed.get((partition, path))
            if source is None:
                try:
                    source = dfu._read_partition_file_rpc(dfu_util, port, partition, path, directory,
                                                          quiet=quiet)
                except dfu.FileRpcStatusError as exc:
                    if exc.status == 2 and (partition, path) in optional:
                        continue
                    raise
            if (partition, path) in optional:
                optional_found.add((partition, path))
            current_sha = _sha(source)
            output_sha = _pinned_output(kind, current_sha)
            if output_sha is not None:
                candidate = patched_bytes(kind, source)
                if _sha(candidate) != output_sha:
                    raise dfu.DfuError("The {} transform did not match its pinned output at {}:{}; no writes attempted."
                                       .format(kind, partition, path))
                stat = dfu._stat_partition_file_rpc(dfu_util, port, partition, path, directory,
                                                    quiet=quiet)
                if len(candidate) > stat["allocated_bytes"]:
                    raise dfu.DfuError("{} would need {} bytes but has only {} allocated; no writes attempted."
                                       .format(path, len(candidate), stat["allocated_bytes"]))
                changes.append((partition, path, candidate))
            elif _already_patched(kind, current_sha):
                continue
            else:
                raise dfu.DfuError("Unsupported {} file at {}:{} (SHA-256 {}). No writes attempted."
                                   .format(kind, partition, path, current_sha))
        if quiet:
            print("\rChecking OTA files [{}] {}/{}".format(
                "#" * 20, len(targets), len(targets)), file=sys.stderr, flush=True)
        for base in EARLY_SKILL_CLIENTS:
            pair = {("skills", base + "/lib/region_config.json"),
                    ("skills", base + "/lib/http/node.js")}
            if len(pair & optional_found) == 1:
                raise dfu.DfuError("An archived skills client is only partly present at {}. No writes attempted."
                                   .format(base))
    return {"changes": changes, "rootfs_profiles": rootfs_profiles}


# Exact output hashes were verified offline against the official 13.0.0 images.
PATCHED_SHA256 = {
    "ca": CA_SHA256,
    "region": "d0a5b081b05a0ff717e4e57623505d88b83fce053b438f5b5e364a3e534d278e",
    "client": "ed9e7db8e584d2728f6f00bdc234f0fb3e43c6ff74c0c59f89682e28fe3a397d",
    "downloader": "71716f3a0e9f5f17e30db67193cb46776ca2c4e0cc2f1a32d0540bd8de497338",
}
PATCHED_54_SHA256 = {
    "downloader": "bc8342a66662d981067a6a6688ac6147c0bca628cd37fc9a61161139acae2ed0",
}
PATCHED_33_SHA256 = {
    "client": "e01ec852303530a6d0acc55a9b8f8eb2f2a801634f7c5c85b4965da772152ec5",
}


def _pinned_output(kind, stock_hash):
    if stock_hash == STOCK_SHA256[kind]:
        return PATCHED_SHA256[kind]
    if stock_hash == STOCK_54_SHA256.get(kind):
        return PATCHED_54_SHA256[kind]
    if stock_hash == STOCK_33_SHA256.get(kind):
        return PATCHED_33_SHA256[kind]
    return None


def _already_patched(kind, current_hash):
    return current_hash in (PATCHED_SHA256[kind], PATCHED_54_SHA256.get(kind),
                            PATCHED_33_SHA256.get(kind))


def _rootfs_profile(dfu_util, port, partition, directory, quiet=False):
    path = "/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js"
    source = dfu._read_partition_file_rpc(dfu_util, port, partition, path, directory,
                                          quiet=quiet)
    digest = _sha(source)
    client_path = ROOT_CLIENTS[0] + "/lib/http/node.js"
    client = dfu._read_partition_file_rpc(dfu_util, port, partition, client_path, directory,
                                          quiet=quiet)
    client_digest = _sha(client)
    current_client = client_digest in (STOCK_SHA256["client"], PATCHED_SHA256["client"])
    early_client = client_digest in (STOCK_33_SHA256["client"], PATCHED_33_SHA256["client"])
    if digest in (STOCK_SHA256["downloader"], PATCHED_SHA256["downloader"]):
        profile = "13.0" if current_client else "3.3" if early_client else None
    elif digest in (STOCK_54_SHA256["downloader"], PATCHED_54_SHA256["downloader"]):
        profile = "5.4" if current_client else None
    else:
        profile = None
    if profile is None:
        raise dfu.DfuError("Unsupported OTA client/downloader pair in {} (SHA-256 {}, {}). No writes attempted."
                           .format(partition, client_digest, digest))
    return profile, source, client


def _existing_credentials(dfu_util, port):
    """Read and validate private credentials before any device write."""
    try:
        with tempfile.TemporaryDirectory(prefix="jibo-repoint-credentials-") as directory:
            payload = dfu._read_partition_file_rpc(dfu_util, port, "var",
                                                   "/jibo/credentials.json", directory)
    except dfu.FileRpcStatusError as exc:
        if exc.status == 2:
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


def repoint_jibo_io(port=None, dfu_util=None, out=None, confirmation=None,
                   claim_code=None, dry_run=False, adopt_existing=False, guided=False):
    dfu_util = dfu_util or dfu.tool("dfu-util")
    partitions = ("rootfsA", "rootfsB", "services", "skills", "var")
    port, names, _, _ = dfu._file_loader_context(port, dfu_util, partitions, not dry_run)
    if guided:
        print("Checking installed OTA files and available space…", flush=True)
    try:
        preflight = plan(dfu_util, port, names, quiet=guided)
    except Exception:
        if guided:
            print(file=sys.stderr, flush=True)
        raise
    changes = preflight["changes"]
    credentials = _existing_credentials(dfu_util, port) if adopt_existing or claim_code else None
    if claim_code and credentials is None:
        raise dfu.DfuError("This robot has no existing credentials; use the portal QR/OOBE flow instead of a claim code.")
    summary = {"status": "plan" if dry_run else "pending", "files_to_change": len(changes),
               "adoption_requested": bool(adopt_existing or claim_code),
               "rootfs_profiles": preflight["rootfs_profiles"],
               "paths": [{"partition": part, "path": path, "sha256": _sha(content)}
                         for part, path, content in changes]}
    if dry_run:
        return summary
    if changes:
        transaction = dfu.FileTransaction(tuple(dict.fromkeys(part for part, _, _ in changes)),
                                          port, dfu_util, out, guided=guided)
        for part, path, content in changes:
            transaction.replace(part, path, content)
        result = transaction.commit(confirmation)
        if result["status"] != "verified":
            return result
        summary["transaction"] = result["operation_directory"]
    summary["status"] = "repointed-for-ota"
    if adopt_existing or claim_code:
        try:
            summary["adoption"] = _adopt_existing(credentials, claim_code)
        except dfu.DfuError as exc:
            raise dfu.DfuError("{} Transaction record: {}".format(
                exc, summary.get("transaction", "no new writes"))) from exc
    summary["next_step"] = "Exit DFU, complete QR setup if needed, then install the jibo.io OTA immediately; this bridge alone is not a complete migration."
    return summary

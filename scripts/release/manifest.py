"""Hash every runtime file, excluding the manifest itself."""
import hashlib
import json
from pathlib import Path


def write_manifest(root, revision):
    root = Path(root)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'release-manifest.json'}
    (root / 'release-manifest.json').write_text(json.dumps({'revision': revision, 'files': files}, indent=2) + '\n')


def verify_manifest(root):
    root = Path(root)
    manifest = json.loads((root / 'release-manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Bundled file failed integrity check: ' + name)
    return manifest

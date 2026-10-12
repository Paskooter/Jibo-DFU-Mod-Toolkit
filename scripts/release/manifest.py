"""Hash every runtime file, excluding the manifest itself."""
import hashlib
import json
import os
from pathlib import Path


def write_manifest(root, revision, exclude=()):
    root = Path(root)
    excluded = set(exclude)
    files = {}
    for directory, _, names in os.walk(root, followlinks=True):
        for name in sorted(names):
            p = Path(directory) / name
            relative = p.relative_to(root).as_posix()
            if p.is_file() and p.name != 'release-manifest.json' and relative not in excluded:
                files[relative] = hashlib.sha256(p.read_bytes()).hexdigest()
    (root / 'release-manifest.json').write_text(json.dumps({'revision': revision, 'files': files,
        'excluded_native_files': sorted(excluded)}, indent=2) + '\n')


def verify_manifest(root):
    root = Path(root)
    manifest = json.loads((root / 'release-manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Bundled file failed integrity check: ' + name)
    return manifest

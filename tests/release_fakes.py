"""Synthetic native build boundary for command-assembly tests only."""
import json
from pathlib import Path
import struct


def windows_build_run(calls, root):
    revision = 'a' * 40
    (root / 'revision.txt').write_text(revision)

    def run(*args, **kwargs):
        calls.append(args)
        if '--noconsole' in args:
            pe = bytearray(512)
            pe[:2] = b'MZ'
            struct.pack_into('<I', pe, 0x3c, 128)
            pe[128:132] = b'PE\x00\x00'
            struct.pack_into('<H', pe, 128 + 24 + 68, 2)
            (root / 'release-out' / 'Jibo-DFU-Windows-x64.exe').write_bytes(pe)
        if '--output' in args:
            receipt = Path(args[-1])
            receipt.write_text('Jibo DFU Toolkit (build aaaaaaa)\n' +
                               json.dumps({'revision': revision}) + '\n')
    return run

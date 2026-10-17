#!/usr/bin/env python3
"""Linux-only support-runner control backend, NOT a native dependency or proof.

A private subreaper owns the fixture even after its immediate parent exits or
changes sessions. It admits no fixture before stdin release. EOF is cleanup.
Only its actual direct children, revalidated across pidfd acquisition, authorize
signals. Receipt PIDs are never consumed. Windows uses the runner's own Job.
"""
import ctypes
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time


def publish(path, value):
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def direct_children():
    return [int(pid) for pid in Path('/proc/self/task', str(os.getpid()), 'children').read_text().split()]


def identity(pid):
    text = Path('/proc', str(pid), 'stat').read_text()
    fields = text[text.rindex(')') + 2:].split()
    return int(fields[1]), int(fields[19])


def main(root):
    root = Path(root)
    request = json.loads((root / 'portable-request.json').read_text())
    assert sys.platform == 'linux' and hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    # Prove the syscall exists before admission, not after running the payload.
    fd = os.pidfd_open(os.getpid())
    os.close(fd)
    publish(root / 'containment-ready.json', dict(kind='linux-test-subreaper', ready=True))
    retained = {}
    process = None
    exit_code = None
    cleanup = []
    deadline = time.monotonic() + request['total_seconds']
    def adopt():
        for pid in direct_children():
            if pid in retained:
                continue
            try:
                before = identity(pid)
                handle = os.pidfd_open(pid)
                after = identity(pid)
            except ProcessLookupError:
                continue
            if before != after or after[0] != os.getpid():
                os.close(handle)
                raise RuntimeError('owned child identity changed during admission')
            retained[pid] = handle
    def finish_tree():
        # A killed parent can reveal another generation, so adopt/reap to empty.
        while True:
            adopt()
            for pid, handle in list(retained.items()):
                running = not bool(select.select([handle], [], [], 0)[0])
                if running:
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                if not select.select([handle], [], [], max(0, deadline - time.monotonic()))[0]:
                    raise TimeoutError('owned portable cleanup deadline')
                if process is not None and pid == process.pid:
                    process.wait(timeout=max(.001, deadline - time.monotonic()))
                else:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    if waited != pid:
                        raise RuntimeError('owned exited child not reapable')
                os.close(handle)
                del retained[pid]
                cleanup.append(dict(pid=pid, signalled=running, authority='actual direct child + retained pidfd'))
            if not direct_children():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('owned adoption deadline')
    try:
        # Bound a missing release too. No payload exists while this is waiting.
        if not select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))[0]:
            raise TimeoutError('owned release deadline')
        if sys.stdin.readline().strip() != request['nonce']:
            return 121
        process = subprocess.Popen([request['binary'], *shlex.split(request['arguments'])],
                                   cwd=request['working_directory'], stdin=subprocess.DEVNULL)
        adopt()
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise TimeoutError('owned payload deadline')
            if select.select([sys.stdin], [], [], .01)[0]:
                sys.stdin.readline()  # explicit stop OR supervisor EOF => cleanup
                break
        exit_code = process.poll()
    finally:
        finish_tree()
        if process is not None:
            exit_code = process.returncode
        publish(root / 'containment.json', dict(kind='linux-test-subreaper', tree_empty=True, owned_remaining=0,
                                               fixture_exit_code=exit_code, cleanup=cleanup))
    return 0 if exit_code is None else exit_code if exit_code >= 0 else 128 - exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))

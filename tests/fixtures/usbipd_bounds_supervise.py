"""Dedicated Linux subreaper for one offline PowerShell fixture invocation.

Only adopted *direct children of this newly created supervisor* are signalled,
using retained pidfds. Journals/PID files never supply signalling authority.
The product-result JSON is captured before this test-only final cleanup.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot establish fixture subreaper')
    child = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    observations = []
    states = []
    timed_out = False
    try:
        out, err = child.communicate(timeout=float(sys.argv[1]))
    except subprocess.TimeoutExpired:
        timed_out = True
        out, err = '', 'fixture outer deadline exceeded'
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=2)
        until = time.monotonic() + 2
        children_file = Path('/proc/self/task') / str(os.getpid()) / 'children'
        while time.monotonic() < until:
            direct = [int(value) for value in children_file.read_text().split()]
            if not direct:
                break
            for pid in direct:
                fd = None
                try:
                    fd = os.pidfd_open(pid)
                    stat = Path('/proc') / str(pid) / 'stat'
                    fields = stat.read_text().rsplit(')', 1)[1].split()
                    if int(fields[1]) != os.getpid():
                        raise RuntimeError('adopted child identity changed')
                    states.append({'pid': pid, 'state_before_test_cleanup': fields[0]})
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    observations.append(pid)
                except ProcessLookupError:
                    pass
                finally:
                    if fd is not None:
                        os.close(fd)
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            time.sleep(.01)
        remaining = children_file.read_text().split()
        print(json.dumps({'supervisor': os.getpid(), 'launched': child.pid,
                          'adopted_pidfd_cleanup': sorted(set(observations)),
                          'adopted_states': states,
                          'remaining_direct_children': remaining,
                          'outer_timeout': timed_out}), file=sys.stderr)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return 124 if timed_out or remaining else child.returncode


if __name__ == '__main__':
    sys.exit(main())

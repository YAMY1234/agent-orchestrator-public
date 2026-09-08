"""Run one command behind a real controlling pseudo-terminal.

The dashboard itself is multi-threaded, so assigning a controlling terminal
from ``preexec_fn`` would be unsafe.  This tiny single-purpose subprocess owns
the ``forkpty`` instead and forwards bytes over ordinary stdin/stdout pipes.
Interactive programs such as OpenSSH can then use ``/dev/tty`` normally while
their prompts remain observable by the parent process.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m agent_orchestrator.pty_bridge COMMAND ...", file=sys.stderr)
        return 2
    pid, master_fd = pty.fork()
    if pid == 0:  # pragma: no cover - replaced by the requested executable
        os.execvp(sys.argv[1], sys.argv[1:])

    def forward_signal(signum, _frame):
        try:
            os.killpg(pid, signum)
        except OSError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward_signal)

    stdin_open = True
    master_open = True
    status = 0
    try:
        while True:
            readable_fds = ([master_fd] if master_open else [])
            if stdin_open:
                readable_fds.append(sys.stdin.fileno())
            readable, _, _ = select.select(readable_fds, [], [], 0.2)
            if master_open and master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    os.write(sys.stdout.fileno(), data)
                else:
                    master_open = False
            if stdin_open and sys.stdin.fileno() in readable:
                data = os.read(sys.stdin.fileno(), 4096)
                if data and master_open:
                    os.write(master_fd, data)
                else:
                    stdin_open = False

            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                # Drain the final prompt/status bytes before exiting.  Unlike
                # pty.spawn(), do not wait for parent stdin to close after the
                # child is already gone.
                while master_open:
                    ready, _, _ = select.select([master_fd], [], [], 0)
                    if not ready:
                        break
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    try:
        return os.waitstatus_to_exitcode(status)
    except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
        return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128


if __name__ == "__main__":
    raise SystemExit(main())

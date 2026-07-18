#!/usr/bin/env python3
"""Own and reap one journal provider process group on Linux."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import signal
import sys


_PR_SET_CHILD_SUBREAPER = 36
_MANAGED_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)


def _process_identity(pid: int, token: str) -> str | None:
    """Return start time only for this live tokenized session leader."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None

    closing_paren = stat.rfind(") ")
    if closing_paren < 0:
        return None
    fields = stat[closing_paren + 2 :].split()
    if len(fields) < 20:
        return None
    state, process_group, session_id, starttime = (
        fields[0],
        fields[2],
        fields[3],
        fields[19],
    )
    if state == "Z" or process_group != str(pid) or session_id != str(pid):
        return None

    arguments = [item.decode(errors="surrogateescape") for item in command if item]
    expected_script = str(Path(__file__).resolve())
    try:
        token_index = arguments.index("--ownership-token")
    except ValueError:
        return None
    if token_index + 1 >= len(arguments) or arguments[token_index + 1] != token:
        return None
    if not any(
        argument == expected_script
        for argument in arguments[: token_index + 1]
    ):
        return None
    return starttime


def _verify_owner(pid: int, token: str, expected_starttime: str | None) -> bool:
    actual_starttime = _process_identity(pid, token)
    return actual_starttime is not None and (
        expected_starttime is None or actual_starttime == expected_starttime
    )


def _set_child_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _exit_status(wait_status: int) -> int:
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status)
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return 1


def _write_ready_file(path: Path, token: str) -> None:
    starttime = _process_identity(os.getpid(), token)
    if starttime is None:
        raise RuntimeError("supervisor ownership identity is not ready")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(starttime + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _supervise(command: list[str], token: str, ready_file: Path) -> int:
    if not command:
        raise ValueError("supervisor command must not be empty")

    signal.pthread_sigmask(signal.SIG_BLOCK, _MANAGED_SIGNALS)
    for managed_signal in _MANAGED_SIGNALS:
        signal.signal(managed_signal, signal.SIG_DFL)
    _set_child_subreaper()

    child_pid = os.fork()
    if child_pid == 0:
        try:
            for managed_signal in _MANAGED_SIGNALS:
                signal.signal(managed_signal, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _MANAGED_SIGNALS)
            os.execvp(command[0], command)
        except BaseException as exc:
            print(f"journal worker exec failed: {exc}", file=sys.stderr)
            os._exit(127)

    shutdown_status = 0

    def record_shutdown(signum: int, _frame) -> None:
        nonlocal shutdown_status
        if shutdown_status == 0:
            shutdown_status = 128 + signum

    for managed_signal in _MANAGED_SIGNALS:
        signal.signal(managed_signal, record_shutdown)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _MANAGED_SIGNALS)
    _write_ready_file(ready_file, token)

    aggregate_status = 0
    while True:
        try:
            _, wait_status = os.wait()
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        child_status = _exit_status(wait_status)
        if child_status != 0 and aggregate_status == 0:
            aggregate_status = child_status

    return shutdown_status or aggregate_status


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--verify-owner"] and len(arguments) == 4:
        return 0 if _verify_owner(
            int(arguments[1]), arguments[2], arguments[3]
        ) else 1
    if (
        len(arguments) < 6
        or arguments[0] != "--ownership-token"
        or arguments[2] != "--ready-file"
    ):
        print(
            "usage: run_j_worker_supervisor.py --ownership-token TOKEN "
            "--ready-file PATH -- COMMAND...",
            file=sys.stderr,
        )
        return 2
    if arguments[4] != "--":
        print("supervisor command must follow --", file=sys.stderr)
        return 2
    try:
        return _supervise(arguments[5:], arguments[1], Path(arguments[3]))
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EINTR:
            return 1
        print(f"journal worker supervisor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

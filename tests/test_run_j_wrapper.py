import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest

from scripts import run_j_worker_supervisor as worker_supervisor


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _isolated_wrapper(
    tmp_path: Path,
    *,
    signal_between_launch_and_record: bool = False,
    supervisor_readiness_fault: str | None = None,
    supervisor_pidfd_capability_fault: bool = False,
    supervisor_signal_unready_fault: bool = False,
) -> tuple[Path, Path]:
    project_dir = tmp_path / "project"
    scripts_dir = project_dir / "scripts"
    marker_dir = tmp_path / "markers"
    scripts_dir.mkdir(parents=True)
    marker_dir.mkdir()

    worker = """\
import os
import signal
import sys
import time
from pathlib import Path

marker_dir = Path(os.environ["PAPERTRACK_FAKE_MARKER_DIR"])
provider = Path(__file__).stem.split("_")[1]
(marker_dir / f"{provider}.started").write_text(str(os.getpid()))

exit_code = os.environ.get(f"PAPERTRACK_FAKE_{provider.upper()}_EXIT")
if exit_code is not None:
    (marker_dir / f"{provider}.finished").write_text(exit_code)
    raise SystemExit(int(exit_code))

if os.environ.get("PAPERTRACK_FAKE_FINISH_PROVIDER") == provider:
    gate = marker_dir / f"{provider}.finish_gate"
    while not gate.exists():
        time.sleep(0.01)
    (marker_dir / f"{provider}.finished").write_text("finished")
    raise SystemExit(0)

def stop(signum, _frame):
    (marker_dir / f"{provider}.signal").write_text(str(signum))
    raise SystemExit(128 + signum)

if os.environ.get("PAPERTRACK_FAKE_IGNORE_SIGNALS_PROVIDER") == provider:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif os.environ.get("PAPERTRACK_FAKE_NO_INT_HANDLER_PROVIDER") != provider:
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
else:
    signal.signal(signal.SIGTERM, stop)
while True:
    signal.pause()
"""
    for provider in ("acs", "aip", "aps"):
        _write_executable(
            scripts_dir / f"rebuild_{provider}_candidates.py",
            "#!/usr/bin/env python3\n" + worker,
        )

    fake_conda = tmp_path / "fake-conda"
    _write_executable(
        fake_conda,
        """#!/usr/bin/env bash
set -uo pipefail

provider=unknown
for argument in "$@"; do
    case "$argument" in
        *rebuild_acs_candidates.py) provider=acs ;;
        *rebuild_aip_candidates.py) provider=aip ;;
        *rebuild_aps_candidates.py) provider=aps ;;
    esac
done
printf '%s' "$$" > "${PAPERTRACK_FAKE_MARKER_DIR}/${provider}.conda"

if [[ "${PAPERTRACK_FAKE_SIGNAL_PARENT_PROVIDER:-}" == "$provider" ]]; then
    kill -s "${PAPERTRACK_FAKE_PARENT_SIGNAL:-TERM}" "$PPID"
fi

shift 4
if [[ "${PAPERTRACK_FAKE_CONDA_EXIT_PROVIDER:-}" == "$provider" ]]; then
    "$@" &
    printf 'exited' > "${PAPERTRACK_FAKE_MARKER_DIR}/${provider}.conda_exited"
    exit 0
fi
exec "$@"
""",
    )

    supervisor = ROOT / "scripts/run_j_worker_supervisor.py"
    if supervisor.exists():
        supervisor_content = supervisor.read_text(encoding="utf-8")
        arguments_assignment = (
            "    arguments = list(sys.argv[1:] if argv is None else argv)"
        )
        assert arguments_assignment in supervisor_content
        injected_cli_faults = []
        if supervisor_pidfd_capability_fault:
            injected_cli_faults.append(
                '    if arguments[:1] == ["check-pidfd"]:\n'
                '        print("injected pidfd capability failure", file=sys.stderr)\n'
                "        return 1"
            )
        if supervisor_signal_unready_fault:
            injected_cli_faults.append(
                '    if arguments[:1] == ["signal-unready"]:\n'
                '        print("injected signal-unready refusal", file=sys.stderr)\n'
                "        return 1"
            )
        if injected_cli_faults:
            supervisor_content = supervisor_content.replace(
                arguments_assignment,
                arguments_assignment + "\n" + "\n".join(injected_cli_faults),
                1,
            )
        if supervisor_readiness_fault is not None:
            wait_for_child = """\
        marker = Path(os.environ["PAPERTRACK_FAKE_MARKER_DIR"]) / "acs.started"
        while not marker.exists():
            time.sleep(0.01)
"""
            if supervisor_readiness_fault == "publication":
                publication = "    os.replace(temporary, path)"
                assert publication in supervisor_content
                replacement = """\
    marker = Path(os.environ["PAPERTRACK_FAKE_MARKER_DIR"]) / "acs.started"
    while not marker.exists():
        time.sleep(0.01)
    raise OSError("injected readiness replace failure")
"""
            elif supervisor_readiness_fault in {"timeout", "timeout_resistant"}:
                publication = "        _write_ready_file(ready_file, token)"
                assert publication in supervisor_content
                replacement = (
                    wait_for_child
                    + "        # Injected fault: suppress readiness publication."
                )
            else:
                raise AssertionError(
                    f"unknown readiness fault: {supervisor_readiness_fault}"
                )
            supervisor_content = supervisor_content.replace(
                publication, replacement, 1
            )
        (scripts_dir / supervisor.name).write_text(
            supervisor_content, encoding="utf-8"
        )

    wrapper_content = (ROOT / "run_j.sh").read_text(encoding="utf-8")
    replacements = {
        "PROJECT_DIR=/home/ansatz/data/code/arxiv_reading": (
            f"PROJECT_DIR={shlex.quote(str(project_dir))}"
        ),
        "CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda": (
            f"CONDA_BIN={shlex.quote(str(fake_conda))}"
        ),
        "DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas": (
            f"DATA_DIR={shlex.quote(str(tmp_path / 'data'))}"
        ),
        'DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"': (
            f"DIAGNOSTICS_DIR={shlex.quote(str(tmp_path / 'diagnostics'))}"
        ),
    }
    for production, isolated in replacements.items():
        assert production in wrapper_content
        wrapper_content = wrapper_content.replace(production, isolated)
    if signal_between_launch_and_record:
        capture = '        "${worker_flags[@]}" &\n    pid="$!"'
        assert capture in wrapper_content
        wrapper_content = wrapper_content.replace(
            capture,
            '        "${worker_flags[@]}" &\n    kill -s TERM "$$"\n    pid="$!"',
            1,
        )
    wrapper = tmp_path / "run_j.sh"
    _write_executable(wrapper, wrapper_content)
    return wrapper, marker_dir


def _wait_for_paths(paths: list[Path], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return
        time.sleep(0.01)
    missing = [str(path) for path in paths if not path.exists()]
    raise AssertionError(f"timed out waiting for marker files: {missing}")


def _wait_until(predicate, description: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {description}")


def _cleanup_process_groups(process: subprocess.Popen[str], marker_dir: Path) -> None:
    for marker in (*marker_dir.glob("*.started"), *marker_dir.glob("*.conda")):
        try:
            pid = int(marker.read_text(encoding="utf-8"))
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _assert_recorded_process_groups_are_gone(marker_dir: Path) -> None:
    for marker in (*marker_dir.glob("*.started"), *marker_dir.glob("*.conda")):
        pid = int(marker.read_text(encoding="utf-8"))
        assert not Path(f"/proc/{pid}").exists()


def _communicate_bounded(
    process: subprocess.Popen[str], marker_dir: Path, timeout: float = 5.0
) -> tuple[str, str]:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _cleanup_process_groups(process, marker_dir)
        process.communicate(timeout=timeout)
        raise AssertionError("run_j.sh did not terminate within the bounded timeout")


@pytest.mark.parametrize(
    ("shutdown_signal", "expected_status", "expected_marker"),
    (
        (signal.SIGINT, 130, str(int(signal.SIGINT))),
        (signal.SIGTERM, 143, str(int(signal.SIGTERM))),
    ),
)
def test_run_j_signal_reaches_all_provider_descendants_and_reaps_groups(
    tmp_path, shutdown_signal, expected_status, expected_marker
):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {"PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir)}
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = [marker_dir / f"{provider}.started" for provider in ("acs", "aip", "aps")]
    signaled = [marker_dir / f"{provider}.signal" for provider in ("acs", "aip", "aps")]

    try:
        _wait_for_paths(started)
        process.send_signal(shutdown_signal)
        _, stderr = _communicate_bounded(process, marker_dir)
        _wait_for_paths(signaled)

        assert process.returncode == expected_status, stderr
        assert [path.read_text(encoding="utf-8") for path in signaled] == [
            expected_marker,
            expected_marker,
            expected_marker,
        ]
        _assert_recorded_process_groups_are_gone(marker_dir)
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_run_j_pending_shutdown_signals_just_launched_group_and_stops_launching(
    tmp_path,
):
    wrapper, marker_dir = _isolated_wrapper(
        tmp_path, signal_between_launch_and_record=True
    )
    environment = os.environ | {"PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir)}
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 143
        assert not (marker_dir / "aip.conda").exists()
        assert not (marker_dir / "aps.conda").exists()
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_run_j_resets_inherited_sigint_for_descendant_without_handler(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_NO_INT_HANDLER_PROVIDER": "acs",
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _wait_for_paths(
            [marker_dir / f"{provider}.started" for provider in ("acs", "aip", "aps")]
        )
        process.send_signal(signal.SIGINT)
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 130, stderr
        assert not (marker_dir / "acs.signal").exists()
        _assert_recorded_process_groups_are_gone(marker_dir)
    finally:
        _cleanup_process_groups(process, marker_dir)


def _find_direct_supervisor(wrapper_pid: int, provider: str) -> int | None:
    children_path = Path(f"/proc/{wrapper_pid}/task/{wrapper_pid}/children")
    try:
        children = children_path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return None
    for child in children:
        try:
            command = Path(f"/proc/{child}/cmdline").read_bytes().replace(b"\0", b" ")
        except FileNotFoundError:
            continue
        if f"rebuild_{provider}_candidates.py".encode() in command:
            return int(child)
    return None


def _proc_starttime(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing_paren = stat.rfind(") ")
    assert closing_paren >= 0
    return stat[closing_paren + 2 :].split()[19]


def test_run_j_reaps_later_provider_as_soon_as_it_finishes(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_FINISH_PROVIDER": "aps",
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _wait_for_paths(
            [marker_dir / f"{provider}.started" for provider in ("acs", "aip", "aps")]
        )
        supervisor_pid: list[int] = []

        def found_aps_supervisor() -> bool:
            pid = _find_direct_supervisor(process.pid, "aps")
            if pid is not None:
                supervisor_pid[:] = [pid]
                return True
            return False

        _wait_until(found_aps_supervisor, "APS supervisor ownership")
        (marker_dir / "aps.finish_gate").touch()
        _wait_for_paths([marker_dir / "aps.finished"])
        _wait_until(
            lambda: not Path(f"/proc/{supervisor_pid[0]}").exists(),
            "prompt APS supervisor reap",
        )
        assert (marker_dir / "acs.started").exists()
        assert (marker_dir / "aip.started").exists()

        process.send_signal(signal.SIGTERM)
        _, stderr = _communicate_bounded(process, marker_dir)
        assert process.returncode == 143, stderr
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_run_j_supervisor_owns_group_after_conda_exits_with_descendant(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_CONDA_EXIT_PROVIDER": "acs",
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _wait_for_paths(
            [
                marker_dir / "acs.started",
                marker_dir / "acs.conda_exited",
                marker_dir / "aip.started",
                marker_dir / "aps.started",
            ]
        )
        process.send_signal(signal.SIGTERM)
        _, stderr = _communicate_bounded(process, marker_dir)
        _wait_for_paths([marker_dir / "acs.signal"])

        assert process.returncode == 143, stderr
        _assert_recorded_process_groups_are_gone(marker_dir)
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_run_j_waits_all_supervisors_and_aggregates_ordinary_failure(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_ACS_EXIT": "0",
        "PAPERTRACK_FAKE_AIP_EXIT": "7",
        "PAPERTRACK_FAKE_APS_EXIT": "0",
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 1, stderr
        assert [
            (marker_dir / f"{provider}.finished").read_text(encoding="utf-8")
            for provider in ("acs", "aip", "aps")
        ] == ["0", "7", "0"]
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_supervisor_ownership_verifier_rejects_unrelated_session(tmp_path):
    supervisor = ROOT / "scripts/run_j_worker_supervisor.py"
    unrelated = subprocess.Popen(
        [
            "/usr/bin/setsid",
            "/usr/bin/python3",
            "-c",
            "import signal; signal.pause()",
            str(supervisor),
            "supervise",
            "--ownership-token",
            "spoofed-token",
        ],
        start_new_session=False,
    )

    try:
        _wait_until(
            lambda: Path(f"/proc/{unrelated.pid}").exists(),
            "unrelated session leader",
        )
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                str(supervisor),
                "verify-owner",
                str(unrelated.pid),
                "spoofed-token",
                _proc_starttime(unrelated.pid),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 1
        os.kill(unrelated.pid, 0)
    finally:
        try:
            os.killpg(unrelated.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("readiness_fault", ("publication", "timeout"))
def test_run_j_readiness_failure_terminates_owned_tree_and_stops_launching(
    tmp_path, readiness_fault
):
    wrapper, marker_dir = _isolated_wrapper(
        tmp_path, supervisor_readiness_fault=readiness_fault
    )
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "TMPDIR": str(tmp_path),
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 1, stderr
        assert (marker_dir / "acs.started").exists(), stderr
        assert not (marker_dir / "aip.conda").exists()
        assert not (marker_dir / "aps.conda").exists()
        _assert_recorded_process_groups_are_gone(marker_dir)
        assert not list(tmp_path.glob("papertrack-run-j.*"))
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_pidfd_signal_pins_before_identity_and_never_signals_spoof(monkeypatch):
    events = []

    monkeypatch.setattr(
        worker_supervisor.os,
        "pidfd_open",
        lambda pid, flags=0: events.append(("open", pid, flags)) or 91,
        raising=False,
    )
    monkeypatch.setattr(
        worker_supervisor,
        "_process_identity",
        lambda pid, token, expected_ppid=None: events.append(
            ("identity", pid, token, expected_ppid)
        )
        or None,
    )
    monkeypatch.setattr(
        worker_supervisor.signal,
        "pidfd_send_signal",
        lambda *args: events.append(("send", *args)),
        raising=False,
    )
    monkeypatch.setattr(
        worker_supervisor.os,
        "close",
        lambda fd: events.append(("close", fd)),
    )

    assert not worker_supervisor._pidfd_signal_owned_unready(
        1234, "expected-token", 4321, signal.SIGTERM
    )
    assert events == [
        ("open", 1234, 0),
        ("identity", 1234, "expected-token", 4321),
        ("close", 91),
    ]


def test_pidfd_signal_sends_only_after_pinned_identity_passes(monkeypatch):
    events = []

    monkeypatch.setattr(
        worker_supervisor.os,
        "pidfd_open",
        lambda pid, flags=0: events.append(("open", pid, flags)) or 92,
        raising=False,
    )
    monkeypatch.setattr(
        worker_supervisor,
        "_process_identity",
        lambda pid, token, expected_ppid=None: events.append(
            ("identity", pid, token, expected_ppid)
        )
        or "starttime",
    )
    monkeypatch.setattr(
        worker_supervisor.signal,
        "pidfd_send_signal",
        lambda fd, signum, info, flags: events.append(
            ("send", fd, signum, info, flags)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        worker_supervisor.os,
        "close",
        lambda fd: events.append(("close", fd)),
    )

    assert worker_supervisor._pidfd_signal_owned_unready(
        1235, "expected-token", 4321, signal.SIGINT
    )
    assert events == [
        ("open", 1235, 0),
        ("identity", 1235, "expected-token", 4321),
        ("send", 92, signal.SIGINT, None, 0),
        ("close", 92),
    ]


def test_pidfd_cli_rejects_unrelated_direct_child_without_signaling(tmp_path):
    supervisor = ROOT / "scripts/run_j_worker_supervisor.py"
    unrelated = subprocess.Popen(
        ["/usr/bin/python3", "-c", "import signal; signal.pause()"]
    )

    try:
        completed = subprocess.run(
            [
                "/usr/bin/python3",
                str(supervisor),
                "signal-unready",
                str(unrelated.pid),
                "expected-token",
                str(os.getpid()),
                "TERM",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 1
        assert "refusing" in completed.stderr or "unable" in completed.stderr
        os.kill(unrelated.pid, 0)
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_run_j_pidfd_preflight_fails_before_any_provider_starts(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(
        tmp_path, supervisor_pidfd_capability_fault=True
    )
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_ACS_EXIT": "0",
        "PAPERTRACK_FAKE_AIP_EXIT": "0",
        "PAPERTRACK_FAKE_APS_EXIT": "0",
        "TMPDIR": str(tmp_path),
    }

    completed = subprocess.run(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 1, completed.stderr
    assert "pidfd" in completed.stderr
    assert not list(marker_dir.iterdir())
    assert not list(tmp_path.glob("papertrack-run-j.*"))


def test_run_j_signal_unready_refusal_relies_on_supervisor_watchdog_cleanup(
    tmp_path,
):
    wrapper, marker_dir = _isolated_wrapper(
        tmp_path,
        supervisor_readiness_fault="timeout_resistant",
        supervisor_signal_unready_fault=True,
    )
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_IGNORE_SIGNALS_PROVIDER": "acs",
        "TMPDIR": str(tmp_path),
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    _, stderr = process.communicate(timeout=5)

    assert process.returncode == 1, stderr
    assert "injected signal-unready refusal" in stderr
    assert (marker_dir / "acs.started").exists(), stderr
    assert not (marker_dir / "aip.conda").exists()
    assert not (marker_dir / "aps.conda").exists()
    _wait_until(
        lambda: all(
            not Path(f"/proc/{int(marker.read_text(encoding='utf-8'))}").exists()
            for marker in (*marker_dir.glob("*.started"), *marker_dir.glob("*.conda"))
        ),
        "supervisor-owned watchdog cleanup",
    )
    assert not list(tmp_path.glob("papertrack-run-j.*"))


def test_run_j_readiness_timeout_kills_signal_resistant_descendant(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(
        tmp_path, supervisor_readiness_fault="timeout_resistant"
    )
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_IGNORE_SIGNALS_PROVIDER": "acs",
        "TMPDIR": str(tmp_path),
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 1, stderr
        assert (marker_dir / "acs.started").exists(), stderr
        assert not (marker_dir / "aip.conda").exists()
        assert not (marker_dir / "aps.conda").exists()
        _assert_recorded_process_groups_are_gone(marker_dir)
        assert not list(tmp_path.glob("papertrack-run-j.*"))
    finally:
        _cleanup_process_groups(process, marker_dir)


def test_run_j_term_escalates_to_kill_for_ready_resistant_descendant(tmp_path):
    wrapper, marker_dir = _isolated_wrapper(tmp_path)
    environment = os.environ | {
        "PAPERTRACK_FAKE_MARKER_DIR": str(marker_dir),
        "PAPERTRACK_FAKE_IGNORE_SIGNALS_PROVIDER": "acs",
    }
    process = subprocess.Popen(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    try:
        _wait_for_paths(
            [marker_dir / f"{provider}.started" for provider in ("acs", "aip", "aps")]
        )
        process.send_signal(signal.SIGTERM)
        _, stderr = _communicate_bounded(process, marker_dir)

        assert process.returncode == 143, stderr
        _assert_recorded_process_groups_are_gone(marker_dir)
    finally:
        _cleanup_process_groups(process, marker_dir)

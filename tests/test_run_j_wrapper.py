import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _isolated_wrapper(
    tmp_path: Path, *, signal_between_launch_and_record: bool = False
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
from pathlib import Path

marker_dir = Path(os.environ["PAPERTRACK_FAKE_MARKER_DIR"])
provider = Path(__file__).stem.split("_")[1]
(marker_dir / f"{provider}.started").write_text(str(os.getpid()))

def stop(signum, _frame):
    (marker_dir / f"{provider}.signal").write_text(str(signum))
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
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
trap - TERM INT

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
"$@" &
child=$!
wait "$child"
""",
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


def _cleanup_process_groups(process: subprocess.Popen[str], marker_dir: Path) -> None:
    for marker in marker_dir.glob("*.conda"):
        try:
            os.killpg(int(marker.read_text(encoding="utf-8")), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _assert_recorded_process_groups_are_gone(marker_dir: Path) -> None:
    for marker in marker_dir.glob("*.conda"):
        with pytest.raises(ProcessLookupError):
            os.killpg(int(marker.read_text(encoding="utf-8")), 0)


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
        _communicate_bounded(process, marker_dir)
        _wait_for_paths(signaled)

        assert process.returncode == expected_status
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
        _communicate_bounded(process, marker_dir)

        assert process.returncode == 143
        assert not (marker_dir / "aip.conda").exists()
        assert not (marker_dir / "aps.conda").exists()
    finally:
        _cleanup_process_groups(process, marker_dir)

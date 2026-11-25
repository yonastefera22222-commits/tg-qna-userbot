"""The Docker image must start on a platform that mounts a root-owned volume.

Both failure modes here are silent-in-the-logs ones, which is why they are pinned
rather than left to a manual deploy:

1. `USER botuser` + a root-owned volume = PermissionError in `data_dir.mkdir()`
   before the first log line. Northflank (Kubernetes) mounts volumes root-owned;
   Render never hit this because its free tier has no disk at all.
2. An entrypoint that *replaces* the CMD instead of forwarding it starts a shell
   that exits immediately, or worse, the app as PID 1 with no signal handling.

These are static checks — the sandbox has no Docker — plus a real run of the
entrypoint's shell logic, which is the part that can actually be exercised here.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text()


# ---------------------------------------------------------------------------
# The Dockerfile contract
# ---------------------------------------------------------------------------


def test_the_entrypoint_is_installed_and_executable() -> None:
    text = _dockerfile()
    assert "COPY deploy/entrypoint.sh" in text
    assert "chmod +x /usr/local/bin/entrypoint.sh" in text
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text


def test_the_image_does_not_hard_switch_to_a_non_root_user() -> None:
    """A root-owned volume cannot be adopted by a container already running as botuser.

    The hardening moved into the entrypoint (chown, then setpriv), so a bare
    `USER botuser` before the entrypoint would defeat it entirely.
    """
    lines = [
        ln.strip()
        for ln in _dockerfile().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "USER botuser" not in lines
    # Root is never the *final* runtime identity; the app is always dropped.
    assert not any(ln.startswith("USER root") for ln in lines)


def test_setpriv_is_available() -> None:
    """Chowning as root then dropping privileges needs util-linux installed."""
    text = _dockerfile()
    assert "util-linux" in text
    # And the entrypoint actually uses it.
    assert "setpriv" in ENTRYPOINT.read_text()


def test_the_command_still_starts_the_bot() -> None:
    assert 'CMD ["python", "-m", "qna_bot"]' in _dockerfile()


def test_data_dir_is_declared_as_a_volume() -> None:
    """The whole point of the exercise: /app/data survives a redeploy."""
    text = _dockerfile()
    assert 'VOLUME ["/app/data"]' in text
    assert "DATA_DIR=/app/data" in text


def test_the_entrypoint_is_syntactically_valid_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_entrypoint_is_executable_in_git() -> None:
    """A non-executable file fails the build at chmod, or worse, at runtime."""
    mode = ENTRYPOINT.stat().st_mode
    assert mode & stat.S_IXUSR, "entrypoint.sh must be committed with +x"


# ---------------------------------------------------------------------------
# The entrypoint's actual behaviour
# ---------------------------------------------------------------------------


def _run(data_dir: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *command],
        env={**os.environ, "DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_it_forwards_the_command_rather_than_replacing_it(tmp_path) -> None:
    """`docker run image echo hi` must print hi, not start a shell."""
    result = _run(tmp_path / "data", "echo", "hello-from-app")
    assert result.returncode == 0
    assert "hello-from-app" in result.stdout


def test_it_creates_a_missing_data_dir(tmp_path) -> None:
    """Config calls mkdir too, but the entrypoint must not depend on that succeeding."""
    target = tmp_path / "fresh" / "data"
    result = _run(target, "echo", "ok")
    assert result.returncode == 0
    assert target.is_dir()


def test_an_unwritable_data_dir_fails_with_a_readable_message(tmp_path) -> None:
    """The failure mode that used to be a bare PermissionError from deep in the app."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        result = _run(locked, "echo", "should-not-run")
        assert result.returncode == 1
        assert "should-not-run" not in result.stdout
        assert "not writable" in result.stderr
        # The message has to name a fix, not just the symptom.
        assert "chown" in result.stderr or "volume" in result.stderr
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(os.geteuid() != 0, reason="root branch only runs as root")
def test_as_root_it_adopts_the_volume_and_drops_privileges(tmp_path) -> None:
    """Pins the Northflank case: root-owned volume, app still runs unprivileged."""
    volume = tmp_path / "volume"
    volume.mkdir()
    volume.chmod(0o755)
    # `id -un` shows the user the app actually runs as.
    result = _run(volume, "id", "-un")
    assert result.returncode == 0
    assert result.stdout.strip() == "botuser"


def test_the_entrypoint_never_echoes_the_environment(tmp_path) -> None:
    """It reads DATA_DIR; it must not dump the env, which holds BOT_TOKEN."""
    text = ENTRYPOINT.read_text()
    assert not re.search(r"^\s*env\s*$", text, re.MULTILINE)
    assert not re.search(r"\bset\s+-x\b", text)


def test_no_privileged_shortcut_is_used() -> None:
    """chown + setpriv, not --privileged or a root CMD."""
    text = ENTRYPOINT.read_text()
    assert "setpriv" in text
    assert "sudo" not in text
    assert "chmod 777" not in text

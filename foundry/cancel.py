"""Cancel sentinel: `foundry cancel` drops `.foundry/cancel`; the orchestrator polls for it.

The cancelling process never touches foundry.lock or git. Only the orchestrator, which owns
the run, writes the cancellation entry and commit, so there is a single writer and the lock
and history stay consistent. The orchestrator polls between phases, so a phase that is
mid-flight is allowed to finish its atomic operation first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from foundry.context import EnvFailure, iso_utc, utc_now
from foundry.lock import LockFile
from foundry.state import STATE_DIRNAME

CANCEL_FILENAME = "cancel"


def sentinel_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_DIRNAME / CANCEL_FILENAME


def is_cancel_requested(workspace: Path) -> bool:
    return sentinel_path(workspace).exists()


def request_cancel(workspace: Path) -> bool:
    """Ask the running orchestrator to stop. False if there is no run in progress."""
    if not LockFile.for_workspace(workspace).is_in_progress():
        return False
    target = sentinel_path(workspace)
    tmp_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".cancel.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"requested_at = {iso_utc(utc_now())}\n")
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError as exc:
        raise EnvFailure(f"cannot write cancel sentinel {target}: {exc}") from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return True


def clear_cancel(workspace: Path) -> None:
    try:
        sentinel_path(workspace).unlink(missing_ok=True)
    except OSError as exc:
        raise EnvFailure(f"cannot remove cancel sentinel: {exc}") from exc

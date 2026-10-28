"""Approved planning artifacts on disk: `.foundry/artifacts/<phase>.md`.

The lock records only hashes (NFR-7), so this is where approved content lives for later
stages: Epic 3 scaffolds from the architecture and epics, and `foundry resume` reloads them
after a crash. A file always holds the *latest approved* version, so a PRD reworked after
pre-flight replaces the earlier one. Writes are temp-file + rename, like the lock, so a crash
never leaves a half-written artifact behind.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from foundry.context import Artifact, EnvFailure
from foundry.state import STATE_DIRNAME

ARTIFACTS_DIRNAME = "artifacts"

# The phases whose approved output is kept on disk. Pre-flight's report is not: it is
# regenerated from the PRD and has no later consumer.
PERSISTED_PHASES = frozenset({"brainstorm", "prd", "architecture", "epics"})


def artifact_path(workspace: Path, phase: str) -> Path:
    return Path(workspace) / STATE_DIRNAME / ARTIFACTS_DIRNAME / f"{phase}.md"


def write_artifact(workspace: Path, phase: str, artifact: Artifact) -> Path:
    """Write the artifact's content byte-for-byte (UTF-8, no newline translation)."""
    target = artifact_path(workspace, phase)
    tmp_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{phase}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(artifact.content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError as exc:
        raise EnvFailure(f"cannot write approved {phase} artifact to {target}: {exc}") from exc
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return target

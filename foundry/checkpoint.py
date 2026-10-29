"""CheckpointHandler protocol (AD-4) and the $EDITOR implementation."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from foundry.context import Artifact, EnvFailure, sha256_of, utc_now


@dataclass(frozen=True)
class CheckpointResult:
    """What a checkpoint hands back: the (possibly edited) artifact plus timing."""

    artifact: Artifact
    presented_hash: str
    editor_open: datetime
    editor_close: datetime

    @property
    def artifact_hash(self) -> str:
        return self.artifact.sha256_hash

    @property
    def approved_without_edits(self) -> bool:
        return self.artifact_hash == self.presented_hash

    @property
    def duration_seconds(self) -> int:
        return round((self.editor_close - self.editor_open).total_seconds())


@runtime_checkable
class CheckpointHandler(Protocol):
    """Swappable checkpoint UI. $EDITOR today; a web UI can implement the same shape."""

    async def present(
        self, artifact: Artifact, metadata: Mapping[str, Any] | None = None
    ) -> CheckpointResult: ...


def resolve_editor_argv(
    env: Mapping[str, str] | None = None, platform: str | None = None
) -> list[str]:
    """$EDITOR (which may carry arguments, e.g. `code --wait`), else vi / notepad."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    configured = (env.get("EDITOR") or "").strip()
    if not configured:
        return ["notepad" if platform == "win32" else "vi"]
    if platform == "win32":
        # posix=False keeps backslashes in paths; quotes are left on the tokens, so strip them
        return [part.strip('"') for part in shlex.split(configured, posix=False)]
    return shlex.split(configured)


class EditorCheckpoint:
    """Opens the artifact in $EDITOR and blocks until the editor process exits."""

    def __init__(self, editor: Sequence[str] | None = None) -> None:
        self._editor = list(editor) if editor is not None else None

    async def present(
        self, artifact: Artifact, metadata: Mapping[str, Any] | None = None
    ) -> CheckpointResult:
        argv = self._editor if self._editor is not None else resolve_editor_argv()
        if not argv:
            raise EnvFailure("$EDITOR is set but empty; set EDITOR to a command that blocks until closed")
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]

        phase = str((metadata or {}).get("phase", "artifact"))
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", phase) + ".md"

        with tempfile.TemporaryDirectory(prefix="foundry-checkpoint-") as tmp:
            path = Path(tmp) / filename
            presented = artifact.content if artifact.content.endswith("\n") else artifact.content + "\n"
            presented_bytes = presented.encode("utf-8")
            path.write_bytes(presented_bytes)

            editor_open = utc_now()
            await self._run_editor(argv, path)
            editor_close = utc_now()

            try:
                edited_bytes = path.read_bytes()
                edited_text = edited_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise EnvFailure(f"could not read the artifact back from the editor: {exc}") from exc

        if sha256_of(edited_bytes) == sha256_of(presented_bytes):
            returned = artifact  # untouched: keep the original object and its hash
        else:
            returned = Artifact(content=edited_text, type=artifact.type)
        return CheckpointResult(
            artifact=returned,
            presented_hash=artifact.sha256_hash,
            editor_open=editor_open,
            editor_close=editor_close,
        )

    @staticmethod
    async def _run_editor(argv: list[str], path: Path) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(*argv, str(path))
            code = await proc.wait()
        except OSError as exc:
            raise EnvFailure(
                f"could not launch editor {argv[0]!r}: {exc}. Set EDITOR to an installed editor."
            ) from exc
        if code != 0:
            raise EnvFailure(f"editor {argv[0]!r} exited with status {code}; checkpoint not approved")

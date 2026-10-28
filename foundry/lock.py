"""foundry.lock: append-only TOML, written only via atomic temp-file + rename (AD-5).

Sections are addressed by unique dotted names (`checkpoints.brainstorm`,
`implementation.epic-1.story-2`). A section is never edited once written, so the file
only ever grows by whole sections and is valid TOML after every write.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from foundry.context import EnvFailure, RunContext, iso_utc

LOCK_FILENAME = "foundry.lock"

STATUS_IN_PROGRESS = "in_progress"
STATUS_CANCELLED = "cancelled"
STATUS_COMPLETED = "completed"

# [run].status is written once, as "in_progress", and never edited. A run ends by
# appending one of these terminal sections instead.
_TERMINAL_SECTIONS = {"cancelled": STATUS_CANCELLED, "completed": STATUS_COMPLETED}


class LockError(EnvFailure):
    """The lock file could not be read, validated, or written."""


def _nest(dotted_name: str, fields: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = fields
    for part in reversed(dotted_name.split(".")):
        nested = {part: nested}
    return nested


def _lookup(data: dict[str, Any], dotted_name: str) -> Any:
    node: Any = data
    for part in dotted_name.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class LockFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_workspace(cls, workspace: Path) -> LockFile:
        return cls(Path(workspace) / LOCK_FILENAME)

    # ---------------------------------------------------------------- reading

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict[str, Any]:
        """Parse the lock file. Missing file reads as empty; invalid TOML is a LockError."""
        if not self.path.exists():
            return {}
        try:
            return tomllib.loads(self.path.read_bytes().decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
            raise LockError(f"{self.path} is unreadable or not valid TOML: {exc}") from exc

    def has_section(self, dotted_name: str) -> bool:
        return isinstance(_lookup(self.read(), dotted_name), dict)

    def status(self) -> str | None:
        """None if there is no run; otherwise in_progress, cancelled or completed."""
        data = self.read()
        if "run" not in data:
            return None
        for section, status in _TERMINAL_SECTIONS.items():
            if section in data:
                return status
        return STATUS_IN_PROGRESS

    def test_runner(self) -> str | None:
        """The test command extracted from the approved architecture, if recorded."""
        runner = self.read().get("run", {}).get("test_runner")
        command = runner.get("command") if isinstance(runner, dict) else None
        return command if isinstance(command, str) else None

    def is_in_progress(self) -> bool:
        return self.status() == STATUS_IN_PROGRESS

    # ---------------------------------------------------------------- writing

    def init_run(self, ctx: RunContext) -> None:
        """Create the lock file with its [run] section. Refuses to touch an existing run."""
        if self.exists() and self.has_section("run"):
            raise LockError(f"{self.path} already records a run; [run] is never rewritten")
        self.append_section(
            "run",
            {
                "run_id": ctx.run_id,
                "idea": ctx.idea,
                "idea_hash": ctx.idea_hash,
                "model": ctx.model_id,
                "started_at": iso_utc(ctx.start_time),
                "review_epics": ctx.review_epics,
                "status": STATUS_IN_PROGRESS,
            },
        )

    def append_section(self, dotted_name: str, fields: dict[str, Any]) -> None:
        """Append one new section. Existing bytes are carried over untouched."""
        existing = self._read_text()
        if existing:
            try:
                parsed = tomllib.loads(existing)
            except tomllib.TOMLDecodeError as exc:
                raise LockError(f"{self.path} is not valid TOML; refusing to append: {exc}") from exc
            if _lookup(parsed, dotted_name) is not None:
                raise LockError(f"section [{dotted_name}] already exists; the lock is append-only")

        try:
            block = tomli_w.dumps(_nest(dotted_name, fields))
        except (TypeError, ValueError) as exc:
            raise LockError(f"cannot serialise section [{dotted_name}]: {exc}") from exc

        if existing and not existing.endswith("\n"):
            existing += "\n"
        updated = f"{existing}\n{block}" if existing else block

        try:
            tomllib.loads(updated)  # never rename an invalid file into place (NFR-5)
        except tomllib.TOMLDecodeError as exc:
            raise LockError(f"appending [{dotted_name}] would produce invalid TOML: {exc}") from exc

        self._atomic_replace(updated)

    def _read_text(self) -> str:
        if not self.path.exists():
            return ""
        try:
            return self.path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            raise LockError(f"cannot read {self.path}: {exc}") from exc

    def _atomic_replace(self, text: str) -> None:
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        except OSError as exc:
            raise LockError(f"cannot write {self.path}: {exc}") from exc
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

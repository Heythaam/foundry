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

# [run].status is written once, as "in_progress", and never edited. A run ends by appending a
# terminal section instead. `[completed]` is final. `[cancelled]` is not: `foundry resume`
# appends `[resumes.resume-N]`, which records how many cancellations it had seen, so a run is
# cancelled exactly when it has more cancellations than any resume has acknowledged. A second
# cancellation is `[cancelled.cancel-2]`, and so on: section names are never reused.


class LockError(EnvFailure):
    """The lock file could not be read, validated, or written."""


def _numbered(node: Any, prefix: str) -> list[tuple[int, dict[str, Any]]]:
    """The `prefix<N>` sub-tables of `node`, ordered by N."""
    found: list[tuple[int, dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith(prefix) and key[len(prefix) :].isdigit() and isinstance(value, dict):
                found.append((int(key[len(prefix) :]), value))
    return sorted(found, key=lambda item: item[0])


def cancellation_count(data: dict[str, Any]) -> int:
    cancelled = data.get("cancelled")
    if not isinstance(cancelled, dict):
        return 0
    return 1 + len(_numbered(cancelled, "cancel-"))


def status_of(data: dict[str, Any]) -> str | None:
    if "run" not in data:
        return None
    if "completed" in data:
        return STATUS_COMPLETED
    acknowledged = max(
        (int(r.get("cancellations_seen", 0)) for _, r in _numbered(data.get("resumes"), "resume-")),
        default=0,
    )
    return STATUS_CANCELLED if cancellation_count(data) > acknowledged else STATUS_IN_PROGRESS


def names_of(data: dict[str, Any]) -> list[str]:
    """Dotted names of every table in `data` that holds values of its own, in order."""
    names: list[str] = []

    def walk(prefix: str, node: dict[str, Any]) -> None:
        if prefix and any(not isinstance(value, dict) for value in node.values()):
            names.append(prefix)
        for key, value in node.items():
            if isinstance(value, dict):
                walk(f"{prefix}.{key}" if prefix else key, value)

    walk("", data)
    return names


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
        return status_of(self.read())

    def test_runner(self) -> str | None:
        """The test command extracted from the approved architecture, if recorded."""
        runner = self.read().get("run", {}).get("test_runner")
        command = runner.get("command") if isinstance(runner, dict) else None
        return command if isinstance(command, str) else None

    def current_model(self) -> str | None:
        """The model the run is on now: the latest `[model_changes.change-N].to`, else `[run].model`."""
        data = self.read()
        changes = _numbered(data.get("model_changes"), "change-")
        if changes:
            return str(changes[-1][1].get("to") or "") or None
        model = data.get("run", {}).get("model")
        return model if isinstance(model, str) else None

    def next_name(self, base: str) -> str:
        """`base` if unused, else `base.run-2`, `base.run-3`...: the lock never reuses a section."""
        data = self.read()
        if not isinstance(_lookup(data, base), dict):
            return base
        number = 2
        while isinstance(_lookup(data, f"{base}.run-{number}"), dict):
            number += 1
        return f"{base}.run-{number}"

    def section_names(self) -> list[str]:
        """Dotted names of every section that holds values, in file order."""
        return names_of(self.read())

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
                "mode": ctx.mode,
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

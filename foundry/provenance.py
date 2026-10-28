"""ProvenanceRecorder: every mutation lands in both sinks, foundry.lock and git (AD-2).

Order per action: journal the intent in the ephemeral state buffer, append the lock
section (atomic), commit it, mark the intent done. If either sink fails the action fails
loudly; a lock entry whose commit failed stays visible as an uncommitted foundry.lock
change plus a non-done row in `.foundry/state.db`, which `foundry resume` reconciles.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from foundry.checkpoint import CheckpointResult
from foundry.context import (
    Artifact,
    EnvFailure,
    FoundryError,
    InfraFailure,
    RunContext,
    classify_exception,
    iso_utc,
    utc_now,
)
from foundry.lock import LOCK_FILENAME, LockFile
from foundry.state import StateBuffer, state_db_path


class GitError(EnvFailure):
    """A git invocation failed."""


def commit_message(action: str, detail: str) -> str:
    """`foundry: <action> — <detail>` (FR-29)."""
    return f"foundry: {action} — {detail}"


async def run_git(workspace: Path, *args: str) -> str:
    """Run git in `workspace`, returning stdout. Never raises anything but FoundryError."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError as exc:
        raise GitError("git was not found on PATH; install git to use Foundry") from exc
    except OSError as exc:
        raise classify_exception(exc) from exc

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        error_cls = InfraFailure if "Could not resolve host" in detail else GitError
        raise error_cls(
            f"git {args[0]} failed (exit {proc.returncode}): {detail}",
            {"args": list(args), "returncode": proc.returncode},
        )
    return stdout.decode("utf-8", errors="replace")


class ProvenanceRecorder:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.lock = LockFile.for_workspace(self.workspace)

    # ------------------------------------------------------------ public actions

    async def record_run_start(self, ctx: RunContext) -> None:
        """Create foundry.lock with its [run] section and commit it."""
        await self._record(
            action="run started",
            detail=ctx.run_id,
            section="run",
            write=lambda: self.lock.init_run(ctx),
        )

    async def record_artifact(self, phase: str, artifact: Artifact) -> None:
        """Append [artifacts.<phase>] with the artifact's hash (never its content, NFR-7)."""
        recorded_at = iso_utc(utc_now())
        await self._record(
            action="artifact recorded",
            detail=f"{phase} {artifact.sha256_hash[:19]}",
            section=f"artifacts.{phase}",
            fields={
                "phase": phase,
                "type": artifact.type.value,
                "artifact_hash": artifact.sha256_hash,
                "recorded_at": recorded_at,
            },
        )

    async def record_checkpoint(
        self, phase: str, result: CheckpointResult, extra: dict[str, Any] | None = None
    ) -> None:
        """Append [checkpoints.<phase>]: editor open/close times and whether it was edited.

        `extra` carries phase-specific results (e.g. pre-flight counts); core fields win.
        """
        await self._record(
            action="checkpoint approved",
            detail=f"{phase} ({'no edits' if result.approved_without_edits else 'edited'})",
            section=f"checkpoints.{phase}",
            fields={
                **(extra or {}),
                "phase": phase,
                "artifact_hash": result.artifact_hash,
                "presented_hash": result.presented_hash,
                "editor_open": iso_utc(result.editor_open),
                "editor_close": iso_utc(result.editor_close),
                "duration_seconds": result.duration_seconds,
                "approved_without_edits": result.approved_without_edits,
            },
        )

    async def record_derived(self, section: str, fields: dict[str, Any]) -> None:
        """Append a section derived from an approved artifact (e.g. `run.test_runner`)."""
        await self._record(
            action="derived section recorded", detail=section, section=section, fields=fields
        )

    async def record_cancelled(self, ctx: RunContext, before: str) -> None:
        """Append [cancelled]; this ends the run without editing [run] (AD-5)."""
        await self._record(
            action="cancelled",
            detail=f"before {before}",
            section="cancelled",
            fields={
                "run_id": ctx.run_id,
                "cancelled_at": iso_utc(utc_now()),
                "before": before,
            },
        )

    async def record_completed(self, ctx: RunContext, final: Artifact) -> None:
        await self._record(
            action="run completed",
            detail=ctx.run_id,
            section="completed",
            fields={
                "completed_at": iso_utc(utc_now()),
                "artifact_hash": final.sha256_hash,
            },
        )

    # --------------------------------------------------------------- the sinks

    async def _record(
        self,
        action: str,
        detail: str,
        section: str,
        fields: dict[str, Any] | None = None,
        write: Callable[[], None] | None = None,
    ) -> None:
        message = commit_message(action, detail)
        try:
            async with StateBuffer(state_db_path(self.workspace)) as buffer:
                op_id = await buffer.begin(action, detail, section)
                try:
                    if write is not None:
                        await asyncio.to_thread(write)
                    else:
                        await asyncio.to_thread(self.lock.append_section, section, fields or {})
                    await self._commit(message)
                except Exception as exc:
                    await buffer.fail(op_id, str(exc))
                    raise
                await buffer.complete(op_id)
        except FoundryError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def _commit(self, message: str) -> None:
        if not (self.workspace / ".git").exists():
            try:
                await run_git(self.workspace, "rev-parse", "--git-dir")
            except GitError:
                await run_git(self.workspace, "init", "--quiet")
        await run_git(self.workspace, "add", "--", LOCK_FILENAME)
        # Pathspec limits the commit to the lock so unrelated staged work is never swept in.
        await run_git(self.workspace, "commit", "--quiet", "-m", message, "--", LOCK_FILENAME)

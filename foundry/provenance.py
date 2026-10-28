"""ProvenanceRecorder: every mutation lands in both sinks, foundry.lock and git (AD-2).

Order per action: journal the intent in the ephemeral state buffer, append the lock
section (atomic), commit it, mark the intent done. If either sink fails the action fails
loudly; a lock entry whose commit failed stays visible as an uncommitted foundry.lock
change plus a non-done row in `.foundry/state.db`, which `foundry resume` reconciles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from foundry.checkpoint import CheckpointResult
from foundry.context import (
    Artifact,
    FoundryError,
    RunContext,
    classify_exception,
    iso_utc,
    utc_now,
)
from foundry.epics import Story
from foundry.gitops import CIResult, GitOps, PullRequestInfo, RepoInfo, commit_message
from foundry.lock import LOCK_FILENAME, LockFile
from foundry.state import StateBuffer, state_db_path


class ProvenanceRecorder:
    def __init__(self, workspace: Path, gitops: GitOps | None = None) -> None:
        self.workspace = Path(workspace)
        self.lock = LockFile.for_workspace(self.workspace)
        self.git = gitops or GitOps(self.workspace)  # AD-8: every git call goes through it

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

    async def record_repo(self, info: RepoInfo) -> None:
        """Append [repo] once GitHub has created the repository (FR-14)."""
        await self._record(
            action="repo created",
            detail=info.full_name,
            section="repo",
            fields={
                "name": info.name,
                "full_name": info.full_name,
                "url": info.html_url,
                "clone_url": info.clone_url,
                "private": info.private,
                "created_at": iso_utc(utc_now()),
            },
        )

    async def record_scaffold(
        self, branch: str, file_hashes: dict[str, str], paths: Sequence[str]
    ) -> None:
        """Append [scaffold] and commit it together with the scaffold files (FR-15)."""
        await self._record(
            action="scaffold",
            detail="README, LICENSE, .gitignore, ci.yml, foundry.lock seeded",
            section="scaffold",
            fields={
                "branch": branch,
                "scaffolded_at": iso_utc(utc_now()),
                "file_hashes": dict(sorted(file_hashes.items())),
            },
            paths=[*paths, LOCK_FILENAME],
        )

    async def record_epic_branch(
        self, number: int, title: str, branch: str, base: str, story_ids: Sequence[str]
    ) -> None:
        """Append [epics.epic-N] when its feature branch exists (FR-16)."""
        await self._record(
            action="epic branch created",
            detail=branch,
            section=f"epics.epic-{number}",
            fields={
                "title": title,
                "branch": branch,
                "base": base,
                "story_ids": list(story_ids),
                "started_at": iso_utc(utc_now()),
            },
        )

    async def record_story(
        self,
        story: Story,
        *,
        artifact_hash: str,
        tests_passed: int | None,
        duration_seconds: float,
        retries: int,
    ) -> None:
        """Append [implementation.epic-E.story-S] and commit it with the story's changes.

        `artifact_hash` is the SHA-256 of the staged diff, so the lock pins exactly what was
        committed without holding any of the agent's output (NFR-7). One call, one commit
        (NFR-8): the message is `foundry: story E.S implemented — title` (FR-19, FR-29).
        """
        await self._record(
            action=f"story {story.id} implemented",
            detail=story.title,
            section=f"implementation.epic-{story.epic}.story-{story.number}",
            fields={
                "story_id": story.id,
                "title": story.title,
                "artifact_hash": artifact_hash,
                "tests_passed": tests_passed if tests_passed is not None else 0,
                "tests_counted": tests_passed is not None,
                "duration_seconds": round(duration_seconds, 3),
                "retries": retries,
                "completed_at": iso_utc(utc_now()),
            },
            paths=None,  # the story's changes and the lock entry land in one commit
        )

    async def record_epic_merged(
        self, number: int, pr: PullRequestInfo, ci: CIResult, merge_sha: str
    ) -> None:
        """Append [epics.epic-N.merged] on main after the PR merged (FR-20).

        The commit message is `foundry: epic N merged — PR #n`. Opening the PR is not a
        separate record: it would add a commit to the branch whose head the PR points at.
        """
        await self._record(
            action=f"epic {number} merged",
            detail=f"PR #{pr.number}",
            section=f"epics.epic-{number}.merged",
            fields={
                "pull_request": pr.number,
                "pull_request_url": pr.url,
                "head_sha": pr.head_sha,
                "merge_sha": merge_sha,
                "ci_checks": list(ci.checks),
                "merged_at": iso_utc(utc_now()),
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
        message: str | None = None,
        paths: Sequence[str] | None = (LOCK_FILENAME,),
    ) -> None:
        """Journal, append the lock section, commit. `paths=None` commits the whole tree."""
        message = message or commit_message(action, detail)
        try:
            async with StateBuffer(state_db_path(self.workspace)) as buffer:
                op_id = await buffer.begin(action, detail, section)
                try:
                    if write is not None:
                        await asyncio.to_thread(write)
                    else:
                        await asyncio.to_thread(self.lock.append_section, section, fields or {})
                    await self.git.commit(message, paths)
                except Exception as exc:
                    await buffer.fail(op_id, str(exc))
                    raise
                await buffer.complete(op_id)
        except FoundryError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc

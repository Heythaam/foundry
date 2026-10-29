"""`foundry resume`: pick an interrupted run up exactly where it stopped (FR-3, FR-32, NFR-4).

A run can stop in three ways: it crashes (power, kill -9), it is cancelled, or an environment
failure halts it. Whatever happened, the committed record is the truth: foundry.lock says which
stories are done, git says what is on disk. This module reads both, repairs the one thing a crash
can leave inconsistent between them (a lock entry that was written but not committed), makes
the model decision (FR-33) and records `foundry: resumed`. Everything after that is the
implementation loop, which is written to continue from the lock (see `ImplementationStage.run`).
"""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Callable
from pathlib import Path

from foundry.agent import print_to_terminal
from foundry.artifacts import artifact_path, write_artifact
from foundry.context import (
    Artifact,
    ArtifactType,
    EnvFailure,
    RunContext,
    classify_exception,
)
from foundry.epics import Story
from foundry.gitops import GitOps, commit_message
from foundry.lock import LOCK_FILENAME, LockFile, names_of
from foundry.models import AnthropicModelChecker, ModelChecker, ModelPrompt, TerminalModelPrompt
from foundry.progress import (
    PHASE_DONE,
    PHASE_PLANNING,
    RunProgress,
    progress_from_lock,
)
from foundry.provenance import ProvenanceRecorder, story_commit_message
from foundry.scaffold import PLANNING_DIR, PLANNING_PHASES
from foundry.state import STATE_DB_FILENAME, STATE_DIRNAME, StateBuffer, state_db_path

_ARTIFACT_TYPES = {
    "brainstorm": ArtifactType.BRAINSTORM,
    "prd": ArtifactType.PRD,
    "architecture": ArtifactType.ARCHITECTURE,
    "epics": ArtifactType.EPICS,
}


def _say(text: str) -> None:
    print_to_terminal(text + "\n")


def require_resumable(progress: RunProgress | None) -> RunProgress:
    """The run to resume, or an informative error when there is nothing to resume (FR-3)."""
    if progress is None:
        raise EnvFailure(
            "There is no Foundry run to resume in this directory.",
            fix="Run `foundry resume` from the directory where `foundry pro` was started.",
        )
    if progress.status == "completed" or progress.phase == PHASE_DONE:
        raise EnvFailure(
            f"Run {progress.run_id} already completed; there is nothing to resume.",
            fix="Start a new run with `foundry pro` in an empty directory.",
        )
    if progress.phase == PHASE_PLANNING:
        raise EnvFailure(
            f"Run {progress.run_id} stopped during planning"
            + (f" (after the approved {progress.last_artifact})" if progress.last_artifact else "")
            + ", before the epics were approved. `foundry resume` continues from the "
            "implementation; planning cannot be resumed.",
            fix="Start a new run with `foundry pro` in an empty directory.",
        )
    return progress


def context_from_lock(workspace: Path) -> RunContext:
    """The RunContext of the recorded run, on the model it is on now."""
    from datetime import datetime, timezone

    progress = require_resumable(progress_from_lock(LockFile.for_workspace(workspace).read()))
    started = datetime.strptime(progress.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return RunContext(
        idea=progress.idea,
        run_id=progress.run_id,
        workspace_path=workspace,
        model_id=progress.model,
        start_time=started,
        review_epics=progress.review_epics,
    )


def resume_point(progress: RunProgress) -> str:
    """Where the run picks up, in words: `story 1.2 (1 of 3 done)`."""
    if progress.next_story:
        return f"story {progress.next_story} ({progress.completed_count} of {progress.stories_total} done)"
    open_epics = [e for e in progress.started_epics if e not in progress.merged_epics]
    if open_epics:
        return f"finishing epic {open_epics[0]} (all {progress.stories_total} stories done)"
    return "completion"


class ResumeCoordinator:
    """Everything that happens between "foundry resume" and the first story."""

    def __init__(
        self,
        ctx: RunContext,
        gitops: GitOps,
        checker: ModelChecker | None = None,
        prompt: ModelPrompt | None = None,
        say: Callable[[str], None] = _say,
    ) -> None:
        self.ctx = ctx
        self.git = gitops
        self.checker = checker or AnthropicModelChecker()
        self.prompt = prompt or TerminalModelPrompt()
        self.say = say

    async def prepare(self, recorder: ProvenanceRecorder) -> None:
        workspace = self.ctx.workspace_path
        progress = require_resumable(progress_from_lock(recorder.lock.read()))
        if not (workspace / ".git").exists():
            raise EnvFailure(
                "This directory has a foundry.lock but is not a git repository.",
                fix="Restore the .git directory (the run's history lives in it), then resume.",
            )
        existing = recorder.lock.read().get("repo")
        if isinstance(existing, dict):
            self.git.repo_full_name = str(existing.get("full_name") or "") or None

        await self.restore_artifacts()
        reconciled = await self.reconcile(recorder)
        await self.rebuild_state(recorder.lock)

        progress = require_resumable(progress_from_lock(recorder.lock.read()))
        tree = "clean" if await self.git.is_clean() else "uncommitted changes"
        self.say(f"Resuming run {progress.run_id}")
        self.say(f"  Resume point: {resume_point(progress)}")
        self.say(f"  Model: {progress.model}")
        if reconciled:
            self.say(f"  Recovered from a crash: committed {', '.join(reconciled)}")
        if progress.last_halt:
            self.say(f"  The run had stopped: {str(progress.last_halt.get('message', ''))[:200]}")

        changed = await self.check_model(recorder, progress)
        recorder_progress = progress_from_lock(recorder.lock.read())
        assert recorder_progress is not None
        await recorder.record_resumed(
            {
                "resume_point": resume_point(recorder_progress),
                "from_status": progress.status,
                "stories_completed": recorder_progress.completed_count,
                "stories_total": recorder_progress.stories_total,
                "model": self.ctx.model_id,
                "model_changed": changed,
                "working_tree": tree,
                "reconciled": reconciled,
            }
        )

    # ----------------------------------------------------------- artifacts

    async def restore_artifacts(self) -> None:
        """Approved planning documents missing from `.foundry/artifacts` come back from git.

        The scaffold commit carried a copy of each under docs/planning/, so deleting `.foundry`
        (or resuming on a fresh clone) does not strand the run.
        """
        workspace = self.ctx.workspace_path
        for phase in PLANNING_PHASES:
            if await asyncio.to_thread(artifact_path(workspace, phase).exists):
                continue
            text = await self.git.committed_file(f"{PLANNING_DIR}/{phase}.md")
            if not text:
                raise EnvFailure(
                    f"The approved {phase} document is missing from {STATE_DIRNAME}/artifacts and "
                    f"from {PLANNING_DIR}/ in git, so there is nothing to build from.",
                    fix=f"Restore {artifact_path(workspace, phase)} from a backup, then resume.",
                )
            await asyncio.to_thread(
                write_artifact, workspace, phase, Artifact(text, _ARTIFACT_TYPES[phase])
            )

    # --------------------------------------------------------- crash repair

    async def reconcile(self, recorder: ProvenanceRecorder) -> list[str]:
        """Commit lock sections that were written but never committed (AD-2, AD-6).

        The recorder appends to foundry.lock and then commits. A crash between the two leaves
        sections in the file that git does not have. They are real: the action they record
        happened. So they are committed, once, with the message the interrupted commit would have
        had. A story entry is committed together with the story's own changes, which are what is
        sitting in the working tree. Returns the sections that were committed.
        """
        committed = await self.git.committed_file(LOCK_FILENAME)
        try:
            known = set(names_of(tomllib.loads(committed))) if committed else set()
        except tomllib.TOMLDecodeError:
            known = set()
        data = recorder.lock.read()
        pending = [name for name in names_of(data) if name not in known]
        if not pending:
            return []

        story_entries = [name for name in pending if name.startswith("implementation.")]
        if story_entries:
            message = self._story_message(data, story_entries[-1], pending)
            await self.git.commit(message, None)  # the story's changes and its lock entries together
        else:
            await self.git.commit(await self._lock_message(pending), [LOCK_FILENAME])
        return pending

    @staticmethod
    def _story_message(data: dict, section: str, pending: list[str]) -> str:
        epic_key, story_key_ = section.split(".")[1:3]
        entry = data["implementation"][epic_key][story_key_]
        story = Story(int(epic_key[5:]), int(story_key_[6:]), str(entry.get("title", "")), "")
        kind = "implemented"
        for name in pending:
            if name.startswith("interventions.") and _section(data, name).get("code_edited"):
                kind = "human"
                break
            if name.startswith("failures.") and _section(data, name).get("retry_outcome") == "passed":
                kind = "auto-fixed"
        return story_commit_message(story, kind)

    async def _lock_message(self, pending: list[str]) -> str:
        """The interrupted action's own message if the journal survived, else a generic one."""
        try:
            async with StateBuffer(state_db_path(self.ctx.workspace_path)) as buffer:
                for op in reversed(await buffer.pending()):
                    if op.section in pending:
                        return commit_message(op.action, op.detail)
        except Exception:  # the journal is only a hint; losing it must not block recovery
            pass
        return commit_message("recovered", ", ".join(pending)[:120])

    async def rebuild_state(self, lock: LockFile) -> None:
        """Recreate `.foundry/state.db` from foundry.lock (AD-6): it is only ever a cache.

        Every section the lock holds becomes a completed journal row; anything the old file said
        was in flight has just been reconciled, so nothing carries over.
        """
        path = state_db_path(self.ctx.workspace_path)
        for suffix in ("", "-wal", "-shm"):
            candidate = path.with_name(STATE_DB_FILENAME + suffix)
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass  # in use by something else: the rows below are still correct
        async with StateBuffer(path) as buffer:
            for name in lock.section_names():
                op = await buffer.begin("recorded", "rebuilt from foundry.lock", name)
                await buffer.complete(op)

    # ------------------------------------------------------------- the model

    async def check_model(self, recorder: ProvenanceRecorder, progress: RunProgress) -> bool:
        """FR-33: is the pinned model still there? If not, the human picks another."""
        pinned = progress.model
        try:
            result = await self.checker.check(pinned)
        except Exception as exc:  # classify before propagating (AD-3); a failed check is not fatal
            error = classify_exception(exc, "check the model")
            self.say(f"  Could not check that {pinned} is still available ({error.message}); continuing with it.")
            return False
        if result.available is None:
            self.say(f"  Could not check that {pinned} is still available ({result.reason}); continuing with it.")
            return False
        if result.available:
            return False

        replacement = await self.prompt.choose(pinned, result.alternatives)
        if replacement == pinned:
            return False
        last = progress.implemented[-1] if progress.implemented else "none"
        await recorder.record_model_change(
            pinned, replacement, last, progress.completed_count, progress.stories_total
        )
        self.ctx.model_id = replacement
        self.say(f"  Continuing on {replacement}; a consistency pass will follow the current epic.")
        return True


def _section(data: dict, dotted: str) -> dict:
    node = data
    for part in dotted.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}

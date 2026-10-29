"""Sequences phases for a run. Phases never call each other; this does."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from foundry.artifacts import PERSISTED_PHASES, read_artifact, write_artifact
from foundry.cancel import clear_cancel, is_cancel_requested
from foundry.checkpoint import CheckpointHandler, EditorCheckpoint
from foundry.context import (
    Artifact,
    ArtifactType,
    EnvFailure,
    FailureCategory,
    FoundryError,
    RunContext,
    check_model_alias,
    classify_exception,
)
from foundry.failures import fix_hint
from foundry.gitops import GitOps
from foundry.lock import STATUS_IN_PROGRESS, LockFile
from foundry.phases import Phase, PhaseHooks
from foundry.provenance import ProvenanceRecorder
from foundry.stages import CancelRequested, Stage

RUN_IN_PROGRESS_MESSAGE = "Run in progress. Use `foundry resume` to continue."

# A human who keeps sending the pipeline back is not converging; stop rather than loop forever.
MAX_REVISIONS = 5


class RunInProgress(EnvFailure):
    def __init__(self) -> None:
        super().__init__(RUN_IN_PROGRESS_MESSAGE)


class RunStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class RunResult:
    ctx: RunContext
    artifact: Artifact
    status: RunStatus = RunStatus.COMPLETED


class Orchestrator:
    def __init__(
        self,
        phases: Sequence[Phase],
        checkpoint: CheckpointHandler | None = None,
        stages: Sequence[Stage] = (),
        gitops: GitOps | None = None,
    ) -> None:
        self.phases = list(phases)
        self.checkpoint = checkpoint or EditorCheckpoint()
        self.gitops = gitops
        self.stages = list(stages)  # run after the phases: repo transition, implementation

    async def run(self, ctx: RunContext) -> RunResult:
        check_model_alias(ctx.model_id)
        await asyncio.to_thread(self._ensure_can_start, ctx)
        await asyncio.to_thread(clear_cancel, ctx.workspace_path)  # a sentinel from a dead run
        recorder = ProvenanceRecorder(ctx.workspace_path, self.gitops)
        await recorder.record_run_start(ctx)

        artifact = Artifact(content=ctx.idea, type=ArtifactType.IDEA)
        for phase in self.phases:
            if is_cancel_requested(ctx.workspace_path):
                return await self._cancel(ctx, recorder, artifact, before=f"phase {phase.name}")
            artifact = await self._execute(phase, artifact, ctx, recorder)

        return await self._finish(ctx, recorder, artifact)

    async def resume(
        self,
        ctx: RunContext,
        prepare: Callable[[ProvenanceRecorder], Awaitable[None]] | None = None,
    ) -> RunResult:
        """Continue a run whose planning is approved (FR-3): the stages again, from the lock.

        `prepare` runs first, with the recorder: the resume module reconciles what the crash left
        behind, checks the model and records `foundry: resumed`. Planning phases are not re-run.
        """
        check_model_alias(ctx.model_id)
        await asyncio.to_thread(clear_cancel, ctx.workspace_path)  # a sentinel from the dead run
        recorder = ProvenanceRecorder(ctx.workspace_path, self.gitops)
        try:
            if prepare is not None:
                await prepare(recorder)
            epics = await asyncio.to_thread(read_artifact, ctx.workspace_path, "epics")
        except Exception as exc:  # classify before propagating (AD-3)
            error = classify_exception(exc, "resume")
            if error is exc:
                raise
            raise error from exc
        artifact = Artifact(content=epics, type=ArtifactType.EPICS)
        ctx.upstream["epics"] = artifact
        return await self._finish(ctx, recorder, artifact)

    async def _finish(
        self, ctx: RunContext, recorder: ProvenanceRecorder, artifact: Artifact
    ) -> RunResult:
        """The stages after planning, then the completion record."""
        for stage in self.stages:
            if is_cancel_requested(ctx.workspace_path):
                return await self._cancel(ctx, recorder, artifact, before=f"stage {stage.name}")
            try:
                await self._run_stage(stage, ctx, recorder)
            except CancelRequested as stop:
                return await self._cancel(ctx, recorder, artifact, before=stop.before)

        if is_cancel_requested(ctx.workspace_path):
            return await self._cancel(ctx, recorder, artifact, before="completion")
        await recorder.record_completed(ctx, artifact)
        return RunResult(ctx=ctx, artifact=artifact)

    @staticmethod
    def _ensure_can_start(ctx: RunContext) -> None:
        lock = LockFile.for_workspace(ctx.workspace_path)
        if not lock.exists():
            return
        status = lock.status()
        if status == STATUS_IN_PROGRESS:
            raise RunInProgress()
        if status is not None:
            raise EnvFailure(
                f"{lock.path} already records a run that is {status}. "
                "Start a new run in an empty directory."
            )

    @staticmethod
    async def _cancel(
        ctx: RunContext, recorder: ProvenanceRecorder, artifact: Artifact, before: str
    ) -> RunResult:
        await recorder.record_cancelled(ctx, before=before)
        await asyncio.to_thread(clear_cancel, ctx.workspace_path)
        return RunResult(ctx=ctx, artifact=artifact, status=RunStatus.CANCELLED)

    async def _execute(
        self, phase: Phase, artifact: Artifact, ctx: RunContext, recorder: ProvenanceRecorder
    ) -> Artifact:
        hooks = phase if isinstance(phase, PhaseHooks) else None
        approved = await self._produce_and_approve(phase, hooks, artifact, ctx, recorder, 0)

        revision = 0
        while hooks is not None and hooks.revise_target and self._wants_revision(hooks, approved):
            revision += 1
            if revision > MAX_REVISIONS:
                raise EnvFailure(
                    f"{phase.name} still asks for {hooks.revise_target} changes after "
                    f"{MAX_REVISIONS} revisions; resolve them and start again."
                )
            target = ctx.upstream.get(hooks.revise_target)
            if target is None:
                raise EnvFailure(f"cannot revise {hooks.revise_target!r}: it has not been approved yet")
            # Re-open the upstream artifact for the human to edit, then re-run this phase on it.
            edited = await self._approve(
                hooks.revise_target, None, target, ctx, recorder, revision
            )
            approved = await self._produce_and_approve(
                phase, hooks, edited, ctx, recorder, revision
            )
        return approved

    async def _produce_and_approve(
        self,
        phase: Phase,
        hooks: PhaseHooks | None,
        artifact: Artifact,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
        revision: int,
    ) -> Artifact:
        produced = await self._run_phase(phase, artifact, ctx)
        if phase.checkpoint:
            return await self._approve(phase.name, hooks, produced, ctx, recorder, revision)
        derived = self._hook(hooks, "derived_sections", produced) if hooks else {}
        return await self._accept(phase.name, derived, produced, ctx, recorder, revision)

    async def _approve(
        self,
        name: str,
        hooks: PhaseHooks | None,
        artifact: Artifact,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
        revision: int,
    ) -> Artifact:
        """Checkpoint, then record. Nothing is recorded until the human has approved (FR-28)."""
        section = _section_name(name, revision)
        try:
            result = await self.checkpoint.present(
                artifact,
                {"phase": section, "base_phase": name, "revision": revision, "run_id": ctx.run_id},
            )
        except Exception as exc:  # classify before propagating (AD-3)
            raise classify_exception(exc) from exc
        # Everything derived from the approved text is computed (and so validated) before
        # anything is written: a human who deleted a required section records nothing.
        extra = self._hook(hooks, "checkpoint_fields", result.artifact) if hooks else {}
        derived = self._hook(hooks, "derived_sections", result.artifact) if hooks else {}
        if name in PERSISTED_PHASES:
            # Before the lock says "approved", so a lock entry always implies the file exists.
            await asyncio.to_thread(write_artifact, ctx.workspace_path, name, result.artifact)
        await recorder.record_checkpoint(section, result, extra)
        return await self._accept(name, derived, result.artifact, ctx, recorder, revision)

    async def _accept(
        self,
        name: str,
        derived: dict[str, dict[str, Any]],
        approved: Artifact,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
        revision: int,
    ) -> Artifact:
        await recorder.record_artifact(_section_name(name, revision), approved)
        for section, fields in derived.items():
            await recorder.record_derived(section, fields)
        ctx.upstream[name] = approved
        return approved

    @staticmethod
    def _hook(hooks: PhaseHooks, method: str, approved: Artifact) -> Any:
        try:
            return getattr(hooks, method)(approved)
        except Exception as exc:  # classify before propagating (AD-3)
            raise classify_exception(exc) from exc

    @classmethod
    def _wants_revision(cls, hooks: PhaseHooks, approved: Artifact) -> bool:
        return bool(cls._hook(hooks, "requests_revision", approved))

    @classmethod
    async def _run_stage(cls, stage: Stage, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        try:
            await stage.run(ctx, recorder)
        except CancelRequested:
            raise
        except Exception as exc:  # classify before propagating (AD-3)
            error = classify_exception(exc, f"stage {stage.name}")
            if error.category is FailureCategory.ENVIRONMENT:
                await cls._halt(stage.name, error, recorder)
            if error is exc:
                raise
            raise error from exc

    @staticmethod
    async def _halt(stage: str, error: FoundryError, recorder: ProvenanceRecorder) -> None:
        """An environment failure stops the run at once; save where, why and what to fix (FR-24).

        The run stays in progress so `foundry resume` can pick it up. If saving itself fails (the
        disk is full, git is gone) the original error is still the one the human sees.
        """
        error.fix = fix_hint(error)
        where = {key: error.context[key] for key in ("epic", "story") if key in error.context}
        try:
            await recorder.record_halt(
                stage, error.category.value, error.message, error.fix, where
            )
            error.context["halted"] = True
        except Exception:
            error.context["halted"] = False

    async def _run_phase(self, phase: Phase, artifact: Artifact, ctx: RunContext) -> Artifact:
        try:
            return await phase.run(artifact, ctx)
        except Exception as exc:  # classify before propagating (AD-3)
            raise classify_exception(exc) from exc


def _section_name(name: str, revision: int) -> str:
    """`prd` for the first pass, `prd.revision-1` for the first rework (names are never reused)."""
    return name if revision == 0 else f"{name}.revision-{revision}"

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.context import (
    Artifact,
    ArtifactType,
    EnvFailure,
    FailureCategory,
    FoundryError,
    InfraFailure,
    RunContext,
)
from foundry.orchestrator import Orchestrator


class RecordingPhase:
    checkpoint = False

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        self.log.append(f"{self.name}<-{input.content}")
        return Artifact(content=self.name, type=ArtifactType.STUB)


class ExplodingPhase:
    checkpoint = False

    def __init__(self, exc: Exception) -> None:
        self.name = "boom"
        self.exc = exc

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        raise self.exc


async def test_phases_run_in_sequence_each_receiving_the_previous_output(workspace: Path) -> None:
    log: list[str] = []
    phases = [RecordingPhase("one", log), RecordingPhase("two", log), RecordingPhase("three", log)]
    result = await Orchestrator(phases).run(RunContext(idea="my idea"))
    assert log == ["one<-my idea", "two<-one", "three<-two"]
    assert result.artifact.content == "three"


async def test_no_phases_returns_the_idea_artifact(workspace: Path) -> None:
    result = await Orchestrator([]).run(RunContext(idea="just an idea"))
    assert result.artifact.type is ArtifactType.IDEA


@pytest.mark.parametrize(
    ("raised", "category"),
    [
        (ValueError("bad"), FailureCategory.ENVIRONMENT),
        (TimeoutError("slow"), FailureCategory.INFRASTRUCTURE),
    ],
)
async def test_phase_exceptions_are_classified_before_propagating(
    workspace: Path, raised: Exception, category: FailureCategory
) -> None:
    with pytest.raises(FoundryError) as info:
        await Orchestrator([ExplodingPhase(raised)]).run(RunContext(idea="x"))
    assert info.value.category is category


async def test_already_classified_errors_are_not_rewrapped(workspace: Path) -> None:
    original = InfraFailure("429")
    with pytest.raises(InfraFailure) as info:
        await Orchestrator([ExplodingPhase(original)]).run(RunContext(idea="x"))
    assert info.value is original
    assert not isinstance(info.value, EnvFailure)


async def test_lock_is_created_before_any_phase_runs(workspace: Path) -> None:
    seen: list[bool] = []

    class Probe:
        name = "probe"
        checkpoint = False

        async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
            seen.append((ctx.workspace_path / "foundry.lock").exists())
            return input

    await Orchestrator([Probe()]).run(RunContext(idea="x"))
    assert seen == [True]


async def test_in_progress_lock_blocks_a_new_run_before_any_phase_runs(workspace: Path) -> None:
    from foundry.lock import LockFile
    from foundry.orchestrator import RunInProgress

    LockFile.for_workspace(workspace).init_run(RunContext(idea="first", workspace_path=workspace))
    ran: list[str] = []

    with pytest.raises(RunInProgress, match="foundry resume"):
        await Orchestrator([RecordingPhase("one", ran)]).run(RunContext(idea="second"))
    assert ran == []


async def test_finished_run_lock_is_not_overwritten_by_a_new_run(workspace: Path) -> None:
    from foundry.lock import LockFile

    LockFile.for_workspace(workspace).init_run(RunContext(idea="old", workspace_path=workspace))
    LockFile.for_workspace(workspace).append_section("completed", {"at": "2026-10-28T00:00:00Z"})
    before = (workspace / "foundry.lock").read_bytes()

    with pytest.raises(EnvFailure, match="empty directory"):
        await Orchestrator([]).run(RunContext(idea="new"))
    assert (workspace / "foundry.lock").read_bytes() == before


async def test_each_phase_and_the_run_itself_are_recorded_in_both_sinks(workspace: Path) -> None:
    import subprocess
    import tomllib

    log: list[str] = []
    await Orchestrator([RecordingPhase("one", log), RecordingPhase("two", log)]).run(
        RunContext(idea="x")
    )
    data = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
    assert set(data["artifacts"]) == {"one", "two"}
    assert "completed" in data
    subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, encoding="utf-8"
    ).stdout.splitlines()
    assert len(subjects) == 4  # run started, 2 artifacts, run completed
    assert all(s.startswith("foundry: ") and " — " in s for s in subjects)

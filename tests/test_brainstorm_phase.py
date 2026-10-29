from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from foundry.agent import ClaudeCodeRunner
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    InfraFailure,
    RunContext,
)
from foundry.orchestrator import Orchestrator
from foundry.phases import Phase
from foundry.pipeline import build_pipeline
from foundry.planning import SKILL_BRAINSTORM, BrainstormPhase
from tests.helpers import FakeRunner, ScriptedCheckpoint

IDEA = Artifact("build a todo app", ArtifactType.IDEA)


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


def git_subjects(workspace: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, encoding="utf-8"
    ).stdout.splitlines()


class TestPhase:
    def test_is_a_checkpointed_phase(self) -> None:
        phase = BrainstormPhase(FakeRunner("x"))
        assert isinstance(phase, Phase)
        assert phase.name == "brainstorm" and phase.checkpoint is True

    async def test_invokes_the_brainstorm_agent_with_the_idea(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n\nIdeas\n")
        ctx = RunContext(idea="build a todo app")
        out = await BrainstormPhase(runner).run(IDEA, ctx)

        assert len(runner.requests) == 1
        prompt = runner.prompts[0]
        assert SKILL_BRAINSTORM in prompt
        assert "build a todo app" in prompt
        assert "non-interactively" in prompt
        assert out.type is ArtifactType.BRAINSTORM
        assert out.content == "# Brainstorm\n\nIdeas\n"

    async def test_output_is_normalised_to_one_trailing_newline(self, workspace: Path) -> None:
        out = await BrainstormPhase(FakeRunner("\n\n# Doc\n\n\n")).run(IDEA, RunContext(idea="x"))
        assert out.content == "# Doc\n"

    async def test_empty_document_is_a_code_failure(self, workspace: Path) -> None:
        with pytest.raises(CodeFailure):
            await BrainstormPhase(FakeRunner("   \n")).run(IDEA, RunContext(idea="x"))

    async def test_agent_failures_propagate_with_their_category(self, workspace: Path) -> None:
        with pytest.raises(InfraFailure):
            await BrainstormPhase(FakeRunner(InfraFailure("429"))).run(IDEA, RunContext(idea="x"))


class TestInPipeline:
    async def test_artifact_goes_to_the_checkpoint_then_provenance_is_recorded(
        self, workspace: Path
    ) -> None:
        checkpoint = ScriptedCheckpoint({"brainstorm": lambda text: text + "\nHuman note\n"})
        ctx = RunContext(idea="build a todo app")
        result = await Orchestrator(
            [BrainstormPhase(FakeRunner("# Brainstorm\n"))], checkpoint=checkpoint
        ).run(ctx)

        assert checkpoint.presented == [("brainstorm", "# Brainstorm\n")]
        data = lock_data(workspace)
        approved = Artifact("# Brainstorm\n\nHuman note\n", ArtifactType.BRAINSTORM)
        assert data["checkpoints"]["brainstorm"]["presented_hash"] == Artifact(
            "# Brainstorm\n", ArtifactType.BRAINSTORM
        ).sha256_hash
        assert data["checkpoints"]["brainstorm"]["artifact_hash"] == approved.sha256_hash
        assert data["artifacts"]["brainstorm"]["artifact_hash"] == approved.sha256_hash
        assert ctx.upstream["brainstorm"].content == approved.content
        assert result.artifact.content == approved.content

    async def test_checkpoint_commit_precedes_artifact_commit(self, workspace: Path) -> None:
        await Orchestrator(
            [BrainstormPhase(FakeRunner("# B\n"))], checkpoint=ScriptedCheckpoint()
        ).run(RunContext(idea="x"))
        subjects = list(reversed(git_subjects(workspace)))
        assert [s.split(" — ")[0] for s in subjects] == [
            "foundry: run started",
            "foundry: checkpoint approved",
            "foundry: artifact recorded",
            "foundry: run completed",
        ]

    async def test_nothing_is_recorded_for_a_checkpoint_the_human_never_approved(
        self, workspace: Path
    ) -> None:
        class Abort:
            async def present(self, artifact, metadata=None):
                raise EnvFailure("editor closed with error")

        with pytest.raises(EnvFailure):
            await Orchestrator([BrainstormPhase(FakeRunner("# B\n"))], checkpoint=Abort()).run(
                RunContext(idea="x")
            )
        data = lock_data(workspace)
        assert "checkpoints" not in data and "artifacts" not in data

    async def test_runs_locally_with_no_github_remote(self, workspace: Path) -> None:
        await Orchestrator(
            [BrainstormPhase(FakeRunner("# B\n"))], checkpoint=ScriptedCheckpoint()
        ).run(RunContext(idea="x"))
        remotes = subprocess.run(
            ["git", "remote"], cwd=workspace, capture_output=True, text=True
        ).stdout.strip()
        assert remotes == ""  # AD-9: no repo exists yet

    async def test_real_runner_streams_to_terminal_then_checkpoints_the_final_text(
        self, workspace: Path, fake_claude
    ) -> None:
        command, _ = fake_claude(
            deltas=["# Brain", "storm\n"], delay=0.2, result="# Brainstorm\n\nFinal text\n"
        )
        streamed: list[str] = []
        checkpoint = ScriptedCheckpoint()
        await Orchestrator(
            [BrainstormPhase(ClaudeCodeRunner(command, on_text=streamed.append))],
            checkpoint=checkpoint,
        ).run(RunContext(idea="build a todo app"))

        assert streamed == ["# Brain", "storm\n"]
        assert checkpoint.presented == [("brainstorm", "# Brainstorm\n\nFinal text\n")]
        assert not (workspace / ".foundry" / "current-story.md").exists()


class TestPipelineWiring:
    def test_default_pipeline_starts_with_brainstorm(self) -> None:
        phases = build_pipeline(FakeRunner("x"))
        assert phases[0].name == "brainstorm"
        assert all(phase.checkpoint for phase in phases)

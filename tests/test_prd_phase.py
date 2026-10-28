from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from foundry.agent import ClaudeCodeRunner
from foundry.context import Artifact, ArtifactType, CodeFailure, RunContext
from foundry.orchestrator import Orchestrator
from foundry.phases import Phase
from foundry.pipeline import build_pipeline
from foundry.planning import SKILL_PRD, BrainstormPhase, PrdPhase
from tests.helpers import FakeRunner, ScriptedCheckpoint

BRAINSTORM = Artifact("# Brainstorm\n\nTheme: offline-first sync\n", ArtifactType.BRAINSTORM)


class TestPhase:
    def test_is_a_checkpointed_phase(self) -> None:
        phase = PrdPhase(FakeRunner("x"))
        assert isinstance(phase, Phase)
        assert phase.name == "prd" and phase.checkpoint is True

    async def test_invokes_the_prd_agent_with_the_brainstorm_artifact_as_input(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# PRD\n\nFR-1: sync\n")
        out = await PrdPhase(runner).run(BRAINSTORM, RunContext(idea="a todo app"))

        prompt = runner.prompts[0]
        assert SKILL_PRD in prompt
        assert BRAINSTORM.content.strip() in prompt  # inlined, not a path reference
        assert "a todo app" in prompt
        assert out.type is ArtifactType.PRD
        assert out.content == "# PRD\n\nFR-1: sync\n"

    async def test_empty_document_is_a_code_failure(self, workspace: Path) -> None:
        with pytest.raises(CodeFailure):
            await PrdPhase(FakeRunner("")).run(BRAINSTORM, RunContext(idea="x"))


class TestPipelineWithCheckpoints:
    async def test_prd_receives_the_human_edited_brainstorm(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", "# PRD\n")
        checkpoint = ScriptedCheckpoint({"brainstorm": lambda t: t + "Theme added by human\n"})
        await Orchestrator([BrainstormPhase(runner), PrdPhase(runner)], checkpoint=checkpoint).run(
            RunContext(idea="x")
        )
        assert "Theme added by human" in runner.prompts[1]

    async def test_prd_is_checkpointed_edited_and_the_edit_is_what_flows_on(
        self, workspace: Path
    ) -> None:
        seen: list[str] = []

        class Next:
            name, checkpoint = "next", False

            async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
                seen.append(input.content)
                return input

        runner = FakeRunner("# Brainstorm\n", "# PRD\n\nFR-1\n")
        checkpoint = ScriptedCheckpoint({"prd": lambda t: t.replace("FR-1", "FR-1 (edited)")})
        ctx = RunContext(idea="x")
        await Orchestrator(
            [BrainstormPhase(runner), PrdPhase(runner), Next()], checkpoint=checkpoint
        ).run(ctx)

        assert checkpoint.phases == ["brainstorm", "prd"]
        assert dict(checkpoint.presented)["prd"] == "# PRD\n\nFR-1\n"
        assert seen == ["# PRD\n\nFR-1 (edited)\n"]
        assert ctx.upstream["prd"].content == "# PRD\n\nFR-1 (edited)\n"

    async def test_provenance_is_recorded_after_approval(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", "# PRD\n")
        checkpoint = ScriptedCheckpoint({"prd": lambda t: t + "more\n"})
        await Orchestrator([BrainstormPhase(runner), PrdPhase(runner)], checkpoint=checkpoint).run(
            RunContext(idea="x")
        )
        data = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
        approved = Artifact("# PRD\nmore\n", ArtifactType.PRD)
        assert data["checkpoints"]["prd"]["approved_without_edits"] is False
        assert data["artifacts"]["prd"]["artifact_hash"] == approved.sha256_hash

    async def test_streams_prd_output_through_the_real_runner(
        self, workspace: Path, fake_claude
    ) -> None:
        command, record = fake_claude(deltas=["# P", "RD\n"], delay=0.1, result="# PRD\n")
        streamed: list[str] = []
        out = await PrdPhase(ClaudeCodeRunner(command, on_text=streamed.append)).run(
            BRAINSTORM, RunContext(idea="x")
        )
        assert streamed == ["# P", "RD\n"] and out.content == "# PRD\n"


class TestPipelineWiring:
    def test_prd_follows_brainstorm(self) -> None:
        assert [p.name for p in build_pipeline(FakeRunner("x"))][:2] == ["brainstorm", "prd"]

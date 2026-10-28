from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from foundry.context import Artifact, ArtifactType, CodeFailure, EnvFailure, RunContext
from foundry.lock import LockFile
from foundry.orchestrator import Orchestrator
from foundry.phases import Phase, PhaseHooks
from foundry.pipeline import build_pipeline
from foundry.planning import (
    SKILL_ARCHITECTURE,
    ArchitecturePhase,
    BrainstormPhase,
    PrdPhase,
    extract_test_runner,
)
from foundry.preflight import PreflightPhase
from tests.helpers import FakeRunner, ScriptedCheckpoint

ARCH = (
    "# Architecture\n\n## Stack\n\nPython 3.11\n\n"
    "## Test Runner\n\n```bash\npytest -q\n```\n\n## Deployment\n\nDocker\n"
)
PRD = (
    "# PRD\n\nFR-1: users log in.\nFR-2: errors are retried.\nFR-3: data is persisted.\n"
    "NFR-1: events are logged.\n"
)
CLEAN = '{"contradictions": [], "traceability_gaps": []}'
CONFLICT = (
    '{"contradictions": [{"requirements": ["FR-1"], "description": "clash"}], "traceability_gaps": []}'
)


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


def pipeline(runner: FakeRunner) -> list:
    return [
        BrainstormPhase(runner),
        PrdPhase(runner),
        PreflightPhase(runner),
        ArchitecturePhase(runner),
    ]


# ------------------------------------------------------------------ extraction (FR-9)


class TestExtractTestRunner:
    def test_reads_the_fenced_command(self) -> None:
        assert extract_test_runner(ARCH) == "pytest -q"

    @pytest.mark.parametrize(
        "doc",
        [
            "## Test Runner\n\n~~~\npytest -q\n~~~\n",
            "## Test Runner\n\n```\npytest -q\n```\n",
            "# test runner\n\n```sh\npytest -q\n```\n",
            "### Test Runner\n\n```sh\npytest -q\n```\n",
            "## Test Runner\n\nRun `pytest -q` from the repo root.\n",
            "## Test Runner\n\n```sh\n$ pytest -q\n```\n",
            "## Test Runner\r\n\r\n```bash\r\npytest -q\r\n```\r\n",
            "## Test Runner ##\n\n```bash\npytest -q\n```\n",
        ],
    )
    def test_accepts_reasonable_variations(self, doc: str) -> None:
        assert extract_test_runner(doc) == "pytest -q"

    def test_keeps_the_whole_command_line(self) -> None:
        doc = "## Test Runner\n\n```bash\ncd app && npm test -- --ci\n```\n"
        assert extract_test_runner(doc) == "cd app && npm test -- --ci"

    def test_subheadings_belong_to_the_section(self) -> None:
        doc = "## Test Runner\n\n### Command\n\n```bash\npytest -q\n```\n\n## Next\n"
        assert extract_test_runner(doc) == "pytest -q"

    def test_stops_at_the_next_heading_of_equal_rank(self) -> None:
        doc = "## Test Runner\n\nTBD.\n\n## Deploy\n\n```bash\nmake deploy\n```\n"
        with pytest.raises(CodeFailure, match="no command"):
            extract_test_runner(doc)

    def test_missing_section_says_how_to_fix_it(self) -> None:
        with pytest.raises(CodeFailure, match=r"no '## Test Runner' section"):
            extract_test_runner("# Architecture\n\n## Stack\n\nPython\n")

    def test_multiple_commands_are_refused_not_guessed(self) -> None:
        doc = "## Test Runner\n\n```bash\npytest\nflake8\n```\n"
        with pytest.raises(CodeFailure, match="exactly one command"):
            extract_test_runner(doc)

    def test_empty_fence_is_refused(self) -> None:
        with pytest.raises(CodeFailure, match="no command"):
            extract_test_runner("## Test Runner\n\n```bash\n\n```\n")

    def test_absurdly_long_command_is_refused(self) -> None:
        with pytest.raises(CodeFailure, match="too long"):
            extract_test_runner("## Test Runner\n\n```bash\n" + "x" * 600 + "\n```\n")


# ---------------------------------------------------------------------- the phase


class TestPhase:
    def test_is_a_checkpointed_phase_with_hooks(self) -> None:
        phase = ArchitecturePhase(FakeRunner("x"))
        assert isinstance(phase, Phase) and isinstance(phase, PhaseHooks)
        assert phase.name == "architecture" and phase.checkpoint is True

    async def test_agent_is_given_the_approved_prd_and_the_test_runner_contract(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner(ARCH)
        ctx = RunContext(idea="a todo app", workspace_path=workspace)
        ctx.upstream["prd"] = Artifact(PRD, ArtifactType.PRD)
        # the phase's `input` is the pre-flight report, not the PRD
        report = Artifact("# Pre-flight Report\n", ArtifactType.PREFLIGHT)
        out = await ArchitecturePhase(runner).run(report, ctx)

        prompt = runner.prompts[0]
        assert SKILL_ARCHITECTURE in prompt
        assert PRD.strip() in prompt and "Pre-flight Report" not in prompt
        assert "## Test Runner" in prompt and "exits non-zero" in prompt
        assert out.type is ArtifactType.ARCHITECTURE

    async def test_needs_the_approved_prd(self, workspace: Path) -> None:
        with pytest.raises(EnvFailure, match="approved PRD"):
            await ArchitecturePhase(FakeRunner(ARCH)).run(
                Artifact("x", ArtifactType.PREFLIGHT), RunContext(idea="x", workspace_path=workspace)
            )

    async def test_an_architecture_without_a_test_runner_fails_fast(self, workspace: Path) -> None:
        ctx = RunContext(idea="x", workspace_path=workspace)
        ctx.upstream["prd"] = Artifact(PRD, ArtifactType.PRD)
        with pytest.raises(CodeFailure, match="Test Runner"):
            await ArchitecturePhase(FakeRunner("# Architecture\n\nNo tests here.\n")).run(
                Artifact("x", ArtifactType.PREFLIGHT), ctx
            )

    def test_derived_section_targets_a_run_subtable(self) -> None:
        arch = Artifact(ARCH, ArtifactType.ARCHITECTURE)
        assert ArchitecturePhase(FakeRunner("x")).derived_sections(arch) == {
            "run.test_runner": {"command": "pytest -q", "source_hash": arch.sha256_hash}
        }


# --------------------------------------------------------------------- in the pipeline


class TestInPipeline:
    async def test_test_runner_is_stored_in_the_lock_without_rewriting_run(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH)
        checkpoint = ScriptedCheckpoint()
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))

        assert checkpoint.phases == ["brainstorm", "prd", "preflight", "architecture"]
        data = lock_data(workspace)
        assert data["run"]["test_runner"]["command"] == "pytest -q"
        assert data["run"]["status"] == "in_progress"  # [run] itself untouched
        assert LockFile.for_workspace(workspace).test_runner() == "pytest -q"

    async def test_lock_stays_append_only_when_the_test_runner_lands(self, workspace: Path) -> None:
        seen: dict[str, bytes] = {}

        class Snapshot(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                if (metadata or {}).get("phase") == "architecture":
                    seen["before"] = (workspace / "foundry.lock").read_bytes()
                return await super().present(artifact, metadata)

        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH)
        await Orchestrator(pipeline(runner), checkpoint=Snapshot()).run(RunContext(idea="x"))
        assert (workspace / "foundry.lock").read_bytes().startswith(seen["before"])

    async def test_the_human_edited_command_is_what_gets_recorded(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH)
        checkpoint = ScriptedCheckpoint({"architecture": lambda t: t.replace("pytest -q", "pytest tests/ -q")})
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))
        entry = lock_data(workspace)["run"]["test_runner"]
        edited = Artifact(ARCH.replace("pytest -q", "pytest tests/ -q"), ArtifactType.ARCHITECTURE)
        assert entry["command"] == "pytest tests/ -q"
        assert entry["source_hash"] == edited.sha256_hash

    async def test_removing_the_section_at_the_checkpoint_records_nothing(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH)
        checkpoint = ScriptedCheckpoint({"architecture": lambda t: t.split("## Test Runner")[0]})
        with pytest.raises(CodeFailure, match="Test Runner"):
            await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))
        data = lock_data(workspace)
        assert "architecture" not in data["checkpoints"] and "architecture" not in data["artifacts"]
        assert "test_runner" not in data["run"]
        assert LockFile.for_workspace(workspace).is_in_progress()

    async def test_architecture_runs_only_after_preflight_is_resolved_and_sees_the_edited_prd(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CONFLICT, CLEAN, ARCH)
        checkpoint = ScriptedCheckpoint({"prd.revision-1": lambda t: t + "FR-9: conflict resolved.\n"})
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))

        assert checkpoint.phases == [
            "brainstorm", "prd", "preflight", "prd.revision-1", "preflight.revision-1", "architecture",
        ]
        assert [r.name for r in runner.requests] == [
            "brainstorm", "prd", "preflight", "preflight", "architecture",
        ]
        assert "FR-9: conflict resolved." in runner.prompts[-1]

    async def test_commit_for_the_derived_section_follows_the_format(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH)
        await Orchestrator(pipeline(runner), checkpoint=ScriptedCheckpoint()).run(RunContext(idea="x"))
        subjects = subprocess.run(
            ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, encoding="utf-8"
        ).stdout.splitlines()
        assert "foundry: derived section recorded — run.test_runner" in subjects

    def test_test_runner_is_none_until_recorded(self, workspace: Path) -> None:
        lock = LockFile.for_workspace(workspace)
        assert lock.test_runner() is None
        lock.init_run(RunContext(idea="x", workspace_path=workspace))
        assert lock.test_runner() is None
        lock.append_section("run.test_runner", {"command": "pytest -q", "source_hash": "sha256:1"})
        assert lock.test_runner() == "pytest -q"
        assert lock.status() == "in_progress"

    def test_architecture_follows_preflight_in_the_pipeline(self) -> None:
        names = [p.name for p in build_pipeline(FakeRunner("x"))]
        assert names[:4] == ["brainstorm", "prd", "preflight", "architecture"]

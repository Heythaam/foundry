from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from foundry.artifacts import PERSISTED_PHASES, artifact_path, write_artifact
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    RunContext,
    sha256_of,
)
from foundry.orchestrator import Orchestrator
from foundry.planning import (
    ArchitecturePhase,
    BrainstormPhase,
    EpicsPhase,
    PrdPhase,
)
from foundry.preflight import PreflightPhase
from foundry.provenance import ProvenanceRecorder
from tests.helpers import FakeRunner, ScriptedCheckpoint

BRAINSTORM = "# Brainstorm\n\nIdeas.\n"
PRD = "# PRD\n\nUsers log in; errors are retried; data is persisted; events are logged.\n"
ARCH = "# Architecture\n\n## Test Runner\n\n```bash\npytest -q\n```\n"
PLAN = "### Epic 1: Foundations\n\n### Story 1.1: Scaffold\n\nAs a dev, I want a scaffold.\n"
CLEAN = '{"contradictions": [], "traceability_gaps": []}'
CONFLICT = (
    '{"contradictions": [{"requirements": ["FR-1"], "description": "clash"}], "traceability_gaps": []}'
)

FILES = {
    "brainstorm": ".foundry/artifacts/brainstorm.md",
    "prd": ".foundry/artifacts/prd.md",
    "architecture": ".foundry/artifacts/architecture.md",
    "epics": ".foundry/artifacts/epics.md",
}


def pipeline(runner: FakeRunner) -> list:
    return [
        BrainstormPhase(runner),
        PrdPhase(runner),
        PreflightPhase(runner),
        ArchitecturePhase(runner),
        EpicsPhase(runner),
    ]


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- the writer


class TestWriteArtifact:
    def test_creates_the_directory_and_writes_the_exact_content(self, tmp_path: Path) -> None:
        assert not (tmp_path / ".foundry").exists()
        artifact = Artifact("# Doc\n\ncafé — ☕\n", ArtifactType.PRD)
        path = write_artifact(tmp_path, "prd", artifact)

        assert path == tmp_path / ".foundry" / "artifacts" / "prd.md"
        assert path.read_bytes() == "# Doc\n\ncafé — ☕\n".encode("utf-8")

    def test_line_endings_are_not_translated(self, tmp_path: Path) -> None:
        artifact = Artifact("a\r\nb\nc\r\n", ArtifactType.PRD)  # whatever the editor saved
        assert write_artifact(tmp_path, "prd", artifact).read_bytes() == b"a\r\nb\nc\r\n"

    def test_a_later_approval_replaces_the_earlier_one(self, tmp_path: Path) -> None:
        write_artifact(tmp_path, "prd", Artifact("v1\n", ArtifactType.PRD))
        write_artifact(tmp_path, "prd", Artifact("v2\n", ArtifactType.PRD))
        assert artifact_path(tmp_path, "prd").read_text(encoding="utf-8") == "v2\n"

    def test_leaves_no_temp_files(self, tmp_path: Path) -> None:
        write_artifact(tmp_path, "prd", Artifact("x\n", ArtifactType.PRD))
        assert [p.name for p in artifact_path(tmp_path, "prd").parent.iterdir()] == ["prd.md"]

    def test_a_failed_write_keeps_the_previous_file_and_is_classified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_artifact(tmp_path, "prd", Artifact("v1\n", ArtifactType.PRD))

        def boom(src: str, dst: str) -> None:
            raise PermissionError("file in use")

        monkeypatch.setattr("foundry.artifacts.os.replace", boom)
        with pytest.raises(EnvFailure, match="cannot write approved prd"):
            write_artifact(tmp_path, "prd", Artifact("v2\n", ArtifactType.PRD))

        assert artifact_path(tmp_path, "prd").read_text(encoding="utf-8") == "v1\n"
        assert [p.name for p in artifact_path(tmp_path, "prd").parent.iterdir()] == ["prd.md"]

    def test_unwritable_location_is_an_environment_failure(self, tmp_path: Path) -> None:
        (tmp_path / ".foundry").write_text("a file where the directory should be", encoding="utf-8")
        with pytest.raises(EnvFailure):
            write_artifact(tmp_path, "prd", Artifact("x\n", ArtifactType.PRD))

    def test_the_persisted_phases_are_the_four_planning_artifacts(self) -> None:
        assert PERSISTED_PHASES == {"brainstorm", "prd", "architecture", "epics"}


# ------------------------------------------------------------ after approval


class TestAfterApproval:
    async def test_each_phase_writes_its_approved_content_including_human_edits(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint(
            {
                "brainstorm": lambda t: t + "Human brainstorm note.\n",
                "prd": lambda t: t + "FR-9: human requirement.\n",
                "architecture": lambda t: t + "\n## Notes\n\nHuman architecture note.\n",
                "epics": lambda t: t + "\n### Story 1.2: Added by human\n\nAs a dev, I want more.\n",
            }
        )
        ctx = RunContext(idea="x")
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(ctx)

        for phase, relative in FILES.items():
            path = workspace / relative
            assert path.is_file(), f"{relative} was not written"
            approved = ctx.upstream[phase].content
            assert path.read_bytes() == approved.encode("utf-8")
        assert "Human brainstorm note." in (workspace / FILES["brainstorm"]).read_text(encoding="utf-8")
        assert "Human architecture note." in (workspace / FILES["architecture"]).read_text(encoding="utf-8")
        assert "Story 1.2" in (workspace / FILES["epics"]).read_text(encoding="utf-8")

    async def test_file_hash_matches_the_hash_recorded_in_the_lock(self, workspace: Path) -> None:
        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint({"prd": lambda t: t + "edited\n"})
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))

        data = lock_data(workspace)
        for phase, relative in FILES.items():
            assert sha256_of((workspace / relative).read_bytes()) == data["artifacts"][phase]["artifact_hash"]

    async def test_the_pre_flight_report_is_not_persisted(self, workspace: Path) -> None:
        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        await Orchestrator(pipeline(runner), checkpoint=ScriptedCheckpoint()).run(RunContext(idea="x"))
        assert sorted(p.name for p in (workspace / ".foundry" / "artifacts").iterdir()) == [
            "architecture.md", "brainstorm.md", "epics.md", "prd.md",
        ]

    async def test_nothing_is_on_disk_until_the_human_approves(self, workspace: Path) -> None:
        seen: dict[str, tuple[bool, list[str]]] = {}

        class Observing(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                phase = str((metadata or {})["phase"])
                directory = workspace / ".foundry" / "artifacts"
                existing = sorted(p.stem for p in directory.glob("*.md")) if directory.exists() else []
                seen[phase] = ((directory / f"{phase}.md").exists(), existing)
                return await super().present(artifact, metadata)

        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        await Orchestrator(pipeline(runner), checkpoint=Observing()).run(RunContext(idea="x"))

        assert seen["brainstorm"] == (False, [])
        assert seen["prd"] == (False, ["brainstorm"])
        assert seen["architecture"] == (False, ["brainstorm", "prd"])
        assert seen["epics"] == (False, ["architecture", "brainstorm", "prd"])

    async def test_an_unapproved_checkpoint_leaves_no_file(self, workspace: Path) -> None:
        class Abort:
            async def present(self, artifact, metadata=None):
                raise EnvFailure("editor crashed")

        with pytest.raises(EnvFailure):
            await Orchestrator([BrainstormPhase(FakeRunner(BRAINSTORM))], checkpoint=Abort()).run(
                RunContext(idea="x")
            )
        assert not (workspace / ".foundry" / "artifacts" / "brainstorm.md").exists()

    async def test_a_rejected_approval_writes_no_file(self, workspace: Path) -> None:
        """Deleting the Test Runner section is rejected, so architecture.md must not appear."""
        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH)
        checkpoint = ScriptedCheckpoint({"architecture": lambda t: t.split("## Test Runner")[0]})
        with pytest.raises(CodeFailure):
            await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))
        assert not (workspace / FILES["architecture"]).exists()
        assert (workspace / FILES["prd"]).exists()  # earlier approvals are untouched

    async def test_the_file_exists_before_the_lock_says_approved(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_disk_when_recorded: dict[str, bool] = {}
        real = ProvenanceRecorder.record_checkpoint

        async def spy(self, phase, result, extra=None):
            on_disk_when_recorded[phase] = (workspace / ".foundry" / "artifacts" / f"{phase}.md").exists()
            return await real(self, phase, result, extra)

        monkeypatch.setattr(ProvenanceRecorder, "record_checkpoint", spy)
        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        await Orchestrator(pipeline(runner), checkpoint=ScriptedCheckpoint()).run(RunContext(idea="x"))

        for phase in FILES:
            assert on_disk_when_recorded[phase] is True, phase
        assert on_disk_when_recorded["preflight"] is False  # not a persisted phase

    async def test_a_prd_reworked_after_preflight_replaces_prd_md(self, workspace: Path) -> None:
        runner = FakeRunner(BRAINSTORM, PRD, CONFLICT, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint({"prd.revision-1": lambda t: t + "FR-9: conflict resolved.\n"})
        ctx = RunContext(idea="x")
        await Orchestrator(pipeline(runner), checkpoint=checkpoint).run(ctx)

        text = (workspace / FILES["prd"]).read_text(encoding="utf-8")
        assert text == PRD + "FR-9: conflict resolved.\n" == ctx.upstream["prd"].content
        assert not list((workspace / ".foundry" / "artifacts").glob("*revision*"))

    async def test_files_are_not_committed_to_git(self, workspace: Path) -> None:
        import subprocess

        runner = FakeRunner(BRAINSTORM, PRD, CLEAN, ARCH, PLAN)
        await Orchestrator(pipeline(runner), checkpoint=ScriptedCheckpoint()).run(RunContext(idea="x"))
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=workspace, capture_output=True, text=True
        ).stdout.split()
        assert tracked == ["foundry.lock"]

    async def test_a_write_failure_stops_the_run_before_anything_is_recorded(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_replace = os.replace

        def fail_for_artifacts_only(src: str, dst: str) -> None:
            if str(dst).endswith(".md"):  # `os` is shared, so leave the lock's writes alone
                raise OSError("disk full")
            real_replace(src, dst)

        monkeypatch.setattr("foundry.artifacts.os.replace", fail_for_artifacts_only)
        with pytest.raises(EnvFailure, match="disk full"):
            await Orchestrator(
                [BrainstormPhase(FakeRunner(BRAINSTORM))], checkpoint=ScriptedCheckpoint()
            ).run(RunContext(idea="x"))
        data = lock_data(workspace)
        assert "checkpoints" not in data and "artifacts" not in data
        assert list((workspace / ".foundry" / "artifacts").glob("*")) == []  # no file, no temp

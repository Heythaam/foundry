from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from foundry.context import Artifact, ArtifactType, CodeFailure, RunContext
from foundry.epics import parse_epics, parse_stories, validate_plan
from foundry.orchestrator import Orchestrator
from foundry.phases import Phase, PhaseHooks
from foundry.pipeline import build_pipeline
from foundry.planning import (
    SKILL_EPICS,
    ArchitecturePhase,
    BrainstormPhase,
    EpicsPhase,
    PrdPhase,
)
from foundry.preflight import PreflightPhase
from tests.helpers import FakeRunner, ScriptedCheckpoint

ARCH = "# Architecture\n\n## Test Runner\n\n```bash\npytest -q\n```\n"
PRD = "# PRD\n\nUsers log in; errors are retried; data is persisted; events are logged.\n"
CLEAN = '{"contradictions": [], "traceability_gaps": []}'

PLAN = """\
# Epics

## Epic List

### Epic 1: Foundations
Sets things up.

### Epic 2: Features
Builds things.

---

## Epic 1 Stories

### Story 1.1: Scaffold

As a developer, I want a scaffold, so that I can start.

**Acceptance Criteria:**
- it exists

### Story 1.2: Config

As a developer, I want config.

#### Notes
Sub-heading belongs to the story.

---

## Epic 2 Stories

### Story 2.1: Feature A

As a user, I want A.
"""

REPO_EPICS = Path(__file__).resolve().parent.parent / "docs" / "epics.md"


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


def full_pipeline(runner: FakeRunner) -> list:
    return [
        BrainstormPhase(runner),
        PrdPhase(runner),
        PreflightPhase(runner),
        ArchitecturePhase(runner),
        EpicsPhase(runner),
    ]


# --------------------------------------------------------------------------- parser


class TestParser:
    def test_groups_stories_under_their_epics_in_order(self) -> None:
        epics = parse_epics(PLAN)
        assert [(e.number, e.title, e.story_ids) for e in epics] == [
            (1, "Foundations", ["1.1", "1.2"]),
            (2, "Features", ["2.1"]),
        ]

    def test_story_text_is_exact_and_stops_at_the_next_story_or_rule(self) -> None:
        story = parse_stories(PLAN)[0]
        assert story.raw.startswith("### Story 1.1: Scaffold")
        assert "**Acceptance Criteria:**\n- it exists" in story.raw
        assert "Story 1.2" not in story.raw

    def test_subheadings_stay_inside_the_story_and_trailing_rules_are_dropped(self) -> None:
        story = parse_stories(PLAN)[1]
        assert "#### Notes" in story.raw and "Sub-heading belongs to the story." in story.raw
        assert not story.raw.rstrip().endswith("---")

    def test_epic_without_a_heading_gets_a_default_title(self) -> None:
        epics = parse_epics("### Story 3.1: Lonely\n\nText.\n")
        assert (epics[0].number, epics[0].title) == (3, "Epic 3")

    def test_windows_line_endings(self) -> None:
        assert parse_epics(PLAN.replace("\n", "\r\n"))[0].story_ids == ["1.1", "1.2"]

    def test_duplicate_story_ids_are_rejected(self) -> None:
        with pytest.raises(CodeFailure, match=r"Story 1\.1 appears more than once"):
            parse_stories("### Story 1.1: A\n\nx\n\n### Story 1.1: B\n\ny\n")

    @pytest.mark.skipif(not REPO_EPICS.exists(), reason="docs/epics.md not present")
    def test_parses_this_projects_own_epics_document(self) -> None:
        epics = parse_epics(REPO_EPICS.read_text(encoding="utf-8"))
        assert [(e.number, len(e.stories)) for e in epics] == [
            (1, 6), (2, 6), (3, 6), (4, 5), (5, 4), (6, 1), (7, 2),
        ]
        assert epics[0].title == "Core Pipeline Infrastructure"
        assert epics[1].stories[2].title == "Pre-flight Analysis Phase"


class TestValidatePlan:
    def test_accepts_a_complete_plan(self) -> None:
        assert len(validate_plan(PLAN)) == 2

    def test_rejects_a_document_with_no_epics(self) -> None:
        with pytest.raises(CodeFailure, match="no epics or stories"):
            validate_plan("# Just prose\n\nNothing structured.\n")

    def test_rejects_an_epic_with_no_stories(self) -> None:
        with pytest.raises(CodeFailure, match=r"Epic\(s\) 2 have no stories"):
            validate_plan("### Epic 1: A\n\n### Story 1.1: S\n\nx\n\n### Epic 2: B\n\nempty\n")


# ---------------------------------------------------------------------------- phase


class TestPhase:
    def test_is_a_checkpointed_phase_with_hooks(self) -> None:
        phase = EpicsPhase(FakeRunner("x"))
        assert isinstance(phase, Phase) and isinstance(phase, PhaseHooks)
        assert phase.name == "epics" and phase.checkpoint is True

    async def test_agent_is_invoked_with_the_architecture_artifact(self, workspace: Path) -> None:
        runner = FakeRunner(PLAN)
        arch = Artifact(ARCH, ArtifactType.ARCHITECTURE)
        out = await EpicsPhase(runner).run(arch, RunContext(idea="a todo app", workspace_path=workspace))

        prompt = runner.prompts[0]
        assert SKILL_EPICS in prompt and ARCH.strip() in prompt
        assert "### Epic N: Title" in prompt and "### Story N.M: Title" in prompt
        assert out.type is ArtifactType.EPICS and out.content == PLAN.strip() + "\n"

    async def test_includes_the_approved_prd_when_there_is_one(self, workspace: Path) -> None:
        ctx = RunContext(idea="x", workspace_path=workspace)
        ctx.upstream["prd"] = Artifact(PRD, ArtifactType.PRD)
        runner = FakeRunner(PLAN)
        await EpicsPhase(runner).run(Artifact(ARCH, ArtifactType.ARCHITECTURE), ctx)
        assert PRD.strip() in runner.prompts[0]

    @pytest.mark.parametrize("bad", ["Just some prose.", "### Epic 1: Empty\n\nNo stories.\n"])
    async def test_an_unusable_plan_fails_before_the_human_sees_it(
        self, workspace: Path, bad: str
    ) -> None:
        with pytest.raises(CodeFailure):
            await EpicsPhase(FakeRunner(bad)).run(
                Artifact(ARCH, ArtifactType.ARCHITECTURE), RunContext(idea="x", workspace_path=workspace)
            )

    def test_derived_plan_section_is_the_story_manifest(self) -> None:
        plan = Artifact(PLAN, ArtifactType.EPICS)
        assert EpicsPhase(FakeRunner("x")).derived_sections(plan) == {
            "plan": {
                "epic_count": 2,
                "story_count": 3,
                "story_ids": ["1.1", "1.2", "2.1"],
                "source_hash": plan.sha256_hash,
            }
        }


# ------------------------------------------------------------------------ pipeline


class TestInPipeline:
    async def test_epics_are_generated_from_the_architecture_and_checkpointed(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint()
        ctx = RunContext(idea="x")
        result = await Orchestrator(full_pipeline(runner), checkpoint=checkpoint).run(ctx)

        assert checkpoint.phases == ["brainstorm", "prd", "preflight", "architecture", "epics"]
        assert ARCH.strip() in runner.prompts[-1]
        assert dict(checkpoint.presented)["epics"] == PLAN.strip() + "\n"
        assert result.artifact.type is ArtifactType.EPICS  # planning pipeline output

    async def test_human_edits_to_the_plan_are_what_gets_recorded(self, workspace: Path) -> None:
        extra = "\n### Story 2.2: Feature B\n\nAs a user, I want B.\n"
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint({"epics": lambda t: t + extra})
        ctx = RunContext(idea="x")
        await Orchestrator(full_pipeline(runner), checkpoint=checkpoint).run(ctx)

        data = lock_data(workspace)
        assert data["plan"]["story_ids"] == ["1.1", "1.2", "2.1", "2.2"]
        assert data["plan"]["story_count"] == 4 and data["plan"]["epic_count"] == 2
        assert data["plan"]["source_hash"] == data["artifacts"]["epics"]["artifact_hash"]
        assert "Story 2.2" in ctx.upstream["epics"].content

    async def test_provenance_follows_approval_and_planning_is_complete(self, workspace: Path) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH, PLAN)
        await Orchestrator(full_pipeline(runner), checkpoint=ScriptedCheckpoint()).run(
            RunContext(idea="x")
        )
        data = lock_data(workspace)
        assert {"brainstorm", "prd", "preflight", "architecture", "epics"} <= set(data["artifacts"])
        assert data["run"]["test_runner"]["command"] == "pytest -q"
        assert "completed" in data  # nothing left to run: repo creation arrives with Epic 3

    async def test_breaking_the_plan_format_at_the_checkpoint_records_nothing(
        self, workspace: Path
    ) -> None:
        runner = FakeRunner("# Brainstorm\n", PRD, CLEAN, ARCH, PLAN)
        checkpoint = ScriptedCheckpoint({"epics": lambda t: "I deleted everything\n"})
        with pytest.raises(CodeFailure, match="no epics or stories"):
            await Orchestrator(full_pipeline(runner), checkpoint=checkpoint).run(RunContext(idea="x"))
        data = lock_data(workspace)
        assert "epics" not in data["checkpoints"] and "plan" not in data

    def test_full_planning_pipeline_order(self) -> None:
        assert [p.name for p in build_pipeline(FakeRunner("x"))] == [
            "brainstorm", "prd", "preflight", "architecture", "epics",
        ]

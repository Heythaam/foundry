from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry.cli import app
from foundry.context import CodeFailure, RunContext
from foundry.epics import Epic, parse_epics
from foundry.provenance import ProvenanceRecorder
from foundry.review import (
    EpicReviewer,
    ProjectState,
    ReviewError,
    apply_review,
    render_review,
)
from tests.helpers import ScriptedCheckpoint

PLAN = """\
### Epic 2: Planning Pipeline

### Story 2.1: Brainstorm

As a developer, I want a brainstorm.

**Acceptance Criteria:**
- streams output

### Story 2.2: PRD

As a developer, I want a PRD.

### Story 2.3: Pre-flight

As a developer, I want checks.
"""

runner = CliRunner()


def epic2() -> Epic:
    return parse_epics(PLAN)[0]


def pieces(doc: str) -> tuple[str, list[str]]:
    """Split a review document into its preamble and its story sections."""
    parts = re.split(r"(?m)^(?=### Story )", doc)
    return parts[0], [p.rstrip() + "\n\n" for p in parts[1:]]


def rebuild(preamble: str, stories: list[str]) -> str:
    return preamble + "".join(stories)


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


def git_count(workspace: Path) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=workspace, capture_output=True, text=True)
    return int(out.stdout.strip())


async def reviewer_for(workspace: Path, checkpoint, enabled: bool = True) -> EpicReviewer:
    recorder = ProvenanceRecorder(workspace)
    await recorder.record_run_start(RunContext(idea="x", workspace_path=workspace))
    return EpicReviewer(checkpoint, recorder, enabled)


STATE = ProjectState(
    completed_story_ids=("1.1", "1.2"), merged_epics=(1,), remaining_epics=(3, 4)
)


# ----------------------------------------------------------------- the review document


class TestRenderReview:
    def test_shows_the_stories_and_the_project_state(self) -> None:
        doc = render_review(epic2(), STATE)
        for story in epic2().stories:
            assert story.raw in doc
        assert "Completed stories (2): 1.1, 1.2" in doc
        assert "Merged epics: 1" in doc
        assert "Epics after this one: 3, 4" in doc
        assert "This epic: 3 stories" in doc
        assert doc.startswith("# Review before Epic 2: Planning Pipeline")

    def test_empty_state_reads_naturally(self) -> None:
        doc = render_review(epic2(), ProjectState())
        assert "Completed stories (0): none yet" in doc and "Epics after this one: none" in doc

    def test_untouched_document_applies_to_an_identical_epic(self) -> None:
        review = apply_review(render_review(epic2(), STATE), epic2())
        assert review.epic == epic2()
        assert (review.skipped, review.edited, review.reordered) == ((), (), False)


class TestApplyReview:
    def doc(self) -> tuple[str, list[str]]:
        return pieces(render_review(epic2(), STATE))

    def test_reorder(self) -> None:
        pre, stories = self.doc()
        review = apply_review(rebuild(pre, [stories[2], stories[0], stories[1]]), epic2())
        assert review.epic.story_ids == ["2.3", "2.1", "2.2"]
        assert review.reordered is True and review.skipped == () and review.edited == ()

    def test_skip_by_deleting_a_section(self) -> None:
        pre, stories = self.doc()
        review = apply_review(rebuild(pre, [stories[0], stories[2]]), epic2())
        assert review.epic.story_ids == ["2.1", "2.3"]
        assert review.skipped == ("2.2",)
        assert review.reordered is False  # removing a story is not reordering the rest

    def test_edit_text_in_place(self) -> None:
        pre, stories = self.doc()
        stories[1] = stories[1].replace("a PRD", "a PRD with an audit trail")
        review = apply_review(rebuild(pre, stories), epic2())
        assert review.edited == ("2.2",)
        assert "audit trail" in review.epic.stories[1].raw
        assert review.epic.stories[0] == epic2().stories[0]

    def test_all_three_at_once(self) -> None:
        pre, stories = self.doc()
        stories[2] = stories[2].replace("checks", "stricter checks")
        review = apply_review(rebuild(pre, [stories[2], stories[0]]), epic2())
        assert review.epic.story_ids == ["2.3", "2.1"]
        assert (review.skipped, review.edited, review.reordered) == (("2.2",), ("2.3",), True)

    def test_skipping_every_story_is_allowed(self) -> None:
        pre, _ = self.doc()
        review = apply_review(pre, epic2())
        assert review.epic.stories == () and review.skipped == ("2.1", "2.2", "2.3")

    def test_edits_above_the_first_story_are_ignored(self) -> None:
        pre, stories = self.doc()
        review = apply_review(rebuild(pre + "Some scribbles of mine.\n\n", stories), epic2())
        assert review.epic == epic2() and review.edited == ()

    def test_deleting_the_marker_comment_is_harmless(self) -> None:
        pre, stories = self.doc()
        pre = re.sub(r"<!--.*?-->", "", pre, flags=re.DOTALL)
        assert apply_review(rebuild(pre, stories), epic2()).epic == epic2()

    def test_adding_or_renumbering_a_story_is_refused(self) -> None:
        pre, stories = self.doc()
        with pytest.raises(ReviewError, match=r"Story 2\.9 is not part of epic 2"):
            apply_review(rebuild(pre, stories + ["### Story 2.9: Sneaky\n\nnew\n"]), epic2())
        with pytest.raises(ReviewError, match=r"Story 3\.1"):
            apply_review(rebuild(pre, [stories[0].replace("2.1", "3.1")]), epic2())

    def test_duplicated_story_is_refused(self) -> None:
        pre, stories = self.doc()
        with pytest.raises(CodeFailure, match="more than once"):
            apply_review(rebuild(pre, stories + [stories[0]]), epic2())


# --------------------------------------------------------------------- the gate


class TestReviewerDisabled:
    async def test_without_the_flag_there_is_no_pause_no_entry_and_no_commit(
        self, workspace: Path
    ) -> None:
        class MustNotOpen(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                raise AssertionError("no checkpoint without --review-epics")

        reviewer = await reviewer_for(workspace, MustNotOpen(), enabled=False)
        before_commits, before_lock = git_count(workspace), (workspace / "foundry.lock").read_bytes()

        review = await reviewer.before_epic(epic2(), STATE)

        assert review.reviewed is False and review.epic == epic2()
        assert git_count(workspace) == before_commits
        assert (workspace / "foundry.lock").read_bytes() == before_lock


class TestReviewerEnabled:
    async def test_presents_the_epic_and_state_at_a_checkpoint(self, workspace: Path) -> None:
        checkpoint = ScriptedCheckpoint()
        reviewer = await reviewer_for(workspace, checkpoint)
        await reviewer.before_epic(epic2(), STATE)

        assert checkpoint.phases == ["epic-2-review"]
        shown = checkpoint.presented[0][1]
        assert "Story 2.1: Brainstorm" in shown and "Completed stories (2): 1.1, 1.2" in shown

    async def test_approving_unchanged_proceeds_with_the_same_epic(self, workspace: Path) -> None:
        reviewer = await reviewer_for(workspace, ScriptedCheckpoint())
        review = await reviewer.before_epic(epic2(), STATE)
        assert review.reviewed is True and review.epic == epic2()
        entry = lock_data(workspace)["checkpoints"]["epic-2-review"]
        assert entry["story_order"] == ["2.1", "2.2", "2.3"] and entry["approved_without_edits"] is True
        assert entry["skipped"] == [] and entry["edited"] == [] and entry["reordered"] is False
        assert entry["epic"] == 2 and entry["completed_stories"] == 2

    async def test_reorder_skip_and_edit_are_applied_and_recorded(self, workspace: Path) -> None:
        def revise(doc: str) -> str:
            pre, stories = pieces(doc)
            stories[2] = stories[2].replace("checks", "stricter checks")
            return rebuild(pre, [stories[2], stories[0]])  # reorder, skip 2.2, edit 2.3

        reviewer = await reviewer_for(workspace, ScriptedCheckpoint({"epic-2-review": revise}))
        review = await reviewer.before_epic(epic2(), STATE)

        assert review.epic.story_ids == ["2.3", "2.1"]
        assert "stricter checks" in review.epic.stories[0].raw
        entry = lock_data(workspace)["checkpoints"]["epic-2-review"]
        assert entry["story_order"] == ["2.3", "2.1"]
        assert entry["skipped"] == ["2.2"] and entry["edited"] == ["2.3"] and entry["reordered"] is True
        assert entry["approved_without_edits"] is False

    async def test_review_is_committed_in_the_forge_log_format(self, workspace: Path) -> None:
        reviewer = await reviewer_for(workspace, ScriptedCheckpoint())
        await reviewer.before_epic(epic2(), STATE)
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=workspace, capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        assert subject == "foundry: checkpoint approved — epic-2-review (no edits)"

    async def test_an_invalid_review_records_nothing(self, workspace: Path) -> None:
        reviewer = await reviewer_for(
            workspace,
            ScriptedCheckpoint({"epic-2-review": lambda d: d + "\n### Story 2.9: Sneaky\n\nx\n"}),
        )
        before = (workspace / "foundry.lock").read_bytes()
        with pytest.raises(ReviewError):
            await reviewer.before_epic(epic2(), STATE)
        assert (workspace / "foundry.lock").read_bytes() == before

    async def test_a_failing_checkpoint_is_classified(self, workspace: Path) -> None:
        class Broken:
            async def present(self, artifact, metadata=None):
                raise RuntimeError("editor exploded")

        reviewer = await reviewer_for(workspace, Broken())
        with pytest.raises(Exception, match="editor exploded") as info:
            await reviewer.before_epic(epic2(), STATE)
        assert type(info.value).__name__ == "EnvFailure"

    async def test_each_epic_gets_its_own_section(self, workspace: Path) -> None:
        reviewer = await reviewer_for(workspace, ScriptedCheckpoint())
        epic_a = epic2()
        epic_b = Epic(3, "Repo", epic_a.stories)  # section name is derived from the epic number
        await reviewer.before_epic(epic_a, STATE)
        await reviewer.before_epic(epic_b, STATE)
        assert {"epic-2-review", "epic-3-review"} <= set(lock_data(workspace)["checkpoints"])


# ------------------------------------------------------------------- flag plumbing


class TestFlag:
    def test_default_is_off(self) -> None:
        assert RunContext(idea="x").review_epics is False

    async def test_for_run_follows_the_run_context(self, workspace: Path) -> None:
        recorder = ProvenanceRecorder(workspace)
        on = EpicReviewer.for_run(RunContext(idea="x", review_epics=True), ScriptedCheckpoint(), recorder)
        off = EpicReviewer.for_run(RunContext(idea="x"), ScriptedCheckpoint(), recorder)
        assert (on.enabled, off.enabled) == (True, False)

    def test_cli_flag_is_recorded_in_the_run_section(self, workspace: Path) -> None:
        result = runner.invoke(app, ["pro", "an idea", "--review-epics"])
        assert result.exit_code == 0, result.output
        assert lock_data(workspace)["run"]["review_epics"] is True

    def test_cli_default_records_false(self, workspace: Path) -> None:
        runner.invoke(app, ["pro", "an idea"])
        assert lock_data(workspace)["run"]["review_epics"] is False

    def test_help_documents_the_flag(self) -> None:
        assert "--review-epics" in runner.invoke(app, ["pro", "--help"]).output

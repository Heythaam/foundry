"""Story 5.2: `foundry status` summarises a run from foundry.lock."""

from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli
from foundry.progress import progress_from_lock
from foundry.status import NO_RUN_MESSAGE, format_duration, render_status
from tests.fake_github import FakeGitHub
from tests.planned import ImplementingRunner
from tests.test_implementation_loop import whole_run
from tests.test_resume import CancelsDuring

cli_runner = CliRunner()
NOW = datetime(2026, 10, 28, 12, 30, 0, tzinfo=timezone.utc)

HEADER = """
[run]
run_id = "run-1"
idea = "a todo tracker"
model = "claude-sonnet-4-5"
started_at = "2026-10-28T10:00:00Z"
review_epics = false
status = "in_progress"

[run.test_runner]
command = "pytest -q"

[plan]
epic_count = 2
story_count = 4
story_ids = ["1.1", "1.2", "2.1", "2.2"]
"""

SCAFFOLDED = """
[repo]
full_name = "me/todo"
[scaffold]
scaffolded_at = "2026-10-28T10:20:00Z"

[epics.epic-1]
branch = "epic-1-core"
story_ids = ["1.1", "1.2"]
started_at = "2026-10-28T10:21:00Z"
"""


def story(epic: int, number: int, at: str) -> str:
    return f'\n[implementation.epic-{epic}.story-{number}]\nstory_id = "{epic}.{number}"\ncompleted_at = "{at}"\n'


def progress_of(text: str):
    result = progress_from_lock(tomllib.loads(text), now=NOW)
    assert result is not None
    return result


class TestWhatItReports:
    def test_a_run_in_the_middle_of_implementation(self) -> None:
        text = HEADER + SCAFFOLDED + story(1, 1, "2026-10-28T11:00:00Z")
        output = render_status(progress_of(text), NOW)

        assert "Run run-1" in output and "a todo tracker" in output
        assert "implementation, epic 1, next story 1.2" in output  # phase / epic / story
        assert "1 of 4 completed" in output  # completed and total stories
        assert "0 of 2 merged" in output
        assert "claude-sonnet-4-5" in output  # model
        assert "Elapsed:     2h 30m" in output  # started 10:00, now 12:30
        assert "Last action: 2026-10-28T11:00:00Z (1h 30m ago)" in output

    def test_counts_follow_the_recorded_stories_not_the_clock(self) -> None:
        text = HEADER + SCAFFOLDED + story(1, 1, "2026-10-28T11:00:00Z") + story(1, 2, "2026-10-28T11:30:00Z")
        progress = progress_of(text)
        assert progress.completed_count == 2 and progress.next_story == "2.1"
        assert progress.implemented == ("1.1", "1.2")

    def test_deferred_and_review_skipped_stories_are_not_left_to_do(self) -> None:
        text = (
            HEADER
            + SCAFFOLDED
            + story(1, 1, "2026-10-28T11:00:00Z")
            + '\n[deferred.epic-1.story-2]\nstory_id = "1.2"\ndeferred_at = "2026-10-28T11:10:00Z"\n'
            + '\n[checkpoints.epic-2-review]\nskipped = ["2.1"]\n'
        )
        progress = progress_of(text)
        assert progress.deferred == ("1.2",) and progress.skipped_in_review == ("2.1",)
        assert progress.next_story == "2.2"
        output = render_status(progress, NOW)
        assert "1 of 4 completed (1 deferred, 1 skipped in review)" in output

    def test_a_finished_run(self) -> None:
        text = (
            HEADER + SCAFFOLDED
            + "".join(story(e, s, "2026-10-28T11:00:00Z") for e, s in ((1, 1), (1, 2), (2, 1), (2, 2)))
            + '\n[epics.epic-1.merged]\npull_request = 1\n\n[epics.epic-2]\nbranch = "epic-2-x"\n'
            + '\n[epics.epic-2.merged]\npull_request = 2\n\n[completed]\ncompleted_at = "2026-10-28T11:45:00Z"\n'
        )
        output = render_status(progress_of(text), NOW)
        assert "Status:      completed" in output and "Phase:       finished" in output
        assert "4 of 4 completed" in output and "2 of 2 merged" in output
        assert "Elapsed:     1h 45m" in output  # to the end of the run, not to now

    def test_a_cancelled_run_says_how_to_continue(self) -> None:
        text = HEADER + SCAFFOLDED + '\n[cancelled]\ncancelled_at = "2026-10-28T11:05:00Z"\nbefore = "story 1.1"\n'
        progress = progress_of(text)
        assert progress.status == "cancelled"
        assert "foundry resume" in render_status(progress, NOW)
        assert "Elapsed:     1h 05m" in render_status(progress, NOW)

    def test_a_run_still_planning(self) -> None:
        text = (
            '[run]\nrun_id = "r"\nidea = "i"\nmodel = "m-1"\nstarted_at = "2026-10-28T10:00:00Z"\n'
            'review_epics = false\n\n[artifacts.prd]\nphase = "prd"\nrecorded_at = "2026-10-28T10:05:00Z"\n'
        )
        output = render_status(progress_of(text), NOW)
        assert "planning (last approved: prd)" in output
        assert "Stories:" not in output  # there is no manifest yet

    def test_a_run_that_has_created_its_repo_but_not_started_building(self) -> None:
        text = HEADER
        assert "creating the repository" in render_status(progress_of(text), NOW)

    def test_a_halt_is_shown_until_the_run_moves_on(self) -> None:
        halt = (
            '\n[halts.halt-1]\nstage = "implementation"\nmessage = "could not find claude"\n'
            'fix = "Install Claude Code"\nhalted_at = "2026-10-28T11:10:00Z"\n'
        )
        stopped = progress_of(HEADER + SCAFFOLDED + story(1, 1, "2026-10-28T11:00:00Z") + halt)
        output = render_status(stopped, NOW)
        assert "stopped by an environment failure" in output
        assert "could not find claude" in output and "Install Claude Code" in output

        moved_on = progress_of(
            HEADER + SCAFFOLDED + story(1, 1, "2026-10-28T11:00:00Z") + halt + story(1, 2, "2026-10-28T12:00:00Z")
        )
        assert moved_on.last_halt is None and "Stopped:" not in render_status(moved_on, NOW)

    def test_a_model_change_and_its_pending_consistency_pass(self) -> None:
        text = (
            HEADER + SCAFFOLDED + story(1, 1, "2026-10-28T11:00:00Z")
            + '\n[model_changes.change-1]\nfrom = "claude-sonnet-4-5"\nto = "claude-sonnet-5-0"\n'
            + 'boundary_story = "1.1"\nstories_completed = 1\nstories_total = 4\nchanged_at = "2026-10-28T11:30:00Z"\n'
        )
        progress = progress_of(text)
        assert progress.model == "claude-sonnet-5-0"
        output = render_status(progress, NOW)
        assert "claude-sonnet-5-0 (changed from claude-sonnet-4-5; a consistency pass follows" in output

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(5, "5s"), (65, "1m 05s"), (3600 + 120, "1h 02m"), (90000, "1d 1h 00m")],
    )
    def test_durations(self, seconds: int, text: str) -> None:
        assert format_duration(seconds) == text


class TestTheCommand:
    def test_no_run_is_a_clear_message(self, workspace: Path) -> None:
        result = cli_runner.invoke(cli.app, ["status"])
        assert result.exit_code == 1
        assert NO_RUN_MESSAGE in result.output

    def test_a_directory_with_an_unreadable_lock_says_so(self, workspace: Path) -> None:
        (workspace / "foundry.lock").write_text("[[[ not toml", encoding="utf-8")
        result = cli_runner.invoke(cli.app, ["status"])
        assert result.exit_code == 1 and "foundry.lock" in result.output

    async def test_a_real_cancelled_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        result = cli_runner.invoke(cli.app, ["status"])

        assert result.exit_code == 0, result.output
        assert "cancelled" in result.output
        assert "1 of 3 completed" in result.output
        assert "implementation, epic 1, next story 1.2" in result.output
        assert "claude-sonnet-4-5" in result.output

    async def test_it_works_while_the_run_is_active_and_reads_committed_state(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        seen: dict[str, str] = {}

        class AsksForStatus(ImplementingRunner):
            async def run(self, request, ctx):
                if request.name == "story 1.2":  # story 1.1 is committed, 1.2 is in flight
                    seen["output"] = cli_runner.invoke(cli.app, ["status"]).output
                return await super().run(request, ctx)

        await whole_run(workspace, tmp_path, AsksForStatus())

        assert "1 of 3 completed" in seen["output"]  # accurate to within the one story in flight
        assert "in progress" in seen["output"]
        after = cli_runner.invoke(cli.app, ["status"]).output
        assert "completed" in after and "3 of 3 completed" in after

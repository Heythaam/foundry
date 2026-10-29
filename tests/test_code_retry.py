"""Story 4.2: a code failure gets exactly one automatic retry, in a fresh session."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.checkpoint import CheckpointHandler
from foundry.context import CodeFailure
from foundry.failures import EscalationCancelled, RetryingFailureHandler
from foundry.lock import LockFile
from tests.fake_github import FakeGitHub
from tests.helpers import ScriptedCheckpoint
from tests.planned import Answers, SequenceRunner, bad, good, started_epic


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def handler(*answers: str) -> tuple[RetryingFailureHandler, Answers]:
    human = Answers(*(answers or ("cancel",)))
    checkpoint: CheckpointHandler = ScriptedCheckpoint({"escalation-story-1.1": human})
    return RetryingFailureHandler(checkpoint), human


class TestRetryThatWorks:
    async def test_the_story_is_committed_as_auto_fixed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )

        assert await stage.implement_story(
            epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder
        )

        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.1 auto-fixed — Todo Model"
        assert len(runner.sessions) == 2

    async def test_the_failure_is_recorded_with_the_retry_outcome(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        section = LockFile.for_workspace(workspace).read()["failures"]["epic-1"]["story-1"]
        assert section["retry_outcome"] == "passed"
        assert section["category"] == "code"
        assert section["retries"] == 1
        assert section["story_id"] == "1.1"
        assert section["exit_code"] == 1
        implementation = LockFile.for_workspace(workspace).read()["implementation"]["epic-1"]["story-1"]
        assert implementation["retries"] == 1

    async def test_failure_entry_and_story_land_in_one_commit(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        before = git(workspace, "rev-list", "--count", "HEAD")
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        assert int(git(workspace, "rev-list", "--count", "HEAD")) == int(before) + 1
        files = git(workspace, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert "foundry.lock" in files and "src/story_1_1.py" in files
        assert "BROKEN" not in (workspace / "src" / "story_1_1.py").read_text(encoding="utf-8")

    async def test_the_lock_holds_a_hash_of_the_output_never_the_output(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        text = (workspace / "foundry.lock").read_text(encoding="utf-8")
        assert "assertion error" not in text
        section = LockFile.for_workspace(workspace).read()["failures"]["epic-1"]["story-1"]
        assert section["output_hash"].startswith("sha256:")

    async def test_a_story_that_passes_first_time_records_no_failure(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner()
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        assert "failures" not in LockFile.for_workspace(workspace).read()
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.1 implemented — Todo Model"


class TestTheRetrySession:
    async def test_it_is_a_new_session_with_the_original_prompt_plus_the_test_output(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        failures, _ = handler()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        await stage.implement_story(epic.stories[0], "ARCH-TEXT", recorder.lock.test_runner(), ctx, recorder)

        first, second = runner.prompts_for("1.1")
        assert "FAILED" not in first and "previous attempt" not in first
        assert second.startswith(first.rstrip("\n"))  # the original prompt, unchanged
        assert "The previous attempt failed" in second
        assert "FAILED story_1_1.py: assertion error" in second  # the failing test output
        assert "ARCH-TEXT" in second and "As a user, I want todos" in second

    async def test_exactly_one_automatic_retry_then_the_human_is_asked(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1")]})  # never gets better
        failures, human = handler("cancel")
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )

        with pytest.raises(EscalationCancelled):
            await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        assert len(runner.sessions) == 2  # the original and one retry: no loop
        assert len(human.documents) == 1

    async def test_a_failed_retry_is_recorded_when_the_human_cancels(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1")]})
        failures, _ = handler("cancel")
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        with pytest.raises(EscalationCancelled):
            await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        section = LockFile.for_workspace(workspace).read()["failures"]["epic-1"]["story-1"]
        assert section["retry_outcome"] == "failed"
        assert "implementation" not in LockFile.for_workspace(workspace).read()

    async def test_the_default_handler_still_stops_at_the_first_red_gate(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), good("1.1")]})
        ctx, recorder, _, stage, epic, _ = await started_epic(workspace, tmp_path, runner=runner)

        with pytest.raises(CodeFailure, match="failed its tests"):
            await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)
        assert len(runner.sessions) == 1


class TestThroughTheWholeLoop:
    async def test_an_epic_with_one_flaky_story_still_merges(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        from foundry.lock import LockFile as Lock
        from tests.fake_github import passing

        runner = SequenceRunner({"1.2": [bad("1.2"), good("1.2")]})
        failures = RetryingFailureHandler(ScriptedCheckpoint({}))
        ctx, recorder, _, stage, epic, branch = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        command = recorder.lock.test_runner()
        for story in epic.stories:
            await stage.implement_story(story, "arch", command, ctx, recorder)
        github.ci = [[passing("test")]]
        await stage.finish_epic(epic, branch, ctx, recorder)

        subjects = git(workspace, "log", "--format=%s", "-8").splitlines()
        assert "foundry: story 1.1 implemented — Todo Model" in subjects
        assert "foundry: story 1.2 auto-fixed — Persistence" in subjects
        assert github.pulls[1]["merged"] is True
        assert Lock.for_workspace(workspace).has_section("epics.epic-1.merged")

    async def test_a_full_run_survives_a_story_that_fails_once(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        from foundry.orchestrator import RunStatus
        from tests.planned import checking_command
        from tests.test_implementation_loop import whole_run

        runner = SequenceRunner({"2.1": [bad("2.1"), good("2.1")]})
        failures = RetryingFailureHandler(ScriptedCheckpoint({}))
        _, result = await whole_run(
            workspace, tmp_path, runner, command=checking_command(tmp_path), failures=failures
        )

        assert result.status is RunStatus.COMPLETED
        data = LockFile.for_workspace(workspace).read()
        assert "merged" in data["epics"]["epic-1"] and "merged" in data["epics"]["epic-2"]
        assert data["failures"]["epic-2"]["story-1"]["retry_outcome"] == "passed"
        assert "foundry: story 2.1 auto-fixed — Add Command" in github.remote_log("todo-tracker", "main", "%s")

"""Story 3.5: passing story changes are committed with a forge-log message and a lock entry."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from foundry.context import CodeFailure, sha256_of
from foundry.epics import parse_epics, parse_stories
from foundry.gitops import GitError
from foundry.implementation import ImplementationStage, Recovery
from foundry.lock import LockFile
from foundry.testgate import GateResult
from tests.fake_github import FakeGitHub
from tests.planned import EPICS, make_scaffolded

STORIES = {story.id: story for story in parse_stories(EPICS)}
COMMIT_FORMAT = re.compile(r"^foundry: .+ — .+$")


def git_bytes(workspace: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, check=True).stdout


def git(workspace: Path, *args: str) -> str:
    return git_bytes(workspace, *args).decode("utf-8").strip()


def green(tests: int | None = 5, retries: int = 0) -> Recovery:
    return Recovery(GateResult(True, 0, f"{tests} passed", 1.5, tests), retries)


async def on_epic_branch(workspace: Path):
    ctx, recorder, gitops = await make_scaffolded(workspace)
    stage = ImplementationStage(gitops)
    await stage.start_epic(parse_epics(EPICS)[0], ctx, recorder)
    return ctx, recorder, gitops, stage


def implement(workspace: Path, name: str = "src/todo.py", text: str = "class Todo: ...\n") -> None:
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class TestCommit:
    async def test_message_follows_the_forge_log_format(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        implement(workspace)
        await stage.commit_story(STORIES["1.1"], green(), 2.0, recorder)

        subject = git(workspace, "log", "-1", "--format=%s")
        assert subject == "foundry: story 1.1 implemented — Todo Model"
        assert COMMIT_FORMAT.match(subject)

    async def test_all_changes_land_in_the_commit_with_the_lock_entry(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        implement(workspace, "src/todo.py")  # a new file
        implement(workspace, "tests/test_todo.py", "def test_x(): ...\n")
        (workspace / "README.md").write_text("# Changed by the agent\n", encoding="utf-8")  # modified
        await stage.commit_story(STORIES["1.1"], green(), 2.0, recorder)

        changed = set(git(workspace, "show", "--name-only", "--format=", "HEAD").splitlines())
        assert changed == {"src/todo.py", "tests/test_todo.py", "README.md", "foundry.lock"}
        assert git(workspace, "status", "--porcelain") == ""

    async def test_exactly_one_commit_per_story(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        before = int(git(workspace, "rev-list", "--count", "HEAD"))
        implement(workspace)
        await stage.commit_story(STORIES["1.1"], green(), 2.0, recorder)
        assert int(git(workspace, "rev-list", "--count", "HEAD")) == before + 1

    async def test_the_commit_is_on_the_epic_branch_and_main_is_untouched(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        main = git(workspace, "rev-parse", "main")
        implement(workspace)
        await stage.commit_story(STORIES["1.1"], green(), 2.0, recorder)

        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "epic-1-core-storage"
        assert git(workspace, "rev-parse", "main") == main

    async def test_the_prompt_file_and_foundry_state_are_never_committed(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        implement(workspace)
        (workspace / ".foundry" / "current-story.md").write_text("prompt\n", encoding="utf-8")
        await stage.commit_story(STORIES["1.1"], green(), 2.0, recorder)

        assert not [f for f in git(workspace, "ls-files").splitlines() if f.startswith(".foundry")]


class TestLockEntry:
    async def entry(self, workspace: Path, story_id: str = "1.1", recovery: Recovery | None = None):
        _, recorder, _, stage = await on_epic_branch(workspace)
        implement(workspace)
        story = STORIES[story_id]
        await stage.commit_story(story, recovery or green(7, retries=1), 3.25, recorder)
        return LockFile.for_workspace(workspace).read()["implementation"][f"epic-{story.epic}"][
            f"story-{story.number}"
        ]

    async def test_section_is_addressed_by_epic_and_story(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        entry = await self.entry(workspace)
        assert entry["story_id"] == "1.1" and entry["title"] == "Todo Model"

    async def test_records_tests_passed_duration_and_retries(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        entry = await self.entry(workspace)
        assert entry["tests_passed"] == 7
        assert entry["tests_counted"] is True
        assert entry["duration_seconds"] == 3.25
        assert entry["retries"] == 1

    async def test_an_uncounted_run_is_marked_rather_than_recorded_as_a_real_zero(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        entry = await self.entry(workspace, recovery=green(tests=None))
        assert entry["tests_passed"] == 0 and entry["tests_counted"] is False

    async def test_artifact_hash_pins_the_committed_diff(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        entry = await self.entry(workspace)
        diff = git_bytes(
            workspace, "diff", "--binary", "HEAD~1", "HEAD", "--", ".", ":(exclude)foundry.lock"
        )
        assert entry["artifact_hash"] == sha256_of(diff)

    async def test_the_lock_holds_no_output_from_the_session_or_the_tests(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        entry = await self.entry(workspace)
        assert set(entry) == {
            "story_id", "title", "artifact_hash", "tests_passed", "tests_counted",
            "duration_seconds", "retries", "completed_at",
        }

    async def test_the_lock_entry_is_in_the_same_commit_as_the_code(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.entry(workspace)
        lock_at_head = git(workspace, "show", "HEAD:foundry.lock")
        assert "[implementation.epic-1.story-1]" in lock_at_head
        assert "[implementation.epic-1.story-1]" not in git(workspace, "show", "HEAD~1:foundry.lock")

    async def test_the_lock_stays_append_only(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        before = (workspace / "foundry.lock").read_text(encoding="utf-8")
        implement(workspace)
        await stage.commit_story(STORIES["1.1"], green(), 1.0, recorder)
        assert (workspace / "foundry.lock").read_text(encoding="utf-8").startswith(before)

    async def test_two_stories_get_two_sections_and_two_commits(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        implement(workspace, "a.py")
        await stage.commit_story(STORIES["1.1"], green(), 1.0, recorder)
        implement(workspace, "b.py")
        await stage.commit_story(STORIES["1.2"], green(), 1.0, recorder)

        stories = LockFile.for_workspace(workspace).read()["implementation"]["epic-1"]
        assert set(stories) == {"story-1", "story-2"}
        subjects = git(workspace, "log", "--format=%s", "-2").splitlines()
        assert subjects == [
            "foundry: story 1.2 implemented — Persistence",
            "foundry: story 1.1 implemented — Todo Model",
        ]


class TestFailures:
    async def test_a_story_that_changed_nothing_is_a_failure_not_a_commit(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, recorder, _, stage = await on_epic_branch(workspace)
        head = git(workspace, "rev-parse", "HEAD")
        with pytest.raises(CodeFailure, match="no changes"):
            await stage.commit_story(STORIES["1.1"], green(), 1.0, recorder)

        assert git(workspace, "rev-parse", "HEAD") == head
        assert not LockFile.for_workspace(workspace).has_section("implementation")

    async def test_a_failed_commit_is_loud_and_leaves_the_lock_entry_for_resume(
        self, workspace: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, recorder, gitops, stage = await on_epic_branch(workspace)
        implement(workspace)

        async def refuse(message: str, paths=None) -> None:
            raise GitError("commit refused by hook")

        monkeypatch.setattr(gitops, "commit", refuse)
        with pytest.raises(GitError, match="refused"):
            await stage.commit_story(STORIES["1.1"], green(), 1.0, recorder)

        # The lock entry was appended but not committed: visible to `foundry resume` (AD-6).
        assert "foundry.lock" in git(workspace, "status", "--porcelain")

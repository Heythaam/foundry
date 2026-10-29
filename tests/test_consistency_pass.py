"""Story 5.4: after a model change, one dedicated session normalises style between epics."""

from __future__ import annotations

import subprocess
from pathlib import Path

from foundry.implementation import build_consistency_prompt
from foundry.lock import LockFile
from foundry.orchestrator import RunStatus
from tests.fake_github import FakeGitHub
from tests.planned import (
    AlwaysAvailable,
    ModelGone,
    RecordingRunner,
    ScriptedModelPrompt,
    checking_command,
    resume_run,
)
from tests.test_implementation_loop import whole_run
from tests.test_resume import CancelsDuring

REPO = "todo-tracker"
NEW = "claude-sonnet-5-0"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


def names(runner: RecordingRunner) -> list[str]:
    return [name for name, _ in runner.models]


async def changed_model_mid_epic(workspace: Path, tmp_path: Path, runner: RecordingRunner, **kwargs):
    """Cancel after story 1.1, then resume on a new model (the old one is gone)."""
    await whole_run(workspace, tmp_path, CancelsDuring("1.1"), **kwargs)
    return await resume_run(
        workspace, runner, checker=ModelGone(), prompt=ScriptedModelPrompt(NEW)
    )


class TestWhenItRuns:
    async def test_it_is_skipped_when_the_model_never_changed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        runner = RecordingRunner()
        await resume_run(workspace, runner, checker=AlwaysAvailable())

        assert "consistency pass" not in names(runner)
        assert "consistency" not in lock(workspace)
        assert not any("consistency" in s for s in git(workspace, "log", "--all", "--format=%s").splitlines())

    async def test_it_runs_when_the_current_epic_completes_and_before_the_next_begins(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = RecordingRunner()
        runner.consistency_files = {"src/story_1_1.py": "ID = '1.1'  # normalised\n"}
        _, result, _ = await changed_model_mid_epic(workspace, tmp_path, runner)

        assert result.status is RunStatus.COMPLETED
        # story 1.2 finishes epic 1; the pass follows; only then does epic 2 (story 2.1) start
        assert names(runner) == ["story 1.2", "consistency pass", "story 2.1"]
        log = git(workspace, "log", "--all", "--topo-order", "--format=%s").splitlines()
        merged = next(i for i, s in enumerate(log) if s.startswith("foundry: epic 1 merged"))
        applied = next(i for i, s in enumerate(log) if s.startswith("foundry: consistency pass"))
        started_two = next(i for i, s in enumerate(log) if s.startswith("foundry: epic branch created — epic-2"))
        assert started_two < applied < merged  # log is newest first: merge, then pass, then epic 2

    async def test_it_runs_after_the_last_epic_if_that_is_where_the_change_landed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("2.1"))
        runner = RecordingRunner()
        runner.consistency_files = {"src/story_2_1.py": "ID = '2.1'  # normalised\n"}
        _, result, _ = await resume_run(
            workspace, runner, checker=ModelGone(), prompt=ScriptedModelPrompt(NEW)
        )

        assert result.status is RunStatus.COMPLETED
        assert names(runner) == ["consistency pass"]
        data = lock(workspace)
        assert "consistency" in data and "completed" in data
        sections = list(data)
        assert sections.index("consistency") < sections.index("completed")

    async def test_one_pass_covers_several_model_changes_and_runs_once(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        await resume_run(  # first change, then cancelled again before epic 2 begins
            workspace, CancelsDuring("1.2"), checker=ModelGone(), prompt=ScriptedModelPrompt("claude-sonnet-5-0")
        )
        runner = RecordingRunner()
        await resume_run(
            workspace, runner, checker=ModelGone(), prompt=ScriptedModelPrompt("claude-opus-5-0")
        )

        assert names(runner).count("consistency pass") == 1
        passes = lock(workspace)["consistency"]
        assert list(passes) == ["pass-1"]
        assert passes["pass-1"]["original_model"] == "claude-sonnet-4-5"
        assert passes["pass-1"]["new_model"] == "claude-opus-5-0"
        assert passes["pass-1"]["through_change"] == 2
        prompt = runner.consistency_prompts[0]
        assert "claude-sonnet-4-5" in prompt and "claude-opus-5-0" in prompt


class TestWhatItDoes:
    async def test_it_is_a_dedicated_session_on_the_new_model(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = RecordingRunner()
        await changed_model_mid_epic(workspace, tmp_path, runner)
        assert ("consistency pass", NEW) in runner.models

    def test_the_prompt_is_about_style_not_logic(self) -> None:
        prompt = build_consistency_prompt("claude-sonnet-4-5", NEW, "pytest -q", "# Arch\n\nPython.")
        assert "naming conventions" in prompt and "formatting" in prompt and "comment" in prompt
        assert "Do NOT change behaviour" in prompt
        assert "`pytest -q`" in prompt and "# Arch" in prompt
        assert "claude-sonnet-4-5" in prompt and NEW in prompt
        assert "git commit" in prompt  # Foundry commits, the session must not

    async def test_its_changes_are_committed_with_its_lock_entry_and_pushed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = RecordingRunner()
        runner.consistency_files = {
            "src/story_1_1.py": "ID = '1.1'  # normalised\n",
            "src/story_1_2.py": "ID = '1.2'  # normalised\n",
        }
        await changed_model_mid_epic(workspace, tmp_path, runner)

        subject = "foundry: consistency pass — model boundary at story 1/3"
        assert git(workspace, "log", "--all", "--format=%s").splitlines().count(subject) == 1
        files = git(workspace, "show", "--name-only", "--format=", f":/{subject}").splitlines()
        assert sorted(files) == ["foundry.lock", "src/story_1_1.py", "src/story_1_2.py"]
        entry = lock(workspace)["consistency"]["pass-1"]
        assert entry["original_model"] == "claude-sonnet-4-5" and entry["new_model"] == NEW
        assert entry["boundary_story"] == "1.1" and entry["files_changed"] == 2
        assert entry["outcome"] == "applied"
        assert subject in github.remote_log(REPO, "main")
        assert "normalised" in git(workspace, "show", "main:src/story_1_1.py")

    async def test_a_pass_that_finds_nothing_to_change_is_still_recorded(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = RecordingRunner()  # writes nothing
        await changed_model_mid_epic(workspace, tmp_path, runner)

        entry = lock(workspace)["consistency"]["pass-1"]
        assert entry["outcome"] == "no changes needed" and entry["files_changed"] == 0
        assert git(workspace, "status", "--porcelain") == ""

    async def test_it_does_not_run_twice(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = RecordingRunner()
        await changed_model_mid_epic(workspace, tmp_path, runner)
        assert names(runner).count("consistency pass") == 1

        from foundry.progress import progress_from_lock

        progress = progress_from_lock(lock(workspace))
        assert progress is not None and progress.pending_model_change is None  # the flag is cleared

    async def test_the_pass_never_breaks_the_build(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        command = checking_command(tmp_path)
        runner = RecordingRunner()
        runner.consistency_files = {"src/story_1_1.py": "ID = '1.1'  # BROKEN by the pass\n"}
        _, result, _ = await changed_model_mid_epic(workspace, tmp_path, runner, command=command)

        assert result.status is RunStatus.COMPLETED  # the run goes on
        entry = lock(workspace)["consistency"]["pass-1"]
        assert entry["outcome"] == "reverted: tests failed" and entry["files_changed"] == 0
        assert "BROKEN" not in git(workspace, "show", "main:src/story_1_1.py")
        assert "consistency pass reverted" in git(workspace, "stash", "list")
        assert git(workspace, "status", "--porcelain") == ""

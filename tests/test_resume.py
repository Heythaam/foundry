"""Story 5.1: `foundry resume` picks an interrupted run up where it stopped, without duplicates."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.cancel import request_cancel
from foundry.context import EnvFailure
from foundry.gitops import GitOps
from foundry.lock import LockFile
from foundry.orchestrator import RunStatus
from foundry.provenance import ProvenanceRecorder
from foundry.resume import context_from_lock, require_resumable
from tests.fake_github import FakeGitHub
from tests.planned import (
    Crash,
    ImplementingRunner,
    bad,
    checking_command,
    resume_run,
)
from tests.test_implementation_loop import whole_run

REPO = "todo-tracker"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


def subjects(workspace: Path) -> list[str]:
    return git(workspace, "log", "--first-parent", "--format=%s", "main").splitlines()


def all_subjects(workspace: Path) -> list[str]:
    return git(workspace, "log", "--all", "--format=%s").splitlines()


def duplicates(items: list[str]) -> list[str]:
    return sorted({item for item in items if items.count(item) > 1})


class CancelsDuring(ImplementingRunner):
    """Asks for a cancel while it is working on `story`."""

    def __init__(self, story: str) -> None:
        super().__init__()
        self.story = story

    async def run(self, request, ctx):
        result = await super().run(request, ctx)
        if request.name == f"story {self.story}":
            request_cancel(ctx.workspace_path)
        return result


class CrashesDuring(ImplementingRunner):
    """Dies in the middle of a story: `after_writing` decides whether its files hit the disk."""

    def __init__(self, story: str, after_writing: bool, files=None) -> None:
        super().__init__(files)
        self.story = story
        self.after_writing = after_writing

    async def run(self, request, ctx):
        if request.name != f"story {self.story}":
            return await super().run(request, ctx)
        if self.after_writing:
            await super().run(request, ctx)
        raise Crash


class TestWhatCanBeResumed:
    def test_no_run_means_an_informative_error(self, workspace: Path) -> None:
        with pytest.raises(EnvFailure, match="no Foundry run to resume"):
            context_from_lock(workspace)

    async def test_a_completed_run_has_nothing_to_resume(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, ImplementingRunner())
        with pytest.raises(EnvFailure, match="already completed"):
            context_from_lock(workspace)

    async def test_a_run_that_never_finished_planning_cannot_be_resumed(
        self, workspace: Path
    ) -> None:
        from tests.planned import make_planned

        _, recorder = await make_planned(workspace)  # approved artifacts but no [plan]
        with pytest.raises(EnvFailure, match="stopped during planning"):
            context_from_lock(workspace)

    def test_the_error_says_what_to_do_instead(self) -> None:
        with pytest.raises(EnvFailure) as raised:
            require_resumable(None)
        assert raised.value.fix


class TestResumeAfterCancel:
    async def test_the_remaining_stories_are_built_and_nothing_is_built_twice(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        _, first = await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        assert first.status is RunStatus.CANCELLED
        assert LockFile.for_workspace(workspace).status() == "cancelled"

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.2", "2.1"]  # 1.1 was done and is not repeated
        assert LockFile.for_workspace(workspace).status() == "completed"
        story_commits = [s for s in all_subjects(workspace) if s.startswith("foundry: story")]
        assert duplicates(story_commits) == [] and len(story_commits) == 3

    async def test_a_resumed_commit_and_lock_entry_record_where_it_picked_up(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        await resume_run(workspace, ImplementingRunner())

        resumed = [s for s in all_subjects(workspace) if s.startswith("foundry: resumed")]
        assert resumed == ["foundry: resumed — story 1.2 (1 of 3 done)"]
        entry = lock(workspace)["resumes"]["resume-1"]
        assert entry["from_status"] == "cancelled"
        assert entry["stories_completed"] == 1 and entry["stories_total"] == 3
        assert entry["cancellations_seen"] == 1
        assert entry["resume_point"] == "story 1.2 (1 of 3 done)"
        assert entry["model_changed"] is False

    async def test_the_resume_point_and_model_are_reported_first(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        said: list[str] = []
        await resume_run(workspace, ImplementingRunner(), said=said)
        assert said[0].startswith("Resuming run ")
        assert "story 1.2 (1 of 3 done)" in said[1]
        assert "claude-sonnet-4-5" in said[2]

    async def test_a_run_can_be_cancelled_and_resumed_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        _, second, _ = await resume_run(workspace, CancelsDuring("1.2"))
        assert second.status is RunStatus.CANCELLED
        assert LockFile.for_workspace(workspace).status() == "cancelled"
        assert lock(workspace)["cancelled"]["cancel-2"]["before"]  # a new section, none reused

        runner = ImplementingRunner()
        _, third, _ = await resume_run(workspace, runner)
        assert third.status is RunStatus.COMPLETED and runner.stories == ["2.1"]
        assert set(lock(workspace)["resumes"]) == {"resume-1", "resume-2"}


class TestResumeAfterAnEnvironmentFailure:
    async def test_fix_it_and_resume(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        broken = ImplementingRunner()
        broken.errors["1.2"] = EnvFailure("could not find 'claude' on PATH")
        with pytest.raises(EnvFailure):
            await whole_run(workspace, tmp_path, broken)
        assert LockFile.for_workspace(workspace).status() == "in_progress"

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.2", "2.1"]
        assert "halt-1" in lock(workspace)["halts"]  # the history of the stop is kept


class TestCrashRecovery:
    """AD-6: a crash costs at most one story, and never a duplicate."""

    async def test_clean_tree_the_story_starts_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        with pytest.raises(Crash):
            await whole_run(workspace, tmp_path, CrashesDuring("1.2", after_writing=False))
        assert git(workspace, "status", "--porcelain") == ""

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.2", "2.1"]  # 1.1 survived; 1.2 is the one re-run

    async def test_dirty_and_green_the_work_is_committed_and_the_loop_moves_on(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        with pytest.raises(Crash):
            await whole_run(workspace, tmp_path, CrashesDuring("1.2", after_writing=True))
        assert "src/story_1_2.py" in git(workspace, "status", "--porcelain")  # left on disk

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["2.1"]  # 1.2 was not run again
        assert "foundry: story 1.2 implemented — Persistence" in all_subjects(workspace)
        entry = lock(workspace)["implementation"]["epic-1"]["story-2"]
        assert entry["recovered_on_resume"] is True
        assert duplicates([s for s in all_subjects(workspace) if s.startswith("foundry: story")]) == []

    async def test_dirty_and_red_the_work_is_set_aside_and_the_story_runs_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        command = checking_command(tmp_path)
        crashing = CrashesDuring("1.2", after_writing=True, files={"1.2": bad("1.2")})
        with pytest.raises(Crash):
            await whole_run(workspace, tmp_path, crashing, command=command)

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.2", "2.1"]
        assert "foundry: story 1.2 discarded on resume" in git(workspace, "stash", "list")
        assert "BROKEN" not in (workspace / "src" / "story_1_2.py").read_text(encoding="utf-8")
        assert "recovered_on_resume" not in lock(workspace)["implementation"]["epic-1"]["story-2"]

    async def test_a_story_whose_lock_entry_was_written_but_not_committed_is_committed_once(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_commit = GitOps.commit

        async def dies_on_story_1_2(self, message, paths=None):
            if "story 1.2 implemented" in message:
                raise Crash
            return await real_commit(self, message, paths)

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "commit", dies_on_story_1_2)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        # the lock says 1.2 is done; git does not know
        assert "story-2" in lock(workspace)["implementation"]["epic-1"]
        assert "foundry: story 1.2 implemented — Persistence" not in all_subjects(workspace)

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["2.1"]  # not redone
        committed = [s for s in all_subjects(workspace) if "story 1.2" in s]
        assert committed == ["foundry: story 1.2 implemented — Persistence"]  # exactly once
        shown = git(workspace, "show", "--name-only", "--format=", ":/story 1.2 implemented").splitlines()
        assert "src/story_1_2.py" in shown and "foundry.lock" in shown

    async def test_an_epic_record_written_but_not_committed_is_committed_once(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_commit = GitOps.commit

        async def dies_on_epic_record(self, message, paths=None):
            if "epic branch created" in message:
                raise Crash
            return await real_commit(self, message, paths)

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "commit", dies_on_epic_record)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.1", "1.2", "2.1"]
        records = [s for s in all_subjects(workspace) if s.startswith("foundry: epic branch created")]
        assert sorted(records) == [
            "foundry: epic branch created — epic-1-core-storage",
            "foundry: epic branch created — epic-2-command-line",
        ]

    async def test_a_branch_created_but_never_recorded_is_reused(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def dies(self, *args, **kwargs):
            raise Crash

        with monkeypatch.context() as patched:
            patched.setattr(ProvenanceRecorder, "record_epic_branch", dies)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        assert git(workspace, "branch", "--show-current") == "epic-1-core-storage"

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)
        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.1", "1.2", "2.1"]

    async def test_a_merge_that_happened_but_was_not_recorded_is_not_merged_twice(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def dies(self, *args, **kwargs):
            raise Crash

        with monkeypatch.context() as patched:
            patched.setattr(ProvenanceRecorder, "record_epic_merged", dies)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        assert github.pulls[1]["merged"] is True
        assert "merged" not in lock(workspace)["epics"]["epic-1"]

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["2.1"]
        assert sorted(pull["head"] for pull in github.pulls.values()) == [
            "epic-1-core-storage",
            "epic-2-command-line",
        ]  # no second PR for epic 1
        merges = [r for r in github.requests if r[0] == "PUT" and r[1].endswith("/pulls/1/merge")]
        assert len(merges) == 1
        assert lock(workspace)["epics"]["epic-1"]["merged"]["pull_request"] == 1

    async def test_a_merge_record_that_was_not_pushed_reaches_the_remote(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_push = GitOps.push

        async def dies_publishing_the_merge_record(self, branch, **kwargs):
            if branch == "main" and "merged" in LockFile.for_workspace(self.workspace).read().get(
                "epics", {}
            ).get("epic-1", {}):
                raise Crash
            return await real_push(self, branch, **kwargs)

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "push", dies_publishing_the_merge_record)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        assert not any(s.startswith("foundry: epic 1 merged") for s in github.remote_log(REPO, "main"))

        await resume_run(workspace, ImplementingRunner())
        assert any(s.startswith("foundry: epic 1 merged") for s in github.remote_log(REPO, "main"))

    async def test_a_repository_created_but_never_scaffolded_is_finished(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def dies(self, *args, **kwargs):
            raise Crash

        with monkeypatch.context() as patched:
            patched.setattr(ProvenanceRecorder, "record_scaffold", dies)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        assert "repo" in lock(workspace) and "scaffold" not in lock(workspace)

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED
        assert list(github.repos) == [REPO]  # the repository was not created a second time
        assert len([s for s in all_subjects(workspace) if s.startswith("foundry: scaffold")]) == 1
        assert "docs/planning/architecture.md" in github.remote_files(REPO, "main")

    async def test_a_scaffold_that_was_never_pushed_is_pushed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def dies_first_time(self, branch, **kwargs):
            raise Crash

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "push", dies_first_time)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, ImplementingRunner())
        assert "scaffold" in lock(workspace)
        assert github.remote_branches(REPO) == []

        _, result, _ = await resume_run(workspace, ImplementingRunner())
        assert result.status is RunStatus.COMPLETED
        assert "README.md" in github.remote_files(REPO, "main")


class TestTheStateBuffer:
    async def cancelled(self, workspace: Path, tmp_path: Path) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))

    async def test_a_missing_state_db_is_rebuilt_from_the_lock(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        import aiosqlite

        await self.cancelled(workspace, tmp_path)
        db = workspace / ".foundry" / "state.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)

        await resume_run(workspace, ImplementingRunner())

        async with aiosqlite.connect(db) as conn:
            rows = await (await conn.execute("SELECT section, status FROM ops")).fetchall()
        sections = {section for section, _ in rows}
        assert {"run", "plan", "repo", "scaffold", "implementation.epic-1.story-1"} <= sections
        assert {status for _, status in rows} == {"done"}  # nothing left in flight

    async def test_a_corrupt_state_db_is_replaced(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        (workspace / ".foundry" / "state.db").write_bytes(b"this is not a database")
        for suffix in ("-wal", "-shm"):
            (workspace / ".foundry" / f"state.db{suffix}").unlink(missing_ok=True)

        _, result, _ = await resume_run(workspace, ImplementingRunner())
        assert result.status is RunStatus.COMPLETED


class TestArtifacts:
    async def test_approved_documents_missing_from_disk_come_back_from_git(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        import shutil

        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        original = (workspace / ".foundry" / "artifacts" / "epics.md").read_bytes()
        shutil.rmtree(workspace / ".foundry" / "artifacts")

        runner = ImplementingRunner()
        _, result, _ = await resume_run(workspace, runner)

        assert result.status is RunStatus.COMPLETED and runner.stories == ["1.2", "2.1"]
        assert (workspace / ".foundry" / "artifacts" / "epics.md").read_bytes() == original

    async def test_documents_that_are_nowhere_are_reported_with_what_to_do(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        import shutil

        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        shutil.rmtree(workspace / ".foundry" / "artifacts")
        git(workspace, "rm", "-rq", "docs/planning")
        git(workspace, "commit", "-qm", "oops")

        with pytest.raises(EnvFailure) as raised:
            await resume_run(workspace, ImplementingRunner())
        assert "approved" in raised.value.message and raised.value.fix


class TestReviewedEpics:
    async def test_an_epic_reviewed_before_the_crash_is_not_reviewed_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from foundry.implementation import ImplementationStage
        from tests.helpers import ScriptedCheckpoint
        from tests.planned import count_command, make_scaffolded, quiet

        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        ctx.review_epics = True
        first = ScriptedCheckpoint()
        stage = ImplementationStage(
            quiet(gitops), ImplementingRunner(), checkpoint=first,
            on_test_output=lambda t: None, say=lambda t: None,
        )

        async def dies(*args, **kwargs):
            raise Crash

        monkeypatch.setattr(stage, "start_epic", dies)
        with pytest.raises(Crash):
            await stage.run(ctx, recorder)
        assert "epic-1-review" in first.phases[0]

        second = ScriptedCheckpoint()
        runner = ImplementingRunner()
        again = ImplementationStage(
            quiet(GitOps.from_env(workspace, require_token=True)), runner, checkpoint=second,
            on_test_output=lambda t: None, say=lambda t: None,
        )
        again.git.repo_full_name = gitops.repo_full_name
        await again.run(ctx, recorder)

        assert second.phases == ["epic-2-review"]  # epic 1 was not put in front of the human again
        assert runner.stories == ["1.1", "1.2", "2.1"]

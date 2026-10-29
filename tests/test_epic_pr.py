"""Story 3.6: a PR per epic, merged when CI is green."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.context import EnvFailure, FailureCategory, InfraFailure
from foundry.epics import parse_epics
from foundry.gitops import CIResult, GitHubError, PullRequestInfo
from foundry.implementation import CIFailure, ImplementationStage
from foundry.lock import LockFile
from tests.fake_github import FakeGitHub, failing, passing, running
from tests.planned import (
    EPICS,
    ImplementingRunner,
    count_command,
    make_scaffolded,
    quiet,
)

REPO = "todo-tracker"
EPIC_1, EPIC_2 = parse_epics(EPICS)


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


async def finished_epic(workspace: Path, tmp_path: Path, github: FakeGitHub, **stage_kwargs):
    """A scaffolded repo whose epic 1 has both stories committed on its branch, PR not yet open."""
    ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
    quiet(gitops)
    stage = ImplementationStage(
        gitops, ImplementingRunner(), on_test_output=lambda t: None, say=lambda t: None, **stage_kwargs
    )
    branch = await stage.start_epic(EPIC_1, ctx, recorder)
    command = recorder.lock.test_runner()
    for story in EPIC_1.stories:
        await stage.implement_story(story, "arch", command, ctx, recorder)
    return ctx, recorder, gitops, stage, branch


class TestPullRequest:
    async def test_opens_a_pr_from_the_epic_branch_to_main(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        pr = await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        record = github.pulls[pr.number]
        assert (record["head"], record["base"]) == ("epic-1-core-storage", "main")
        assert record["title"] == "Epic 1: Core Storage"
        assert pr.url.endswith(f"/pull/{pr.number}")

    async def test_every_story_commit_is_on_the_remote_branch_before_the_pr_opens(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        local = git(workspace, "rev-parse", branch)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert github.pulls[1]["sha"] == local
        subjects = github.remote_log(REPO, branch)
        assert "foundry: story 1.1 implemented — Todo Model" in subjects
        assert "foundry: story 1.2 implemented — Persistence" in subjects

    async def test_the_pr_body_lists_the_stories(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        from foundry.implementation import _pr_body

        body = _pr_body(EPIC_1)
        assert "Story 1.1: Todo Model" in body and "Story 1.2: Persistence" in body
        assert "Story 2.1" not in body


class TestCiGate:
    async def test_waits_for_ci_before_merging(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[running()], [running()], [running("lint"), passing()], [passing(), passing("lint")]]
        calls_when_merged: list[int] = []
        original = github._merge

        def spy(number: int, title: str):
            calls_when_merged.append(github.ci_calls)
            return original(number, title)

        github._merge = spy  # type: ignore[method-assign]
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert calls_when_merged == [4]  # four CI snapshots: the first three were not green
        assert github.pulls[1]["merged"] is True

    async def test_merges_automatically_when_ci_is_green(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[passing(), {"name": "docs", "status": "completed", "conclusion": "skipped"}]]
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert github.pulls[1]["merged"] is True
        files = github.remote_files(REPO, "main")
        assert "src/story_1_1.py" in files and "src/story_1_2.py" in files

    async def test_red_ci_stops_with_the_failing_checks_and_the_pr_link_and_does_not_merge(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[passing("lint"), failing("test")]]
        main_before = github.remote_log(REPO, "main")

        with pytest.raises(CIFailure) as info:
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert "test: failure" in info.value.message and "lint" not in info.value.message.split("failed")[1].split(";")[0]
        assert "/pull/1" in info.value.message
        assert info.value.category is FailureCategory.CODE
        assert github.pulls[1]["merged"] is False
        assert github.remote_log(REPO, "main") == main_before
        assert not LockFile.for_workspace(workspace).has_section("epics.epic-1.merged")

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
    async def test_every_non_success_conclusion_is_red(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, conclusion: str
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[failing("test", conclusion)]]
        with pytest.raises(CIFailure):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)

    async def test_the_failure_handler_gets_the_epic_pr_and_ci_result(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        seen: list[tuple[int, int, tuple[str, ...]]] = []

        class Recovers:
            async def on_test_failure(self, story, result, ctx, attempt):
                raise AssertionError("not used")

            async def on_ci_failure(self, epic, ci: CIResult, pr: PullRequestInfo, ctx, recheck) -> CIResult:
                seen.append((epic.number, pr.number, ci.failures))
                return CIResult(True, ("test: success",))

        ctx, recorder, _, stage, branch = await finished_epic(
            workspace, tmp_path, github, failures=Recovers()
        )
        github.ci = [[failing("test")]]
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert seen[0][:2] == (1, 1) and "test: failure" in seen[0][2][0]
        assert github.pulls[1]["merged"] is True

    async def test_a_handler_that_returns_red_does_not_unlock_the_merge(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        class Lies:
            async def on_test_failure(self, story, result, ctx, attempt):
                raise AssertionError("not used")

            async def on_ci_failure(self, epic, ci, pr, ctx, recheck):
                return ci

        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github, failures=Lies())
        github.ci = [[failing("test")]]
        with pytest.raises(CIFailure, match="after recovery"):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert github.pulls[1]["merged"] is False

    async def test_ci_that_never_appears_is_an_environment_failure(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[]]
        gitops.ci_no_checks_grace = 0
        with pytest.raises(EnvFailure, match="no CI checks"):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert github.pulls[1]["merged"] is False

    async def test_ci_that_never_finishes_is_an_infrastructure_failure(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.ci = [[running()]]
        gitops.ci_timeout = 0
        with pytest.raises(InfraFailure, match="did not finish"):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert github.pulls[1]["merged"] is False


class TestAfterMerge:
    async def test_records_the_merge_with_the_epic_merged_commit_on_main(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: epic 1 merged — PR #1"
        assert github.remote_log(REPO, "main")[0] == "foundry: epic 1 merged — PR #1"

    async def test_the_merge_lands_in_the_lock_next_to_the_branch_entry(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        epic = LockFile.for_workspace(workspace).read()["epics"]["epic-1"]
        assert epic["branch"] == branch
        merged = epic["merged"]
        assert merged["pull_request"] == 1 and merged["pull_request_url"].endswith("/pull/1")
        assert merged["head_sha"] == github.pulls[1]["sha"]
        assert len(merged["merge_sha"]) == 40
        assert merged["ci_checks"] == ["test: success"]

    async def test_local_main_matches_the_remote_and_the_tree_is_clean(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)

        assert git(workspace, "rev-parse", "main") == git(github.bare(REPO), "rev-parse", "main")
        assert git(workspace, "status", "--porcelain") == ""
        assert (workspace / "src" / "story_1_1.py").exists()  # the merged work is on local main

    async def test_main_history_has_the_merge_then_the_record(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        first_parent = github.remote_log(REPO, "main", "%s")
        assert first_parent[0] == "foundry: epic 1 merged — PR #1"
        assert first_parent[1].startswith("Merge epic 1: Core Storage")

    async def test_the_next_epic_can_start_from_the_updated_main(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        second = await stage.start_epic(EPIC_2, ctx, recorder)

        assert second == "epic-2-command-line"
        assert (workspace / "src" / "story_1_2.py").exists()  # epic 2 builds on epic 1's merge


class TestGitHubFailures:
    async def test_a_refused_merge_is_a_classified_error_and_records_nothing(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.merge_refused = "Pull Request is not mergeable"
        with pytest.raises(GitHubError, match="not mergeable"):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert not LockFile.for_workspace(workspace).has_section("epics.epic-1.merged")

    async def test_a_server_error_opening_the_pr_is_an_infrastructure_failure(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, branch = await finished_epic(workspace, tmp_path, github)
        github.fail("/pulls", 503, "unavailable")
        with pytest.raises(InfraFailure):
            await stage.finish_epic(EPIC_1, branch, ctx, recorder)
        assert github.pulls == {}

"""Story 3.2: a feature branch per epic."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.context import EnvFailure
from foundry.epics import Epic, parse_epics
from foundry.implementation import ImplementationStage, epic_branch_name
from foundry.lock import LockFile
from tests.fake_github import FakeGitHub
from tests.planned import EPICS, make_scaffolded

REPO = "todo-tracker"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


class TestBranchName:
    def test_is_epic_number_and_slugified_title(self) -> None:
        assert epic_branch_name(Epic(2, "Planning Pipeline", ())) == "epic-2-planning-pipeline"
        assert epic_branch_name(Epic(10, "CI/CD & Deploy!", ())) == "epic-10-ci-cd-deploy"

    def test_long_titles_are_shortened_without_a_trailing_hyphen(self) -> None:
        name = epic_branch_name(Epic(1, "An extremely long epic title " * 5, ()))
        assert name.startswith("epic-1-an-extremely-long-epic-title")
        assert len(name) <= len("epic-1-") + 40 and not name.endswith("-")

    def test_an_untitled_epic_still_gets_a_valid_name(self) -> None:
        assert epic_branch_name(Epic(3, "???", ())) == "epic-3-foundry-project"


class TestStartEpic:
    async def start(self, workspace: Path, github: FakeGitHub, number: int = 1):
        ctx, recorder, gitops = await make_scaffolded(workspace)
        epic = parse_epics(EPICS)[number - 1]
        stage = ImplementationStage(gitops)
        branch = await stage.start_epic(epic, ctx, recorder)
        return ctx, recorder, gitops, stage, epic, branch

    async def test_creates_epic_n_slug_from_main_and_checks_it_out(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, _, _, _, _, branch = await self.start(workspace, github)
        main_before = git(workspace, "rev-parse", "main")

        assert branch == "epic-1-core-storage"
        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == branch
        assert git(workspace, "merge-base", "main", branch) == main_before

    async def test_the_branch_is_pushed_to_the_remote_before_any_story(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, _, _, _, _, branch = await self.start(workspace, github)

        assert sorted(github.remote_branches(REPO)) == ["epic-1-core-storage", "main"]
        assert git(workspace, "rev-parse", "--abbrev-ref", "@{upstream}") == f"origin/{branch}"
        assert git(workspace, "rev-parse", branch) == git(
            github.bare(REPO), "rev-parse", f"refs/heads/{branch}"
        )

    async def test_main_is_untouched_on_the_remote(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace)
        remote_main = github.remote_log(REPO, "main")
        await ImplementationStage(gitops).start_epic(parse_epics(EPICS)[0], ctx, recorder)
        assert github.remote_log(REPO, "main") == remote_main

    async def test_branch_creation_is_recorded_in_the_lock_and_in_git(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        _, _, _, _, epic, branch = await self.start(workspace, github)

        section = LockFile.for_workspace(workspace).read()["epics"]["epic-1"]
        assert section["branch"] == branch and section["base"] == "main"
        assert section["title"] == "Core Storage" and section["story_ids"] == ["1.1", "1.2"]
        assert git(workspace, "log", "-1", "--format=%s") == (
            "foundry: epic branch created — epic-1-core-storage"
        )
        assert github.remote_log(REPO, branch)[0] == "foundry: epic branch created — epic-1-core-storage"

    async def test_the_working_tree_is_clean_afterwards(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.start(workspace, github)
        assert git(workspace, "status", "--porcelain") == ""

    async def test_each_epic_branches_from_main_not_from_the_previous_epic(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops, stage, _, first = await self.start(workspace, github)
        await gitops.checkout("main")
        second = await stage.start_epic(parse_epics(EPICS)[1], ctx, recorder)

        assert second == "epic-2-command-line"
        assert git(workspace, "merge-base", "main", second) == git(workspace, "rev-parse", "main")
        assert first not in git(workspace, "branch", "--merged", second)  # not stacked on epic 1

    async def test_refuses_to_start_from_a_branch_other_than_main(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, _, stage, _, _ = await self.start(workspace, github)
        with pytest.raises(EnvFailure, match="must start from main"):
            await stage.start_epic(parse_epics(EPICS)[1], ctx, recorder)  # still on epic 1

    async def test_refuses_to_start_over_uncommitted_changes(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace)
        (workspace / "stray.txt").write_text("left behind\n", encoding="utf-8")
        with pytest.raises(EnvFailure, match="stray.txt"):
            await ImplementationStage(gitops).start_epic(parse_epics(EPICS)[0], ctx, recorder)
        assert github.remote_branches(REPO) == ["main"]

    async def test_a_failed_push_is_a_classified_error_and_not_silent(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace)
        git(workspace, "remote", "set-url", "origin", str(workspace / "gone.git"))
        with pytest.raises(EnvFailure, match="push"):
            await ImplementationStage(gitops).start_epic(parse_epics(EPICS)[0], ctx, recorder)

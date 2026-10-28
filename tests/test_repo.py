"""Story 3.1: repository creation and scaffold."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from typer.testing import CliRunner

from foundry import cli as cli_module
from foundry.context import EnvFailure, FailureCategory, InfraFailure
from foundry.gitops import GitHubError, GitOps, RepoExists, missing_token_error
from foundry.lock import LockFile
from foundry.repo import RepoStage
from foundry.scaffold import CI_WORKFLOW_PATH
from tests.fake_github import TOKEN, FakeGitHub
from tests.planned import ARCHITECTURE, EPICS, PRD, ScriptedNamePrompt, make_planned

REAL_BUILD_STAGES = cli_module._build_stages  # conftest replaces it for the other CLI tests
cli_runner = CliRunner()

SCAFFOLD_SUBJECT = "foundry: scaffold — README, LICENSE, .gitignore, ci.yml, foundry.lock seeded"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def stage(workspace: Path, github: FakeGitHub, prompt=None, **kwargs) -> RepoStage:
    gitops = GitOps.from_env(workspace, require_token=True)
    return RepoStage(gitops, prompt or ScriptedNamePrompt(), **kwargs)


class TestNameConfirmation:
    async def test_derives_the_name_from_the_project_and_asks_before_creating(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        prompt = ScriptedNamePrompt()
        await stage(workspace, github, prompt).run(ctx, recorder)

        assert [c["suggested"] for c in prompt.calls] == ["todo-tracker"]
        assert list(github.repos) == ["todo-tracker"]

    async def test_the_human_can_replace_the_suggested_name(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        await stage(workspace, github, ScriptedNamePrompt("My Cool Todos")).run(ctx, recorder)
        assert list(github.repos) == ["my-cool-todos"]

    async def test_nothing_reaches_github_before_the_name_is_confirmed(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        seen_requests: list[int] = []

        class Spy(ScriptedNamePrompt):
            async def confirm_name(self, suggested, *, private, problem=None):
                seen_requests.append(len(github.requests))
                return await super().confirm_name(suggested, private=private, problem=problem)

        await stage(workspace, github, Spy()).run(ctx, recorder)
        assert seen_requests == [0]

    async def test_a_declined_confirmation_creates_nothing(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)

        class Closed:
            async def confirm_name(self, suggested, *, private, problem=None):
                raise EnvFailure("standard input is closed")

        with pytest.raises(EnvFailure):
            await RepoStage(GitOps.from_env(workspace, require_token=True), Closed()).run(
                ctx, recorder
            )
        assert github.repos == {} and github.requests == []
        assert not LockFile.for_workspace(workspace).has_section("repo")

    async def test_a_taken_name_asks_again_with_the_reason(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        github.repos["todo-tracker"] = {"bare": workspace, "private": False}
        prompt = ScriptedNamePrompt(None, "todo-tracker-2")
        await stage(workspace, github, prompt).run(ctx, recorder)

        assert prompt.calls[0]["problem"] is None
        assert "already taken" in prompt.calls[1]["problem"]
        assert "todo-tracker-2" in github.repos

    async def test_gives_up_after_three_taken_names(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        for name in ("a", "b", "c"):
            github.repos[name] = {"bare": workspace, "private": False}
        with pytest.raises(RepoExists):
            await stage(workspace, github, ScriptedNamePrompt("a", "b", "c")).run(ctx, recorder)

    async def test_visibility_is_shown_to_the_prompt_and_sent_to_github(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        prompt = ScriptedNamePrompt()
        await stage(workspace, github, prompt, private=True).run(ctx, recorder)
        assert prompt.calls[0]["private"] is True
        assert github.repos["todo-tracker"]["private"] is True

    async def test_repos_are_public_by_default(self, workspace: Path, github: FakeGitHub) -> None:
        ctx, recorder = await make_planned(workspace)
        await stage(workspace, github).run(ctx, recorder)
        assert github.repos["todo-tracker"]["private"] is False


class TestScaffoldCommit:
    async def run_stage(self, workspace: Path, github: FakeGitHub):
        ctx, recorder = await make_planned(workspace)
        await stage(workspace, github).run(ctx, recorder)
        return ctx

    async def test_main_on_the_remote_has_exactly_the_scaffold(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        assert sorted(github.remote_files("todo-tracker")) == sorted(
            [
                ".github/workflows/ci.yml",
                ".gitignore",
                "LICENSE",
                "README.md",
                "docs/planning/architecture.md",
                "docs/planning/brainstorm.md",
                "docs/planning/epics.md",
                "docs/planning/prd.md",
                "foundry.lock",
            ]
        )
        assert github.remote_branches("todo-tracker") == ["main"]

    async def test_the_scaffold_commit_message(self, workspace: Path, github: FakeGitHub) -> None:
        await self.run_stage(workspace, github)
        assert github.remote_log("todo-tracker")[0] == SCAFFOLD_SUBJECT
        assert git(workspace, "log", "-1", "--format=%s") == SCAFFOLD_SUBJECT

    async def test_the_scaffold_is_a_single_commit_with_its_lock_entry(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        changed = git(workspace, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert "foundry.lock" in changed and "README.md" in changed and "LICENSE" in changed
        lock = tomllib.loads(github.remote_show("todo-tracker", "main", "foundry.lock"))
        assert "scaffold" in lock  # the pushed lock already says it was scaffolded

    async def test_repo_creation_has_its_own_commit_and_lock_entry(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        subjects = github.remote_log("todo-tracker")
        assert "foundry: repo created — octocat/todo-tracker" in subjects
        repo = LockFile.for_workspace(workspace).read()["repo"]
        assert repo["full_name"] == "octocat/todo-tracker"
        assert repo["url"] == "https://github.com/octocat/todo-tracker"
        assert repo["private"] is False

    async def test_license_is_mit_and_gitignore_ignores_foundry_state(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        assert github.remote_show("todo-tracker", "main", "LICENSE").startswith("MIT License")
        ignore = github.remote_show("todo-tracker", "main", ".gitignore").splitlines()
        assert ".foundry/" in ignore and "__pycache__/" in ignore

    async def test_ci_runs_only_the_lock_test_command_on_prs_to_main(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace, test_command="pip install -e . && pytest -q")
        await stage(workspace, github).run(ctx, recorder)
        ci = github.remote_show("todo-tracker", "main", CI_WORKFLOW_PATH)
        assert "branches: [main]" in ci and "pull_request:" in ci
        assert len(re.findall(r"^\s+run:", ci, re.MULTILINE)) == 1
        assert ci.endswith("          pip install -e . && pytest -q")
        assert LockFile.for_workspace(workspace).test_runner() == "pip install -e . && pytest -q"

    async def test_planning_documents_are_committed_exactly_as_approved(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        assert github.remote_show("todo-tracker", "main", "docs/planning/prd.md") == PRD.strip()
        assert (
            github.remote_show("todo-tracker", "main", "docs/planning/architecture.md")
            == ARCHITECTURE.strip()
        )
        assert (
            github.remote_show("todo-tracker", "main", "docs/planning/epics.md") == EPICS.strip()
        )

    async def test_the_lock_records_the_scaffold_with_per_file_hashes(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        scaffold = LockFile.for_workspace(workspace).read()["scaffold"]
        assert scaffold["branch"] == "main"
        assert set(scaffold["file_hashes"]) == {
            "README.md", "LICENSE", ".gitignore", CI_WORKFLOW_PATH,
            "docs/planning/brainstorm.md", "docs/planning/prd.md",
            "docs/planning/architecture.md", "docs/planning/epics.md",
        }
        assert all(h.startswith("sha256:") for h in scaffold["file_hashes"].values())

    async def test_local_branch_is_main_tracking_origin(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.run_stage(workspace, github)
        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert git(workspace, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/main"
        assert git(workspace, "status", "--porcelain") == ""

    async def test_a_workspace_on_another_branch_name_is_renamed_to_main(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        git(workspace, "branch", "-m", "master")
        await stage(workspace, github).run(ctx, recorder)
        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert github.remote_branches("todo-tracker") == ["main"]

    async def test_running_it_again_after_scaffolding_does_nothing(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        runner = stage(workspace, github)
        await runner.run(ctx, recorder)
        requests = len(github.requests)
        await runner.run(ctx, recorder)
        assert len(github.requests) == requests and list(github.repos) == ["todo-tracker"]


class TestPreconditions:
    async def test_needs_the_test_runner_recorded_in_the_lock(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        text = LockFile.for_workspace(workspace).path.read_text(encoding="utf-8")
        text = text.replace("[run.test_runner]", "[run.other]")
        LockFile.for_workspace(workspace).path.write_text(text, encoding="utf-8")
        with pytest.raises(EnvFailure, match="test_runner"):
            await stage(workspace, github).run(ctx, recorder)
        assert github.requests == []

    async def test_needs_the_approved_artifacts_on_disk(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        (workspace / ".foundry" / "artifacts" / "epics.md").unlink()
        with pytest.raises(EnvFailure, match="epics"):
            await stage(workspace, github).run(ctx, recorder)
        assert github.requests == []

    async def test_refuses_to_overwrite_files_already_in_the_directory(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        (workspace / "README.md").write_text("mine\n", encoding="utf-8")
        with pytest.raises(EnvFailure, match="README.md"):
            await stage(workspace, github).run(ctx, recorder)
        assert (workspace / "README.md").read_text(encoding="utf-8") == "mine\n"
        assert github.requests == []

    async def test_refuses_a_directory_already_attached_to_a_remote(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        git(workspace, "remote", "add", "origin", "https://example.invalid/x.git")
        with pytest.raises(EnvFailure, match="remote"):
            await stage(workspace, github).run(ctx, recorder)
        assert github.requests == []


class TestCredentials:
    """NFR-6: the token comes from GITHUB_TOKEN only and never lands in any file."""

    def test_construction_fails_fast_without_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(EnvFailure, match="GITHUB_TOKEN"):
            GitOps.from_env(tmp_path, require_token=True)
        assert GitOps.from_env(tmp_path).token is None  # local-only use needs no token

    async def test_remote_operations_without_a_token_are_env_failures(self, tmp_path: Path) -> None:
        with pytest.raises(EnvFailure) as info:
            await GitOps(tmp_path).create_repo("x")
        assert info.value.category is FailureCategory.ENVIRONMENT
        assert str(missing_token_error()) in str(info.value)

    async def test_the_token_authenticates_api_calls(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        await stage(workspace, github).run(ctx, recorder)
        assert github.requests and all(TOKEN in auth for _, _, auth in github.requests)

    async def test_the_token_is_in_no_file_in_the_workspace_or_history(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        await stage(workspace, github).run(ctx, recorder)
        for path in workspace.rglob("*"):
            if path.is_file():
                assert TOKEN.encode() not in path.read_bytes(), path
        assert TOKEN not in git(workspace, "log", "-p", "--all")
        assert TOKEN not in git(workspace, "config", "--list", "--local")

    def test_gitops_does_not_expose_the_token_in_its_repr(self, tmp_path: Path) -> None:
        assert TOKEN not in repr(GitOps(tmp_path, token=TOKEN))


class TestGitHubFailures:
    async def failing_create(
        self, workspace: Path, github: FakeGitHub, status: int, message: str = "nope"
    ):
        ctx, recorder = await make_planned(workspace)
        github.fail("/user/repos", status, message)
        with pytest.raises(Exception) as info:
            await stage(workspace, github).run(ctx, recorder)
        return info.value

    async def test_bad_credentials_are_an_environment_failure_naming_the_variable(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        error = await self.failing_create(workspace, github, 401)
        assert isinstance(error, GitHubError) and error.category is FailureCategory.ENVIRONMENT
        assert "GITHUB_TOKEN" in error.message

    async def test_missing_scope_is_an_environment_failure(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        error = await self.failing_create(workspace, github, 403)
        assert error.category is FailureCategory.ENVIRONMENT and "scope" in error.message

    async def test_rate_limits_and_server_errors_are_infrastructure_failures(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        for status, message in ((429, "slow down"), (403, "API rate limit exceeded"), (502, "bad")):
            ws = workspace / f"w{status}"
            ws.mkdir()
            ctx, recorder = await make_planned(ws)
            github.fail("/user/repos", status, message)
            with pytest.raises(InfraFailure):
                await stage(ws, github).run(ctx, recorder)

    async def test_an_unreachable_github_is_an_infrastructure_failure(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        gitops = GitOps(workspace, token=TOKEN, api_url="http://127.0.0.1:9")
        with pytest.raises(InfraFailure):
            await RepoStage(gitops, ScriptedNamePrompt()).run(ctx, recorder)

    async def test_a_failed_creation_records_nothing_and_leaves_no_scaffold_files(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        await self.failing_create(workspace, github, 500)
        lock = LockFile.for_workspace(workspace)
        assert not lock.has_section("repo") and not lock.has_section("scaffold")
        assert not (workspace / "README.md").exists()
        assert not (workspace / "LICENSE").exists()

    async def test_the_token_never_appears_in_an_error_message(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        error = await self.failing_create(workspace, github, 401, message=f"bad credentials {TOKEN}")
        assert TOKEN not in str(error)

    async def test_a_failed_push_is_a_classified_error_without_the_token(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder = await make_planned(workspace)
        runner = stage(workspace, github)
        original = runner.git.add_remote

        async def broken_remote(url: str) -> None:
            await original(str(workspace / "does-not-exist.git"))

        runner.git.add_remote = broken_remote  # type: ignore[method-assign]
        with pytest.raises(EnvFailure) as info:
            await runner.run(ctx, recorder)
        assert "push" in info.value.message and TOKEN not in info.value.message


class TestGitOpsIsTheOnlyOwner:
    """AD-8: no git subprocess, GitHub client or token read anywhere else."""

    FORBIDDEN = {
        "a git subprocess": re.compile(r"""create_subprocess_exec\(\s*["']git["']"""),
        "subprocess.run of git": re.compile(r"""subprocess\.(?:run|Popen|check_\w+)\(\s*\[\s*["']git["']"""),
        "PyGithub": re.compile(r"^\s*(?:from|import) github\b", re.MULTILINE),
        "the token variable": re.compile(r"""environ(?:\.get)?\W+["']GITHUB_TOKEN["']"""),
    }

    def test_nothing_outside_gitops_touches_git_or_github(self) -> None:
        root = Path(__file__).resolve().parent.parent / "foundry"
        offenders = [
            f"{path.name}: {what}"
            for path in root.glob("*.py")
            if path.name != "gitops.py"
            for what, pattern in self.FORBIDDEN.items()
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_gitops_itself_does_contain_them(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "foundry" / "gitops.py").read_text(
            encoding="utf-8"
        )
        assert "create_subprocess_exec" in text and "from github import" in text


class TestCli:
    def test_pro_without_a_token_stops_before_any_planning_work(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("foundry.cli._build_stages", REAL_BUILD_STAGES)
        result = cli_runner.invoke(cli_module.app, ["pro", "an idea"])
        assert result.exit_code == 1
        assert "GITHUB_TOKEN" in result.output
        assert list(workspace.iterdir()) == []  # no lock, no commits, no agent run

    def test_pro_builds_the_repo_stage_with_one_shared_gitops(
        self, workspace: Path, github: FakeGitHub
    ) -> None:
        stages, gitops = REAL_BUILD_STAGES(workspace, False)
        assert [s.name for s in stages] == ["repo", "implementation"]
        assert all(stage.git is gitops for stage in stages) and gitops.token == TOKEN

    def test_private_flag_reaches_the_repo_stage(self, workspace: Path, github: FakeGitHub) -> None:
        stages, _ = REAL_BUILD_STAGES(workspace, True)
        assert stages[0].private is True

    def test_help_documents_private(self) -> None:
        assert "--private" in cli_runner.invoke(cli_module.app, ["pro", "--help"]).output

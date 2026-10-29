"""Story 6.1: after every epic is merged, Foundry commits a production-ready Dockerfile (FR-30)."""

from __future__ import annotations

import asyncio
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli
from foundry.context import CodeFailure, EnvFailure, RunContext, sha256_of
from foundry.dockerfile import (
    BuildStep,
    DockerfileInfo,
    DockerfileStage,
    build_dockerfile_prompt,
    check_dockerfile,
    deployment_files,
    detect_build_step,
    inspect_dockerfile,
)
from foundry.gitops import GitOps
from foundry.implementation import ImplementationStage
from foundry.lock import LockFile
from foundry.orchestrator import RunStatus
from foundry.progress import progress_from_lock
from foundry.provenance import ProvenanceRecorder
from foundry.repo import RepoStage
from foundry.status import render_status
from tests.fake_github import FakeGitHub
from tests.planned import (
    MULTI_STAGE,
    NODE_PACKAGE,
    SINGLE_STAGE,
    Crash,
    FinalizingRunner,
    ScriptedNamePrompt,
    checking_command,
    quiet,
    resume_run,
)
from tests.test_implementation_loop import whole_run

REPO = "todo-tracker"
SUBJECT = "foundry: dockerfile generated — python:3.12-slim, 8000"
cli_runner = CliRunner()


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


def subjects(workspace: Path) -> list[str]:
    return git(workspace, "log", "--all", "--format=%s").splitlines()


def without(dockerfile: str, instruction: str) -> str:
    """The Dockerfile minus every line that starts with `instruction`."""
    return "".join(
        line for line in dockerfile.splitlines(keepends=True) if not line.startswith(instruction)
    )


def build_files(story: str = "1.1") -> dict[str, dict[str, str]]:
    """Story `story` also writes a package.json with a build script: the project has a build step."""
    return {story: {f"src/story_{story.replace('.', '_')}.py": f"ID = '{story}'\n", "package.json": NODE_PACKAGE}}


# ------------------------------------------------------------------ the build-step decision


class TestDetectingABuildStep:
    def test_a_project_that_runs_from_source_has_none(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'todo'\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        assert not detect_build_step(tmp_path).present

    def test_a_package_json_with_a_build_script_has_one(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(NODE_PACKAGE, encoding="utf-8")
        step = detect_build_step(tmp_path)
        assert step.present and "package.json" in step.evidence

    def test_a_package_json_without_one_does_not(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node ."}}', encoding="utf-8")
        assert not detect_build_step(tmp_path).present

    @pytest.mark.parametrize(
        "marker", ["Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "tsconfig.json", "App.csproj"]
    )
    def test_compiled_languages_have_one(self, tmp_path: Path, marker: str) -> None:
        (tmp_path / marker).write_text("x\n", encoding="utf-8")
        step = detect_build_step(tmp_path)
        assert step.present and marker in step.evidence

    def test_a_makefile_with_a_build_target_has_one(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n\nbuild:\n\tcc main.c\n", encoding="utf-8")
        assert detect_build_step(tmp_path).present
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        assert not detect_build_step(tmp_path).present

    def test_a_frontend_one_directory_down_counts(self, tmp_path: Path) -> None:
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "package.json").write_text(NODE_PACKAGE, encoding="utf-8")
        step = detect_build_step(tmp_path)
        assert step.present and "web/package.json" in step.evidence

    def test_vendored_and_generated_directories_are_ignored(self, tmp_path: Path) -> None:
        for directory in ("node_modules", ".venv", "dist"):
            (tmp_path / directory).mkdir()
            (tmp_path / directory / "package.json").write_text(NODE_PACKAGE, encoding="utf-8")
        assert not detect_build_step(tmp_path).present

    def test_the_architecture_can_say_so_when_the_repository_does_not(self, tmp_path: Path) -> None:
        step = detect_build_step(tmp_path, "Build with `npm run build`, serve from dist/.")
        assert step.present and "npm run build" in step.evidence
        assert not detect_build_step(tmp_path, "Python 3.12, stdlib only.").present


# ------------------------------------------------------------------- reading a Dockerfile


class TestInspectingADockerfile:
    def test_a_single_stage_file(self) -> None:
        info = inspect_dockerfile(SINGLE_STAGE)
        assert info.stages == 1 and not info.multi_stage
        assert info.base_image == "python:3.12-slim" and info.ports == (8000,)
        assert info.healthcheck is not None and "/health" in info.healthcheck

    def test_a_multi_stage_file_reads_its_final_stage(self) -> None:
        info = inspect_dockerfile(MULTI_STAGE)
        assert info.stages == 2 and info.multi_stage
        assert info.base_images == ("node:20-alpine", "node:20-alpine")
        assert info.ports == (3000,) and info.healthcheck is not None

    def test_expose_and_healthcheck_in_an_earlier_stage_do_not_count(self) -> None:
        text = (
            "FROM golang:1.22 AS build\nEXPOSE 8080\nHEALTHCHECK CMD true\nRUN go build\n"
            "FROM gcr.io/distroless/static:nonroot\nCOPY --from=build /app /app\n"
        )
        info = inspect_dockerfile(text)
        assert info.ports == () and info.healthcheck is None

    def test_healthcheck_none_is_no_healthcheck(self) -> None:
        assert inspect_dockerfile("FROM a:1\nEXPOSE 80\nHEALTHCHECK NONE\n").healthcheck is None

    def test_a_healthcheck_needs_a_command(self) -> None:
        assert inspect_dockerfile("FROM a:1\nHEALTHCHECK --interval=5s\n").healthcheck is None

    def test_instructions_are_case_insensitive_and_continuations_are_joined(self) -> None:
        text = (
            "# syntax=docker/dockerfile:1\nfrom --platform=linux/amd64 python:3.12 as app\n"
            "expose 8000/tcp\n"
            "healthcheck --interval=10s \\\n  --timeout=2s \\\n  cmd curl -f http://localhost:8000/health\n"
        )
        info = inspect_dockerfile(text)
        assert info.base_image == "python:3.12" and info.ports == (8000,)
        assert info.healthcheck is not None and "curl" in info.healthcheck

    def test_a_port_given_through_arg_or_env_is_resolved(self) -> None:
        assert inspect_dockerfile("ARG PORT=9000\nFROM a:1\nEXPOSE $PORT\n").ports == (9000,)
        assert inspect_dockerfile("FROM a:1\nENV PORT 7000\nEXPOSE ${PORT}\n").ports == (7000,)
        assert inspect_dockerfile("FROM a:1\nEXPOSE ${PORT:-5000}\n").ports == (5000,)
        assert inspect_dockerfile("FROM a:1\nEXPOSE $UNKNOWN\n").ports == ()

    def test_several_ports_and_a_range(self) -> None:
        assert inspect_dockerfile("FROM a:1\nEXPOSE 80 443/tcp 9000-9010\n").ports == (80, 443, 9000)

    def test_an_empty_file_has_no_stages(self) -> None:
        assert inspect_dockerfile("# nothing\n").stages == 0


class TestTheRules:
    single = DockerfileInfo(1, ("python:3.12",), (8000,), "CMD true")
    multi = DockerfileInfo(2, ("node:20", "node:20"), (3000,), "CMD true")
    compiled = BuildStep(True, "go.mod: Go compiles to a binary")
    plain = BuildStep(False, "nothing is compiled")

    def test_a_good_file_has_no_problems(self) -> None:
        assert check_dockerfile(self.single, self.plain) == []
        assert check_dockerfile(self.multi, self.compiled) == []

    def test_a_missing_from_is_the_only_problem_reported(self) -> None:
        assert check_dockerfile(DockerfileInfo(0, (), (), None), self.plain) == [
            "the Dockerfile has no FROM instruction"
        ]

    def test_each_requirement_is_reported_on_its_own(self) -> None:
        no_port = check_dockerfile(DockerfileInfo(1, ("a:1",), (), "CMD true"), self.plain)
        no_health = check_dockerfile(DockerfileInfo(1, ("a:1",), (80,), None), self.plain)
        assert len(no_port) == 1 and "EXPOSE" in no_port[0]
        assert len(no_health) == 1 and "HEALTHCHECK" in no_health[0]

    def test_a_build_step_requires_multiple_stages(self) -> None:
        problems = check_dockerfile(self.single, self.compiled)
        assert len(problems) == 1 and "multi-stage" in problems[0] and "go.mod" in problems[0]

    def test_no_build_step_requires_a_single_stage(self) -> None:
        problems = check_dockerfile(self.multi, self.plain)
        assert len(problems) == 1 and "single stage" in problems[0]

    def test_platform_files_are_recognised_wherever_they_are(self) -> None:
        paths = ["Dockerfile", "src/app.py", "fly.toml", "deploy/Render.yaml", "k8s/deployment.yaml",
                 "docker-compose.yml", "helm/chart/values.yaml", ".dockerignore", "docs/compose.md"]
        assert deployment_files(paths) == [
            "fly.toml", "deploy/Render.yaml", "k8s/deployment.yaml", "docker-compose.yml",
            "helm/chart/values.yaml",
        ]


class TestThePrompt:
    def test_a_build_step_asks_for_multiple_stages(self) -> None:
        prompt = build_dockerfile_prompt("# Arch\n\nGo.", "go test ./...", BuildStep(True, "go.mod"))
        assert "MULTI-STAGE" in prompt and "SINGLE-STAGE" not in prompt

    def test_no_build_step_asks_for_one_stage(self) -> None:
        prompt = build_dockerfile_prompt("# Arch\n\nPython.", "pytest -q", BuildStep(False, "none"))
        assert "SINGLE-STAGE" in prompt and "MULTI-STAGE" not in prompt

    def test_it_states_every_requirement_and_carries_the_architecture(self) -> None:
        prompt = build_dockerfile_prompt("# Arch\n\nPython.", "pytest -q", BuildStep(False, "none"))
        for expected in ("EXPOSE", "HEALTHCHECK", "/health", "non-root", "never `latest`", "`pytest -q`",
                         "git commit", "foundry.lock", "no docker-compose, fly.toml", "# Arch"):
            assert expected in prompt, expected
        assert "Problems with your previous attempt" not in prompt

    def test_a_correction_lists_what_was_wrong(self) -> None:
        prompt = build_dockerfile_prompt("# Arch", "pytest", BuildStep(False, "none"), ["no EXPOSE", "no HEALTHCHECK"])
        assert "Problems with your previous attempt" in prompt
        assert "- no EXPOSE" in prompt and "- no HEALTHCHECK" in prompt


# ----------------------------------------------------------------------- the whole run


class TestFinalization:
    async def test_the_dockerfile_is_committed_to_main_after_the_last_epic_merges(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner()
        ctx, result = await whole_run(workspace, tmp_path, runner, finalize=True)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.1", "1.2", "2.1"]  # one session per story, then the Dockerfile one
        assert len(runner.dockerfile_prompts) == 1
        assert git(workspace, "branch", "--show-current") == "main"
        assert (workspace / "Dockerfile").read_text(encoding="utf-8") == SINGLE_STAGE

        log = git(workspace, "log", "--topo-order", "--format=%s").splitlines()  # newest first
        merged = next(i for i, s in enumerate(log) if s.startswith("foundry: epic 2 merged"))
        generated = log.index(SUBJECT)
        completed = next(i for i, s in enumerate(log) if s.startswith("foundry: run completed"))
        assert completed < generated < merged
        assert log.count(SUBJECT) == 1

    async def test_the_commit_holds_the_dockerfile_and_its_lock_entry(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        files = git(workspace, "show", "--name-only", "--format=", f":/{SUBJECT}").splitlines()
        assert sorted(files) == ["Dockerfile", "foundry.lock"]

    async def test_it_is_pushed_so_the_remote_main_has_it(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        assert "Dockerfile" in github.remote_files(REPO, "main")
        assert SUBJECT in github.remote_log(REPO, "main")
        assert github.remote_show(REPO, "main", "Dockerfile").strip() == SINGLE_STAGE.strip()

    async def test_provenance_is_recorded_in_the_lock(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        entry = lock(workspace)["dockerfile"]

        assert entry["base_image"] == "python:3.12-slim" and entry["port"] == 8000
        assert entry["ports"] == [8000] and entry["stages"] == 1 and entry["multi_stage"] is False
        assert entry["build_step"] is False and entry["attempts"] == 1
        assert entry["healthcheck"].startswith("--interval=30s") and "/health" in entry["healthcheck"]
        assert entry["files"] == ["Dockerfile"]
        assert entry["model"] == "claude-sonnet-4-5" and entry["generated_at"].endswith("Z")
        assert entry["tests_passed"] == 3
        assert entry["artifact_hash"] == sha256_of(SINGLE_STAGE)  # the hash, never the content (NFR-7)
        assert SINGLE_STAGE not in (workspace / "foundry.lock").read_text(encoding="utf-8")

    async def test_the_run_is_marked_complete_after_the_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        _, result = await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        assert result.status is RunStatus.COMPLETED
        assert LockFile.for_workspace(workspace).status() == "completed"
        sections = list(lock(workspace))
        assert sections.index("dockerfile") < sections.index("completed")  # [completed] is last
        assert sections[-1] == "completed"
        assert git(workspace, "log", "-1", "--format=%s").startswith("foundry: run completed")
        assert git(workspace, "status", "--porcelain") == ""

    async def test_nothing_is_generated_without_the_stage(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner()
        await whole_run(workspace, tmp_path, runner)
        assert runner.dockerfile_prompts == [] and "dockerfile" not in lock(workspace)

    async def test_the_session_sees_the_architecture_and_the_test_command(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner()
        await whole_run(workspace, tmp_path, runner, finalize=True)
        prompt = runner.dockerfile_prompts[0]
        assert "## Architecture" in prompt and "count_tests.py" in prompt
        assert "SINGLE-STAGE" in prompt


class TestSingleAndMultiStage:
    async def test_a_project_with_a_build_step_gets_a_multi_stage_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": MULTI_STAGE}], files=build_files())
        await whole_run(workspace, tmp_path, runner, finalize=True)

        assert "MULTI-STAGE" in runner.dockerfile_prompts[0]
        entry = lock(workspace)["dockerfile"]
        assert entry["multi_stage"] is True and entry["stages"] == 2
        assert entry["build_step"] is True and "package.json" in entry["build_step_evidence"]
        assert entry["port"] == 3000 and entry["base_image"] == "node:20-alpine"
        assert "foundry: dockerfile generated — node:20-alpine, 3000" in subjects(workspace)

    async def test_a_single_stage_answer_to_a_build_step_is_sent_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": SINGLE_STAGE}, {"Dockerfile": MULTI_STAGE}], files=build_files())
        await whole_run(workspace, tmp_path, runner, finalize=True)

        assert len(runner.dockerfile_prompts) == 2
        assert "Problems with your previous attempt" in runner.dockerfile_prompts[1]
        assert "must be multi-stage" in runner.dockerfile_prompts[1]
        assert lock(workspace)["dockerfile"]["attempts"] == 2
        assert (workspace / "Dockerfile").read_text(encoding="utf-8") == MULTI_STAGE

    async def test_a_multi_stage_answer_to_a_project_without_one_is_sent_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": MULTI_STAGE}, {"Dockerfile": SINGLE_STAGE}])
        await whole_run(workspace, tmp_path, runner, finalize=True)
        assert "must be a single stage" in runner.dockerfile_prompts[1]
        assert lock(workspace)["dockerfile"]["multi_stage"] is False


class TestWhatTheDockerfileMustHave:
    async def test_no_healthcheck_is_sent_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": without(SINGLE_STAGE, "HEALTHCHECK")}, {"Dockerfile": SINGLE_STAGE}])
        await whole_run(workspace, tmp_path, runner, finalize=True)
        assert "no HEALTHCHECK" in runner.dockerfile_prompts[1]
        assert "HEALTHCHECK" in (workspace / "Dockerfile").read_text(encoding="utf-8")

    async def test_no_expose_is_sent_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": without(SINGLE_STAGE, "EXPOSE")}, {"Dockerfile": SINGLE_STAGE}])
        await whole_run(workspace, tmp_path, runner, finalize=True)
        assert "no EXPOSE" in runner.dockerfile_prompts[1]
        assert lock(workspace)["dockerfile"]["port"] == 8000

    async def test_no_dockerfile_at_all_is_sent_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{}, {"Dockerfile": SINGLE_STAGE}])
        await whole_run(workspace, tmp_path, runner, finalize=True)
        assert "there is no file named Dockerfile" in runner.dockerfile_prompts[1]

    async def test_platform_specific_files_are_sent_back_and_never_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner(
            [
                {"Dockerfile": SINGLE_STAGE, "fly.toml": "app = 'todo'\n", "docker-compose.yml": "services: {}\n"},
                {"fly.toml": None, "docker-compose.yml": None},
            ]
        )
        await whole_run(workspace, tmp_path, runner, finalize=True)

        assert "remove the platform-specific files docker-compose.yml, fly.toml" in runner.dockerfile_prompts[1]
        tracked = git(workspace, "ls-files").splitlines()
        assert "Dockerfile" in tracked and "fly.toml" not in tracked and "docker-compose.yml" not in tracked
        assert lock(workspace)["dockerfile"]["files"] == ["Dockerfile"]

    async def test_editing_foundry_lock_is_refused_and_undone(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner([{"Dockerfile": SINGLE_STAGE, "foundry.lock": "# tampered\n"}])
        with pytest.raises(CodeFailure, match="foundry.lock was edited"):
            await whole_run(workspace, tmp_path, runner, finalize=True)

        assert "foundry.lock was edited" in runner.dockerfile_prompts[1]
        data = lock(workspace)  # the stash put the real lock back
        assert "dockerfile" not in data and "completed" not in data and data["run"]["run_id"]

    async def test_the_session_may_add_a_health_endpoint_and_it_ships_with_the_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner(
            [{"Dockerfile": SINGLE_STAGE, "src/health.py": "def health():\n    return 'ok'\n"}]
        )
        await whole_run(workspace, tmp_path, runner, finalize=True)

        files = git(workspace, "show", "--name-only", "--format=", f":/{SUBJECT}").splitlines()
        assert sorted(files) == ["Dockerfile", "foundry.lock", "src/health.py"]
        assert lock(workspace)["dockerfile"]["files"] == ["Dockerfile", "src/health.py"]

    async def test_a_change_that_breaks_the_tests_is_sent_back_with_the_output(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = FinalizingRunner(
            [
                {"Dockerfile": SINGLE_STAGE, "src/health.py": "STATUS = 'BROKEN'\n"},
                {"src/health.py": "STATUS = 'ok'\n"},
            ]
        )
        await whole_run(workspace, tmp_path, runner, command=checking_command(tmp_path), finalize=True)

        assert "tests fail after your changes" in runner.dockerfile_prompts[1]
        assert "FAILED health.py" in runner.dockerfile_prompts[1]
        assert "BROKEN" not in git(workspace, "show", "main:src/health.py")
        assert lock(workspace)["dockerfile"]["attempts"] == 2


class TestWhenItCannotBeDone:
    async def test_three_bad_attempts_stop_the_run_with_the_problems_and_leave_main_clean(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        bad = {"Dockerfile": without(SINGLE_STAGE, "HEALTHCHECK")}
        runner = FinalizingRunner([bad])
        with pytest.raises(CodeFailure) as stopped:
            await whole_run(workspace, tmp_path, runner, finalize=True)

        assert len(runner.dockerfile_prompts) == 3
        assert "after 3 attempts" in stopped.value.message and "no HEALTHCHECK" in stopped.value.message
        assert git(workspace, "status", "--porcelain") == ""  # nothing half-done left on main
        assert "dockerfile attempt discarded" in git(workspace, "stash", "list")
        data = lock(workspace)
        assert "dockerfile" not in data and "completed" not in data
        assert LockFile.for_workspace(workspace).status() == "in_progress"
        assert not any(s.startswith("foundry: dockerfile") for s in subjects(workspace))

    async def test_resume_finishes_the_job_without_redoing_any_story(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        with pytest.raises(CodeFailure):
            await whole_run(workspace, tmp_path, FinalizingRunner([{}]), finalize=True)

        runner = FinalizingRunner()
        _, result, _ = await resume_run(workspace, runner, finalize=True)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == [] and len(runner.dockerfile_prompts) == 1
        assert lock(workspace)["dockerfile"]["base_image"] == "python:3.12-slim"
        assert LockFile.for_workspace(workspace).status() == "completed"
        assert "Dockerfile" in github.remote_files(REPO, "main")

    async def test_the_stage_refuses_to_run_before_every_epic_is_merged(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, first = await whole_run(workspace, tmp_path, FinalizingRunner(cancel_after="1.1"))
        assert first.status is RunStatus.CANCELLED  # story 1.1 is done; 1.2 and 2.1 are not
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        runner = FinalizingRunner()
        stage = DockerfileStage(gitops, runner, say=lambda t: None)
        with pytest.raises(EnvFailure, match=r"story 1\.2 is not done") as refused:
            await stage.run(ctx, ProvenanceRecorder(workspace, gitops))
        assert refused.value.fix and "foundry resume" in refused.value.fix
        assert runner.dockerfile_prompts == []

    async def test_it_must_be_on_main(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, _ = await whole_run(workspace, tmp_path, FinalizingRunner())  # merged, no Dockerfile yet
        git(workspace, "checkout", "-q", "-b", "elsewhere")
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        stage = DockerfileStage(gitops, FinalizingRunner(), say=lambda t: None)
        with pytest.raises(EnvFailure, match="working tree is on 'elsewhere'") as refused:
            await stage.run(ctx, ProvenanceRecorder(workspace, gitops))
        assert refused.value.fix and "main" in refused.value.fix


# ------------------------------------------------------------------- crash and cancel


class TestCrashesAndCancels:
    async def test_cancelled_before_finalization_resumes_into_it(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        _, first = await whole_run(
            workspace, tmp_path, FinalizingRunner(cancel_after="2.1"), finalize=True
        )
        assert first.status is RunStatus.CANCELLED
        assert "dockerfile" not in lock(workspace)
        assert lock(workspace)["cancelled"]["before"] == "stage finalization"

        runner = FinalizingRunner()
        _, result, _ = await resume_run(workspace, runner, finalize=True)
        assert result.status is RunStatus.COMPLETED
        assert runner.stories == [] and len(runner.dockerfile_prompts) == 1
        assert "dockerfile" in lock(workspace) and LockFile.for_workspace(workspace).status() == "completed"

    async def test_a_crash_in_the_session_leaves_files_that_resume_sets_aside(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        class DiesAfterWriting(FinalizingRunner):
            async def run(self, request, ctx):
                if request.name == "dockerfile":
                    (ctx.workspace_path / "Dockerfile").write_text("FROM half\n", encoding="utf-8")
                    raise Crash
                return await super().run(request, ctx)

        with pytest.raises(Crash):
            await whole_run(workspace, tmp_path, DiesAfterWriting(), finalize=True)
        assert (workspace / "Dockerfile").exists() and "dockerfile" not in lock(workspace)

        runner = FinalizingRunner()
        _, result, _ = await resume_run(workspace, runner, finalize=True)

        assert result.status is RunStatus.COMPLETED
        assert (workspace / "Dockerfile").read_text(encoding="utf-8") == SINGLE_STAGE
        assert "foundry: dockerfile attempt discarded on resume" in git(workspace, "stash", "list")

    async def test_an_entry_written_but_not_committed_is_committed_once_with_the_file(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_commit = GitOps.commit

        async def dies_on_the_dockerfile(self, message, paths=None):
            if "dockerfile generated" in message:
                raise Crash
            return await real_commit(self, message, paths)

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "commit", dies_on_the_dockerfile)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        assert "dockerfile" in lock(workspace) and SUBJECT not in subjects(workspace)

        runner = FinalizingRunner()
        _, result, _ = await resume_run(workspace, runner, finalize=True)

        assert result.status is RunStatus.COMPLETED
        assert runner.dockerfile_prompts == []  # not generated a second time
        assert subjects(workspace).count(SUBJECT) == 1
        files = git(workspace, "show", "--name-only", "--format=", f":/{SUBJECT}").splitlines()
        assert sorted(files) == ["Dockerfile", "foundry.lock"]
        assert "Dockerfile" in github.remote_files(REPO, "main")

    async def test_a_commit_that_never_reached_the_remote_is_pushed_on_resume(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_push = GitOps.push
        state = {"armed": False}

        async def dies_pushing_the_dockerfile(self, branch, **kwargs):
            if state["armed"] and branch == "main":
                raise Crash
            return await real_push(self, branch, **kwargs)

        class Arms(FinalizingRunner):
            async def run(self, request, ctx):
                if request.name == "dockerfile":
                    state["armed"] = True
                return await super().run(request, ctx)

        with monkeypatch.context() as patched:
            patched.setattr(GitOps, "push", dies_pushing_the_dockerfile)
            with pytest.raises(Crash):
                await whole_run(workspace, tmp_path, Arms(), finalize=True)
        assert SUBJECT in subjects(workspace) and "Dockerfile" not in github.remote_files(REPO, "main")

        runner = FinalizingRunner()
        _, result, _ = await resume_run(workspace, runner, finalize=True)

        assert result.status is RunStatus.COMPLETED and runner.dockerfile_prompts == []
        assert "Dockerfile" in github.remote_files(REPO, "main")

    async def test_a_finished_run_is_not_given_a_second_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, FinalizingRunner(), finalize=True)
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        runner = FinalizingRunner()
        stage = DockerfileStage(gitops, runner, say=lambda t: None)
        await stage.run(RunContext(idea="x", workspace_path=workspace), ProvenanceRecorder(workspace, gitops))
        assert runner.dockerfile_prompts == []


# --------------------------------------------------------------- status, resume point, CLI


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
epic_count = 1
story_count = 1
story_ids = ["1.1"]

[repo]
full_name = "me/todo"

[scaffold]
scaffolded_at = "2026-10-28T10:20:00Z"

[epics.epic-1]
branch = "epic-1-core"
story_ids = ["1.1"]
started_at = "2026-10-28T10:21:00Z"

[implementation.epic-1.story-1]
story_id = "1.1"
completed_at = "2026-10-28T11:00:00Z"
"""
MERGED = '\n[epics.epic-1.merged]\npull_request = 1\nmerged_at = "2026-10-28T11:05:00Z"\n'
DOCKERFILE = '\n[dockerfile]\nbase_image = "python:3.12-slim"\nport = 8000\ngenerated_at = "2026-10-28T11:10:00Z"\n'


def progress_of(text: str):
    result = progress_from_lock(tomllib.loads(text))
    assert result is not None
    return result


class TestStatusAndResumePoint:
    def test_the_last_epic_open_is_still_implementation(self) -> None:
        assert progress_of(HEADER).phase == "implementation"

    def test_every_epic_merged_is_finalization(self) -> None:
        progress = progress_of(HEADER + MERGED)
        assert progress.phase == "finalization" and progress.dockerfile is None
        assert "finalization: generating the Dockerfile" in render_status(progress)

    def test_a_generated_dockerfile_is_shown(self) -> None:
        progress = progress_of(HEADER + MERGED + DOCKERFILE)
        assert progress.phase == "finalization" and progress.dockerfile["port"] == 8000
        output = render_status(progress)
        assert "finalization: recording completion" in output
        assert "Dockerfile:  generated (python:3.12-slim, port 8000)" in output

    def test_a_completed_run_is_done_and_still_shows_its_dockerfile(self) -> None:
        text = HEADER + MERGED + DOCKERFILE + '\n[completed]\ncompleted_at = "2026-10-28T11:15:00Z"\n'
        progress = progress_of(text)
        assert progress.phase == "done" and "Dockerfile:  generated" in render_status(progress)

    def test_resume_says_where_it_is_going(self) -> None:
        from foundry.resume import resume_point

        assert resume_point(progress_of(HEADER + MERGED)) == "finalization (the Dockerfile)"
        assert resume_point(progress_of(HEADER + MERGED + DOCKERFILE)) == "completion"


class TestTheCommand:
    def test_resume_runs_to_a_committed_dockerfile_and_a_completed_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asyncio.run(whole_run(workspace, tmp_path, FinalizingRunner(cancel_after="2.1"), finalize=True))
        runner = FinalizingRunner()

        def build(directory: Path, private: bool):
            ops = quiet(GitOps.from_env(directory, require_token=True))
            return [
                RepoStage(ops, ScriptedNamePrompt()),
                ImplementationStage(ops, runner, on_test_output=lambda t: None, say=lambda t: None),
                DockerfileStage(ops, runner, on_test_output=lambda t: None, say=lambda t: None),
            ], ops

        monkeypatch.setattr(cli, "_build_stages", build)
        result = cli_runner.invoke(cli.app, ["resume"])

        assert result.exit_code == 0, result.output
        assert "Resume point: finalization (the Dockerfile)" in result.output
        assert "finished" in result.output
        assert SUBJECT in subjects(workspace)
        status = cli_runner.invoke(cli.app, ["status"])
        assert "Dockerfile:  generated (python:3.12-slim, port 8000)" in status.output
        assert "Status:      completed" in status.output

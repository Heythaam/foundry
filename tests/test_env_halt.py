"""Story 4.5: an environment failure stops the run at once, saved to the lock, with what to fix."""

from __future__ import annotations

import errno
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli
from foundry.context import EnvFailure, FoundryError, InfraFailure, RunContext
from foundry.failures import (
    GENERIC_FIX,
    BackoffPolicy,
    RetryingFailureHandler,
    describe_halt,
    fix_hint,
)
from foundry.lock import LockFile
from foundry.orchestrator import Orchestrator, RunInProgress
from foundry.provenance import ProvenanceRecorder
from tests.fake_github import FakeGitHub
from tests.helpers import ScriptedCheckpoint
from tests.planned import Answers, ImplementingRunner, instant_backoff
from tests.test_implementation_loop import whole_run

REAL_BUILD_STAGES = cli._build_stages  # captured before the autouse fixture stubs it
cli_runner = CliRunner()


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


class TestFixHints:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("GITHUB_TOKEN is not set. Foundry creates a repo", "Set the GITHUB_TOKEN environment variable"),
            ("GitHub rejected GITHUB_TOKEN while trying to x: Bad credentials", "Create a new token"),
            ("GitHub refused to create: Forbidden. The token needs `repo` scope", "`repo` and `workflow` scope"),
            ("could not find 'claude' on PATH; install Claude Code to run agents", "Install Claude Code"),
            ("git was not found on PATH; install git to use Foundry", "Install git"),
            ("the disk is full (No space left on device)", "Free up disk space"),
            ("permission denied 'foundry.lock': nope", "can write to the project directory"),
            ("could not launch editor 'nano': x", "Set EDITOR"),
            ("the test command could not run ('pytest')", "Fix `[run.test_runner]`"),
            ("no CI checks appeared for abc1234 after 180s", "ci.yml is on main"),
            ("cannot start story 1.1: the working tree has uncommitted changes (a.py)", "Commit or stash"),
        ],
    )
    def test_known_problems_say_what_to_fix(self, message: str, expected: str) -> None:
        assert expected in fix_hint(EnvFailure(message))

    def test_an_explicit_fix_wins(self) -> None:
        assert fix_hint(EnvFailure("GITHUB_TOKEN is not set", fix="Do this instead")) == "Do this instead"

    def test_an_unknown_problem_still_gets_an_instruction(self) -> None:
        assert fix_hint(EnvFailure("something odd")) == GENERIC_FIX

    def test_the_halt_message_has_the_error_the_fix_and_the_way_back(self) -> None:
        text = describe_halt(EnvFailure("GITHUB_TOKEN is not set."))
        lines = text.splitlines()
        assert lines[0] == "GITHUB_TOKEN is not set."
        assert lines[1].startswith("To fix: Set the GITHUB_TOKEN environment variable")
        assert "foundry resume" in lines[2]


class TestTheHaltInTheRun:
    @property
    def ENV(self) -> EnvFailure:  # a fresh instance each time: errors carry state
        return EnvFailure("could not find 'claude' on PATH; install Claude Code to run agents")

    async def test_an_environment_failure_in_a_session_stops_the_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = self.ENV
        with pytest.raises(EnvFailure) as raised:
            await whole_run(workspace, tmp_path, runner)

        assert runner.stories == ["1.1"]  # no later story was started
        assert raised.value.fix and "Install Claude Code" in raised.value.fix
        assert raised.value.context["halted"] is True

    async def test_nothing_is_retried_and_no_human_is_asked(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = self.ENV
        human = Answers("retry")
        backoff = instant_backoff()
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-story-1.1": human}))
        with pytest.raises(EnvFailure):
            await whole_run(workspace, tmp_path, runner, failures=failures, backoff=backoff)

        assert runner.stories == ["1.1"]  # exactly one call
        assert backoff.waits == [] and human.documents == []  # type: ignore[attr-defined]

    async def test_the_state_is_saved_to_the_lock_and_to_git(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.2"] = self.ENV
        with pytest.raises(EnvFailure):
            await whole_run(workspace, tmp_path, runner)

        halt = lock(workspace)["halts"]["halt-1"]
        assert halt["stage"] == "implementation"
        assert halt["category"] == "environment"
        assert "could not find 'claude'" in halt["message"]
        assert "Install Claude Code" in halt["fix"]
        assert (halt["epic"], halt["story"]) == (1, "1.2")
        assert halt["branch"] == "epic-1-core-storage"
        assert halt["head_sha"] == git(workspace, "rev-parse", "HEAD~1")  # the halt commit follows it
        assert halt["halted_at"].endswith("Z")
        assert git(workspace, "log", "-1", "--format=%s").startswith("foundry: halted (implementation)")
        # story 1.1 is still recorded as done: resume has exactly where the run got to
        assert set(lock(workspace)["implementation"]["epic-1"]) == {"story-1"}

    async def test_the_run_stays_in_progress_so_it_can_be_resumed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = self.ENV
        with pytest.raises(EnvFailure):
            await whole_run(workspace, tmp_path, runner)

        data = lock(workspace)
        assert LockFile.for_workspace(workspace).status() == "in_progress"
        assert "completed" not in data and "cancelled" not in data
        with pytest.raises(RunInProgress):  # a new run here is refused: resume is the way back
            Orchestrator._ensure_can_start(RunContext(idea="again", workspace_path=workspace))

    async def test_a_second_halt_gets_its_own_section(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = self.ENV
        with pytest.raises(EnvFailure):
            await whole_run(workspace, tmp_path, runner)

        recorder = ProvenanceRecorder(workspace)
        section = await recorder.record_halt("implementation", "environment", "again", "fix it", {})
        assert section == "halts.halt-2"
        assert set(lock(workspace)["halts"]) == {"halt-1", "halt-2"}

    @pytest.mark.parametrize(
        ("raised", "fix"),
        [
            (OSError(errno.ENOSPC, "No space left on device"), "Free up disk space"),
            (PermissionError(errno.EACCES, "Permission denied", "src/x.py"), "can write to the project directory"),
            (ValueError("nobody planned for this"), "Fix the problem described above"),
        ],
    )
    async def test_other_environment_failures_halt_the_same_way(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, raised: Exception, fix: str
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = raised
        with pytest.raises(FoundryError) as caught:
            await whole_run(workspace, tmp_path, runner)

        assert caught.value.category.value == "environment"
        assert fix in lock(workspace)["halts"]["halt-1"]["fix"]
        assert runner.stories == ["1.1"]

    async def test_a_test_command_that_cannot_run_halts_instead_of_retrying(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        failures = RetryingFailureHandler(ScriptedCheckpoint({}))
        with pytest.raises(EnvFailure, match="could not run"):
            await whole_run(
                workspace, tmp_path, runner, command="definitely-not-a-program-xyz", failures=failures
            )
        assert runner.stories == ["1.1"]  # not retried
        assert "install what the test command needs" in lock(workspace)["halts"]["halt-1"]["fix"]

    async def test_infrastructure_failures_are_not_halts(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = InfraFailure("API down")
        with pytest.raises(InfraFailure):
            await whole_run(workspace, tmp_path, runner)
        assert "halts" not in lock(workspace)

    async def test_failing_to_save_the_halt_never_hides_the_real_error(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(self, *args, **kwargs):
            raise OSError(errno.ENOSPC, "no room to write the halt")

        monkeypatch.setattr(ProvenanceRecorder, "record_halt", broken)
        runner = ImplementingRunner()
        runner.errors["1.1"] = self.ENV
        with pytest.raises(EnvFailure) as raised:
            await whole_run(workspace, tmp_path, runner)

        assert "could not find 'claude'" in raised.value.message
        assert raised.value.context["halted"] is False


class TestWhatTheHumanReads:
    @staticmethod
    def run_pro(monkeypatch: pytest.MonkeyPatch, error: Exception):
        class Failing:
            name = "failing"

            async def run(self, ctx, recorder) -> None:
                raise error

        monkeypatch.setattr(cli, "_build_stages", lambda *args: ([Failing()], None))
        return cli_runner.invoke(cli.app, ["pro", "an idea"], env={"EDITOR": ""})

    def test_a_halt_prints_the_error_the_fix_and_how_to_continue(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self.run_pro(
            monkeypatch,
            EnvFailure("GITHUB_TOKEN is not set. Foundry creates and updates a GitHub repository."),
        )
        assert result.exit_code == 1
        assert "GITHUB_TOKEN is not set." in result.output
        assert "To fix: Set the GITHUB_TOKEN environment variable" in result.output
        assert "foundry resume" in result.output
        assert lock(workspace)["halts"]["halt-1"]["stage"] == "failing"

    def test_the_unsaved_halt_says_so(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(self, *args, **kwargs):
            raise OSError("no")

        monkeypatch.setattr(ProvenanceRecorder, "record_halt", broken)
        result = self.run_pro(monkeypatch, EnvFailure("the disk is full (x)"))
        assert "To fix: Free up disk space" in result.output
        assert "could not be saved to foundry.lock" in result.output

    def test_other_categories_print_only_their_message(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self.run_pro(monkeypatch, InfraFailure("GitHub is down"))
        assert result.exit_code == 1
        assert "GitHub is down" in result.output and "To fix" not in result.output


class TestBuildStages:
    def test_a_missing_token_is_reported_before_any_planning_with_its_fix(
        self, workspace: Path
    ) -> None:
        with pytest.raises(EnvFailure) as raised:
            REAL_BUILD_STAGES(workspace, False)
        assert "GITHUB_TOKEN" in fix_hint(raised.value)

    def test_the_cli_wires_in_the_retry_backoff_and_escalation_policy(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        monkeypatch.setenv("FOUNDRY_MAX_RETRIES", "2")
        stages, gitops = REAL_BUILD_STAGES(workspace, False)
        implementation = stages[1]
        assert isinstance(implementation.failures, RetryingFailureHandler)
        assert isinstance(implementation.backoff, BackoffPolicy)
        assert implementation.backoff.max_retries == 2
        assert implementation.git is gitops

    def test_a_bad_retry_setting_stops_the_run_before_it_starts(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        monkeypatch.setenv("FOUNDRY_MAX_RETRIES", "lots")
        monkeypatch.setattr(cli, "_build_stages", REAL_BUILD_STAGES)
        result = cli_runner.invoke(cli.app, ["pro", "an idea"])
        assert result.exit_code == 1
        assert "FOUNDRY_MAX_RETRIES" in result.output
        assert not (workspace / "foundry.lock").exists()

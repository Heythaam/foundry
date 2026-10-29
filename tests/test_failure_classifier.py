"""Story 4.1: every external call is classified before its error leaves the component."""

from __future__ import annotations

import asyncio
import errno
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from foundry.agent import AgentRequest, ClaudeCodeRunner
from foundry.checkpoint import EditorCheckpoint
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    FailureCategory,
    FoundryError,
    InfraFailure,
    RunContext,
    classify_exception,
)
from foundry.failures import FailureClassifier
from foundry.gitops import GitError, GitOps, classify_github_error, run_git
from foundry.orchestrator import Orchestrator
from foundry.testgate import run_test_command
from tests.helpers import FakeRunner  # noqa: F401  (keeps the helpers import path honest)

CODE, INFRA, ENV = FailureCategory.CODE, FailureCategory.INFRASTRUCTURE, FailureCategory.ENVIRONMENT


class TestTheThreeCategories:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("slow"),
            asyncio.TimeoutError(),
            ConnectionResetError("reset"),
            ConnectionRefusedError("refused"),
            socket.gaierror(-2, "Name or service not known"),
            subprocess.TimeoutExpired("git", 5),
        ],
    )
    def test_network_and_timeout_errors_are_infrastructure(self, exc: BaseException) -> None:
        assert classify_exception(exc).category is INFRA

    @pytest.mark.parametrize(
        ("exc", "fragment"),
        [
            (OSError(errno.ENOSPC, "No space left on device"), "disk is full"),
            (PermissionError(errno.EACCES, "Permission denied", "foundry.lock"), "permission denied"),
            (OSError(errno.EROFS, "Read-only file system"), "permission denied"),
        ],
    )
    def test_disk_and_permission_errors_are_environment_with_a_fix(
        self, exc: OSError, fragment: str
    ) -> None:
        error = classify_exception(exc)
        assert error.category is ENV
        assert fragment in error.message
        assert error.fix  # the human is told what to do
        assert error.__cause__ is exc

    @pytest.mark.parametrize(
        "exc", [ValueError("weird"), KeyError("k"), RuntimeError("?"), OSError("odd"), Exception()]
    )
    def test_unclassifiable_errors_default_to_environment(self, exc: BaseException) -> None:
        assert classify_exception(exc).category is ENV

    @pytest.mark.parametrize("cls", [CodeFailure, InfraFailure, EnvFailure])
    def test_an_already_classified_error_is_never_reclassified(self, cls: type[FoundryError]) -> None:
        error = cls("already sorted")
        assert classify_exception(error) is error

    def test_the_source_of_the_failure_is_attached(self) -> None:
        error = classify_exception(TimeoutError("t"), "push epic branch")
        assert error.source == "push epic branch"

    def test_an_existing_source_is_kept(self) -> None:
        error = classify_exception(classify_exception(TimeoutError("t"), "inner"), "outer")
        assert error.source == "inner"

    def test_keyboard_interrupt_is_not_swallowed(self) -> None:
        classifier = FailureClassifier()

        def interrupted() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            classifier.sync_call("anything", interrupted)


class TestFailureClassifierWrapper:
    async def test_a_raw_exception_leaves_the_wrapper_classified(self) -> None:
        async def boom() -> None:
            raise TimeoutError("took too long")

        with pytest.raises(InfraFailure) as raised:
            await FailureClassifier().call("fetch thing", boom)
        assert raised.value.source == "fetch thing"
        assert isinstance(raised.value.__cause__, TimeoutError)

    async def test_arguments_and_results_pass_through(self) -> None:
        async def add(a: int, b: int = 0) -> int:
            return a + b

        assert await FailureClassifier().call("add", add, 1, b=2) == 3

    async def test_unknown_errors_become_environment_failures(self) -> None:
        async def boom() -> None:
            raise ValueError("no idea")

        with pytest.raises(EnvFailure):
            await FailureClassifier().call("mystery", boom)

    async def test_cancellation_is_not_treated_as_a_failure(self) -> None:
        async def cancelled() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await FailureClassifier().call("x", cancelled)

    def test_sync_calls_are_classified_too(self) -> None:
        def boom() -> None:
            raise ConnectionError("down")

        with pytest.raises(InfraFailure):
            FailureClassifier().sync_call("sync thing", boom)


class TestEveryExternalCall:
    """One failure of each kind, driven through the real component, lands in the right category."""

    @pytest.fixture
    def ctx(self, workspace: Path) -> RunContext:
        return RunContext(idea="x", workspace_path=workspace, model_id="claude-sonnet-4-5")

    @staticmethod
    def request() -> AgentRequest:
        return AgentRequest(name="story 1.1", prompt="# Task\n")

    # --- Claude Code subprocess

    async def test_agent_rate_limit_is_infrastructure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="rate limited", is_error=True, api_error_status=429)
        with pytest.raises(FoundryError) as raised:
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(self.request(), ctx)
        assert raised.value.category is INFRA

    async def test_agent_server_error_is_infrastructure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="overloaded", is_error=True, api_error_status=529)
        with pytest.raises(FoundryError) as raised:
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(self.request(), ctx)
        assert raised.value.category is INFRA

    async def test_agent_timeout_is_infrastructure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(hang=True)
        runner = ClaudeCodeRunner(command, on_text=lambda t: None, timeout_seconds=0.5)
        with pytest.raises(FoundryError) as raised:
            await runner.run(self.request(), ctx)
        assert raised.value.category is INFRA

    async def test_agent_bad_request_is_environment(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="invalid api key", is_error=True, api_error_status=401)
        with pytest.raises(FoundryError) as raised:
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(self.request(), ctx)
        assert raised.value.category is ENV

    async def test_a_missing_claude_executable_is_environment(self, ctx) -> None:
        runner = ClaudeCodeRunner(["definitely-not-claude-xyz"], on_text=lambda t: None)
        with pytest.raises(FoundryError) as raised:
            await runner.run(self.request(), ctx)
        assert raised.value.category is ENV

    async def test_empty_agent_output_is_a_code_failure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="   ")
        with pytest.raises(FoundryError) as raised:
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(self.request(), ctx)
        assert raised.value.category is CODE

    # --- git

    async def test_git_missing_from_path_is_environment(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_git(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_git)
        with pytest.raises(GitError) as raised:
            await run_git(workspace, "status")
        assert raised.value.category is ENV

    async def test_a_git_command_that_fails_is_environment(self, workspace: Path) -> None:
        with pytest.raises(GitError) as raised:
            await run_git(workspace, "checkout", "no-such-branch")
        assert raised.value.category is ENV

    async def test_git_network_errors_are_infrastructure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m", "x"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "http://127.0.0.1:1/none.git"], cwd=workspace, check=True
        )
        with pytest.raises(FoundryError) as raised:
            await GitOps(workspace, token="t").push("main")  # nothing listens on port 1
        assert raised.value.category is INFRA

    # --- GitHub API

    @pytest.mark.parametrize(
        ("status", "category"),
        [(429, INFRA), (500, INFRA), (502, INFRA), (503, INFRA), (401, ENV), (403, ENV), (404, ENV), (422, ENV)],
    )
    def test_github_statuses(self, status: int, category: FailureCategory) -> None:
        from github import GithubException

        error = classify_github_error(GithubException(status, {"message": "x"}, {}), "do a thing")
        assert isinstance(error, FoundryError)
        assert error.category is category

    def test_github_rate_limit_403_is_infrastructure(self) -> None:
        from github import GithubException

        exc = GithubException(403, {"message": "API rate limit exceeded"}, {})
        assert classify_github_error(exc, "do a thing").category is INFRA

    def test_github_connection_errors_are_infrastructure(self) -> None:
        import requests

        assert classify_github_error(requests.ConnectionError("down"), "x").category is INFRA
        assert classify_github_error(requests.Timeout("slow"), "x").category is INFRA

    def test_an_unknown_github_exception_is_environment(self) -> None:
        assert classify_github_error(RuntimeError("?"), "x").category is ENV

    # --- the test command

    async def test_a_test_command_that_cannot_start_is_environment(self, workspace: Path) -> None:
        with pytest.raises(FoundryError) as raised:
            await run_test_command(
                "definitely-not-a-program-xyz --flag", workspace, on_output=lambda t: None
            )
        assert raised.value.category is ENV

    async def test_failing_tests_are_not_an_exception_at_all(self, workspace: Path) -> None:
        result = await run_test_command(
            f'"{sys.executable}" -c "import sys; sys.exit(1)"', workspace, on_output=lambda t: None
        )
        assert result.passed is False  # the caller decides: code failure -> retry (Story 4.2)

    # --- $EDITOR

    async def test_a_missing_editor_is_environment(self) -> None:
        checkpoint = EditorCheckpoint(["definitely-not-an-editor-xyz"])
        with pytest.raises(FoundryError) as raised:
            await checkpoint.present(Artifact("x", ArtifactType.STUB), {"phase": "p"})
        assert raised.value.category is ENV


class TestTheOrchestratorBoundary:
    """AD-3: whatever a phase or stage raises, the orchestrator hands on a FoundryError."""

    class _Explodes:
        name = "explodes"

        def __init__(self, exc: BaseException) -> None:
            self.exc = exc

        async def run(self, ctx, recorder) -> None:
            raise self.exc

    @pytest.mark.parametrize(
        ("exc", "category"),
        [
            (TimeoutError("t"), INFRA),
            (ValueError("v"), ENV),
            (OSError(errno.ENOSPC, "full"), ENV),
        ],
    )
    async def test_stage_exceptions_are_classified(
        self, workspace: Path, exc: BaseException, category: FailureCategory
    ) -> None:
        from foundry.phases import StubPhase

        ctx = RunContext(idea="x", workspace_path=workspace)
        orchestrator = Orchestrator(
            [StubPhase()], checkpoint=_Approve(), stages=[self._Explodes(exc)]
        )
        with pytest.raises(FoundryError) as raised:
            await orchestrator.run(ctx)
        assert raised.value.category is category


class _Approve:
    async def present(self, artifact, metadata=None):
        from datetime import datetime, timezone

        from foundry.checkpoint import CheckpointResult

        now = datetime.now(timezone.utc)
        return CheckpointResult(artifact, artifact.sha256_hash, now, now)

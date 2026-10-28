"""Story 3.4: the test execution gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from foundry.context import CodeFailure, EnvFailure, RunContext
from foundry.epics import parse_stories
from foundry.implementation import FailureHandler, HaltOnFailure, ImplementationStage, Recovery
from foundry.testgate import GateResult, parse_tests_passed, run_test_command
from tests.planned import EPICS

STORY = parse_stories(EPICS)[0]


def python_command(tmp_path: Path, body: str, name: str = "check.py") -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return subprocess.list2cmdline([sys.executable, str(script)])


def stage(**kwargs) -> ImplementationStage:
    return ImplementationStage(None, on_test_output=lambda text: None, **kwargs)  # type: ignore[arg-type]


class TestRunTestCommand:
    async def test_exit_zero_passes(self, tmp_path: Path) -> None:
        command = python_command(tmp_path, "print('3 passed in 0.1s')")
        result = await run_test_command(command, tmp_path, on_output=lambda t: None)
        assert result.passed and result.exit_code == 0
        assert result.tests_passed == 3

    async def test_nonzero_exit_fails_and_keeps_the_exit_code(self, tmp_path: Path) -> None:
        command = python_command(tmp_path, "import sys; print('1 failed'); sys.exit(3)")
        result = await run_test_command(command, tmp_path, on_output=lambda t: None)
        assert not result.passed and result.exit_code == 3
        assert result.tests_passed is None

    async def test_stdout_and_stderr_are_both_captured_for_retry_context(
        self, tmp_path: Path
    ) -> None:
        command = python_command(
            tmp_path,
            "import sys\nprint('from stdout', flush=True)\n"
            "print('from stderr', file=sys.stderr, flush=True)\nsys.exit(1)",
        )
        result = await run_test_command(command, tmp_path, on_output=lambda t: None)
        assert "from stdout" in result.output and "from stderr" in result.output

    async def test_runs_from_the_given_directory(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        command = python_command(tmp_path, "import os; print(os.getcwd())")
        result = await run_test_command(command, repo, on_output=lambda t: None)
        assert Path(result.output.strip()).resolve() == repo.resolve()

    async def test_output_is_forwarded_as_it_is_produced(self, tmp_path: Path) -> None:
        seen: list[str] = []
        command = python_command(tmp_path, "print('hello from tests')")
        await run_test_command(command, tmp_path, on_output=seen.append)
        assert "hello from tests" in "".join(seen)

    async def test_shell_syntax_works(self, tmp_path: Path) -> None:
        command = python_command(tmp_path, "print('1 passed')") + " && " + python_command(
            tmp_path, "print('2 passed')", "second.py"
        )
        result = await run_test_command(command, tmp_path, on_output=lambda t: None)
        assert result.passed and result.tests_passed == 2

    async def test_duration_is_measured(self, tmp_path: Path) -> None:
        command = python_command(tmp_path, "import time; time.sleep(0.3)")
        result = await run_test_command(command, tmp_path, on_output=lambda t: None)
        assert result.duration_seconds >= 0.3

    async def test_a_hung_suite_is_killed_and_reported_as_failed(self, tmp_path: Path) -> None:
        command = python_command(tmp_path, "import time; print('started', flush=True); time.sleep(60)")
        result = await run_test_command(command, tmp_path, timeout=1.0, on_output=lambda t: None)
        assert result.timed_out and not result.passed
        assert "started" in result.output and "timed out" in result.output

    async def test_a_missing_program_is_an_environment_failure_not_a_failed_story(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(EnvFailure, match="could not run"):
            await run_test_command(
                "definitely-not-a-real-test-runner-xyz --all", tmp_path, on_output=lambda t: None
            )

    async def test_a_long_failing_suite_that_mentions_missing_commands_is_still_a_failure(
        self, tmp_path: Path
    ) -> None:
        body = "print('x' * 50 + ' command not found ' + 'y' * 600)\nraise SystemExit(1)"
        result = await run_test_command(python_command(tmp_path, body), tmp_path, on_output=lambda t: None)
        assert not result.passed

    async def test_huge_output_keeps_the_tail(self, tmp_path: Path) -> None:
        body = "import sys\nsys.stdout.write('a' * 3_000_000 + '\\nFAILED THE END\\n')\nsys.exit(1)"
        result = await run_test_command(python_command(tmp_path, body), tmp_path, on_output=lambda t: None)
        assert len(result.output) < 3_000_000 and "FAILED THE END" in result.output


class TestParseTestsPassed:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("........\n309 passed in 89.88s (0:01:29)\n", 309),
            ("1 failed, 12 passed, 2 skipped in 3s", 12),
            ("Test Suites: 2 passed, 2 total\nTests:       7 passed, 7 total\n", 7),
            ("test result: ok. 3 passed; 0 failed\ntest result: ok. 4 passed; 0 failed\n", 7),
            ("  5 passing (12ms)\n", 5),
            ("Ran 11 tests in 0.004s\n\nOK\n", 11),
            ("everything is fine\n", None),
            ("", None),
        ],
    )
    def test_reads_common_runner_summaries(self, output: str, expected: int | None) -> None:
        assert parse_tests_passed(output) == expected


class TestGate:
    ctx = RunContext("i")

    async def test_passing_tests_proceed_with_the_result(self, workspace: Path, tmp_path: Path) -> None:
        command = python_command(tmp_path, "print('4 passed')")
        recovery = await stage().run_gate(STORY, command, RunContext("i", workspace_path=workspace))
        assert recovery.result.passed and recovery.result.tests_passed == 4 and recovery.retries == 0

    async def test_failing_tests_are_handed_to_the_failure_handler_with_the_output(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        received: list[GateResult] = []

        class Handler:
            async def on_test_failure(self, story, result, ctx) -> Recovery:
                received.append(result)
                return Recovery(GateResult(True, 0, "fixed\n2 passed", 1.0, 2), retries=1)

        command = python_command(tmp_path, "import sys; print('AssertionError: boom'); sys.exit(1)")
        recovery = await stage(failures=Handler()).run_gate(
            STORY, command, RunContext("i", workspace_path=workspace)
        )

        assert "AssertionError: boom" in received[0].output  # captured for retry context
        assert recovery.retries == 1 and recovery.result.tests_passed == 2

    async def test_the_default_handler_halts_with_the_story_and_the_output(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        command = python_command(tmp_path, "import sys; print('AssertionError: boom'); sys.exit(1)")
        with pytest.raises(CodeFailure) as info:
            await stage().run_gate(STORY, command, RunContext("i", workspace_path=workspace))

        assert "story 1.1" in info.value.message and "AssertionError: boom" in info.value.message
        assert "AssertionError: boom" in info.value.context["output"]
        assert info.value.context["story"] == "1.1"

    async def test_a_handler_that_returns_a_red_result_does_not_pass_the_gate(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        class Lying:
            async def on_test_failure(self, story, result, ctx) -> Recovery:
                return Recovery(result)  # still red

        command = python_command(tmp_path, "import sys; sys.exit(1)")
        with pytest.raises(CodeFailure, match="still fails"):
            await stage(failures=Lying()).run_gate(
                STORY, command, RunContext("i", workspace_path=workspace)
            )

    async def test_the_gate_runs_in_the_repo_root(self, workspace: Path, tmp_path: Path) -> None:
        command = python_command(tmp_path, "import os, sys; sys.exit(0 if os.path.exists('marker') else 1)")
        (workspace / "marker").write_text("x", encoding="utf-8")
        recovery = await stage().run_gate(STORY, command, RunContext("i", workspace_path=workspace))
        assert recovery.result.passed

    def test_handlers_conform_to_the_protocol(self) -> None:
        assert isinstance(HaltOnFailure(), FailureHandler)

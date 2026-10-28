from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from foundry.agent import (
    DEFAULT_ALLOWED_TOOLS,
    AgentRequest,
    AgentRunner,
    ClaudeCodeRunner,
    classify_agent_failure,
)
from foundry.context import (
    CodeFailure,
    EnvFailure,
    FailureCategory,
    InfraFailure,
    RunContext,
)

PROMPT_PATH = Path(".foundry") / "current-story.md"


@pytest.fixture
def ctx(workspace: Path) -> RunContext:
    return RunContext(idea="x", workspace_path=workspace, model_id="claude-sonnet-4-5")


def request(prompt: str = "# Task: demo\n") -> AgentRequest:
    return AgentRequest(name="demo", prompt=prompt)


class TestStreaming:
    async def test_text_deltas_reach_the_terminal_as_they_arrive(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(deltas=["one ", "two ", "three"], delay=0.4, result="one two three")
        arrivals: list[tuple[float, str]] = []
        runner = ClaudeCodeRunner(command, on_text=lambda t: arrivals.append((time.monotonic(), t)))

        started = time.monotonic()
        await runner.run(request(), ctx)
        finished = time.monotonic()

        assert [text for _, text in arrivals] == ["one ", "two ", "three"]
        # NFR-3: the first chunk is shown long before the process ends, not flushed at exit
        assert arrivals[0][0] - started < (finished - started) - 0.6
        assert arrivals[-1][0] - arrivals[0][0] >= 0.6

    async def test_result_text_is_the_deliverable(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(deltas=["draft"], result="# Final document")
        result = await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)
        assert result.text == "# Final document"

    async def test_unicode_and_large_results_survive(self, fake_claude, ctx) -> None:
        big = "café — ☕ " + ("x" * 200_000)
        command, _ = fake_claude(deltas=["café — ☕"], result=big)
        chunks: list[str] = []
        result = await ClaudeCodeRunner(command, on_text=chunks.append).run(request(), ctx)
        assert result.text == big
        assert chunks == ["café — ☕"]

    async def test_non_json_noise_is_ignored(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(deltas=["a"], garbage=True, result="ok")
        assert (await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)).text == "ok"

    def test_runner_satisfies_the_protocol(self) -> None:
        assert isinstance(ClaudeCodeRunner(["claude"]), AgentRunner)


class TestInvocation:
    async def test_prompt_is_written_to_current_story_md_and_removed_afterwards(
        self, fake_claude, ctx, workspace: Path
    ) -> None:
        command, record = fake_claude(result="done")
        await ClaudeCodeRunner(command, on_text=lambda t: None).run(request("# Task: hello\n"), ctx)

        seen = json.loads(record.read_text(encoding="utf-8"))
        assert seen["prompt_file"] == "# Task: hello\n"  # present while the agent ran
        assert not (workspace / PROMPT_PATH).exists()  # AD-7: gone afterwards

    async def test_prompt_file_is_removed_even_when_the_agent_fails(
        self, fake_claude, ctx, workspace: Path
    ) -> None:
        command, _ = fake_claude(exit=1, stderr="boom")
        with pytest.raises(EnvFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)
        assert not (workspace / PROMPT_PATH).exists()

    async def test_runs_in_the_workspace_with_the_pinned_model_and_restricted_tools(
        self, fake_claude, ctx, workspace: Path
    ) -> None:
        command, record = fake_claude(result="done")
        await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

        seen = json.loads(record.read_text(encoding="utf-8"))
        argv = seen["argv"]
        assert Path(seen["cwd"]).resolve() == workspace.resolve()
        assert argv[argv.index("--model") + 1] == "claude-sonnet-4-5"
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--include-partial-messages" in argv and "--no-session-persistence" in argv
        assert ".foundry/current-story.md" in argv[argv.index("-p") + 1]
        tools = argv[argv.index("--allowedTools") + 1 :]
        assert tools == list(DEFAULT_ALLOWED_TOOLS)
        assert not {"Bash", "Write", "Edit"} & set(tools)

    async def test_each_call_is_a_fresh_subprocess(self, fake_claude, ctx) -> None:
        command, record = fake_claude(result="done")
        runner = ClaudeCodeRunner(command, on_text=lambda t: None)
        await runner.run(request("first"), ctx)
        first = json.loads(record.read_text(encoding="utf-8"))
        await runner.run(request("second"), ctx)
        second = json.loads(record.read_text(encoding="utf-8"))
        assert (first["prompt_file"], second["prompt_file"]) == ("first", "second")
        assert "--resume" not in second["argv"] and "--continue" not in second["argv"]


class TestFailures:
    async def test_missing_claude_binary_is_an_environment_failure(self, ctx) -> None:
        with pytest.raises(EnvFailure, match="Claude Code") as info:
            await ClaudeCodeRunner(["claude-does-not-exist-xyz"]).run(request(), ctx)
        assert info.value.category is FailureCategory.ENVIRONMENT

    async def test_rate_limit_on_stderr_is_infrastructure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(exit=1, stderr="API Error: 429 rate limit exceeded")
        with pytest.raises(InfraFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

    async def test_error_result_with_5xx_status_is_infrastructure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="Overloaded", is_error=True, api_error_status=529)
        with pytest.raises(InfraFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

    async def test_auth_failure_is_environment(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="Invalid API key", is_error=True, api_error_status=401)
        with pytest.raises(EnvFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

    async def test_unrecognised_failure_defaults_to_environment(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(exit=2, stderr="something strange")
        with pytest.raises(EnvFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

    async def test_empty_output_is_a_code_failure(self, fake_claude, ctx) -> None:
        command, _ = fake_claude(result="   ")
        with pytest.raises(CodeFailure):
            await ClaudeCodeRunner(command, on_text=lambda t: None).run(request(), ctx)

    async def test_hung_agent_is_killed_and_reported_as_infrastructure(
        self, fake_claude, ctx, workspace: Path
    ) -> None:
        command, _ = fake_claude(hang=True)
        runner = ClaudeCodeRunner(command, on_text=lambda t: None, timeout_seconds=1.0)
        started = time.monotonic()
        with pytest.raises(InfraFailure, match="did not finish"):
            await runner.run(request(), ctx)
        assert time.monotonic() - started < 20
        assert not (workspace / PROMPT_PATH).exists()

    def test_classifier_rules(self) -> None:
        assert isinstance(classify_agent_failure("x", 429), InfraFailure)
        assert isinstance(classify_agent_failure("x", 503), InfraFailure)
        assert isinstance(classify_agent_failure("x", 403), EnvFailure)
        assert isinstance(classify_agent_failure("connection reset by peer"), InfraFailure)
        assert isinstance(classify_agent_failure("???"), EnvFailure)

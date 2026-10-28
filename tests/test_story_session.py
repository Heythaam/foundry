"""Story 3.3: each story is implemented by its own Claude Code subprocess (AD-7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.agent import ClaudeCodeRunner
from foundry.context import EnvFailure, FailureCategory, InfraFailure, RunContext
from foundry.epics import parse_stories
from foundry.implementation import IMPLEMENTATION_TOOLS, ImplementationStage, build_story_prompt
from tests.planned import ARCHITECTURE, EPICS

STORIES = {story.id: story for story in parse_stories(EPICS)}
COMMAND = "pytest -q"


def sessions(record: Path) -> list[dict]:
    log = Path(str(record) + ".log")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def stage_with(fake_claude, workspace: Path, on_text=lambda text: None, **scenario):
    command, record = fake_claude(result="done", **scenario)
    runner = ClaudeCodeRunner(command=command, on_text=on_text, allowed_tools=IMPLEMENTATION_TOOLS)
    # The session needs no git: GitOps is only used by the epic/commit steps.
    return ImplementationStage(gitops=None, runner=runner), record  # type: ignore[arg-type]


class TestPrompt:
    def test_contains_story_text_acceptance_criteria_and_the_architecture_inlined(self) -> None:
        prompt = build_story_prompt(STORIES["1.1"], ARCHITECTURE, COMMAND)

        assert "# Task: implement story 1.1: Todo Model" in prompt
        assert "As a user, I want todos, So that I can track work." in prompt
        assert "**Acceptance Criteria:**" in prompt
        assert "**Given** a title **When** I create a todo **Then** it has an id" in prompt
        assert ARCHITECTURE.strip() in prompt  # the document itself, not a path to it
        assert prompt.index("## Story") < prompt.index("## Architecture")

    def test_tells_the_agent_the_test_command_and_not_to_commit(self) -> None:
        prompt = build_story_prompt(STORIES["1.1"], ARCHITECTURE, "pip install -e . && pytest -q")
        assert "`pip install -e . && pytest -q`" in prompt
        assert "Do not run `git commit`" in prompt
        assert "foundry.lock" in prompt

    def test_only_the_current_story_is_included(self) -> None:
        prompt = build_story_prompt(STORIES["1.1"], ARCHITECTURE, COMMAND)
        assert "Persistence" not in prompt and "Add Command" not in prompt


class TestSession:
    async def test_the_prompt_file_the_subprocess_sees_holds_story_and_architecture(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, record = stage_with(fake_claude, workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))

        seen = sessions(record)[0]["prompt_file"]
        assert "Todo Model" in seen and "Acceptance Criteria" in seen
        assert ARCHITECTURE.strip() in seen

    async def test_the_subprocess_runs_in_the_repo_root(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, record = stage_with(fake_claude, workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert Path(sessions(record)[0]["cwd"]).resolve() == workspace.resolve()

    async def test_the_prompt_file_is_deleted_when_the_session_ends(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, _ = stage_with(fake_claude, workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert not (workspace / ".foundry" / "current-story.md").exists()

    async def test_the_prompt_file_is_deleted_even_when_the_session_fails(
        self, workspace: Path, fake_claude
    ) -> None:
        command, _ = fake_claude(exit=1, stderr="boom")
        stage = ImplementationStage(None, ClaudeCodeRunner(command=command))  # type: ignore[arg-type]
        with pytest.raises(EnvFailure):
            await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert not (workspace / ".foundry" / "current-story.md").exists()

    async def test_every_story_gets_its_own_subprocess_and_its_own_prompt(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, record = stage_with(fake_claude, workspace)
        ctx = RunContext("i", workspace_path=workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, ctx)
        await stage.run_session(STORIES["1.2"], ARCHITECTURE, COMMAND, ctx)

        first, second = sessions(record)
        assert first["pid"] != second["pid"]
        assert "Todo Model" in first["prompt_file"] and "Persistence" not in first["prompt_file"]
        assert "Persistence" in second["prompt_file"] and "Todo Model" not in second["prompt_file"]

    async def test_no_session_state_is_carried_between_stories(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, record = stage_with(fake_claude, workspace)
        ctx = RunContext("i", workspace_path=workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, ctx)
        await stage.run_session(STORIES["1.2"], ARCHITECTURE, COMMAND, ctx)

        for session in sessions(record):
            argv = session["argv"]
            assert "--no-session-persistence" in argv
            assert not {"--resume", "--continue", "-c", "-r", "--session-id"} & set(argv)

    async def test_output_streams_to_the_terminal_as_it_arrives(
        self, workspace: Path, fake_claude
    ) -> None:
        chunks: list[str] = []
        stage, _ = stage_with(
            fake_claude, workspace, on_text=chunks.append, deltas=["Reading ", "the ", "story\n"]
        )
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert chunks == ["Reading ", "the ", "story\n"]

    async def test_the_session_can_change_files_in_the_repo(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, _ = stage_with(fake_claude, workspace, writes={"src/todo.py": "x = 1\n"})
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert (workspace / "src" / "todo.py").read_text(encoding="utf-8") == "x = 1\n"

    async def test_implementation_sessions_get_edit_and_shell_tools(
        self, workspace: Path, fake_claude
    ) -> None:
        stage, record = stage_with(fake_claude, workspace)
        await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        argv = sessions(record)[0]["argv"]
        allowed = argv[argv.index("--allowedTools") + 1 :]
        assert {"Write", "Edit", "Bash", "Read"} <= set(allowed)

    def test_planning_sessions_stay_read_only(self) -> None:
        assert not {"Write", "Edit", "Bash"} & set(ClaudeCodeRunner().allowed_tools)

    def test_the_default_runner_is_a_claude_code_runner_with_implementation_tools(self) -> None:
        runner = ImplementationStage(None).runner  # type: ignore[arg-type]
        assert isinstance(runner, ClaudeCodeRunner)
        assert list(runner.allowed_tools) == list(IMPLEMENTATION_TOOLS)


class TestSessionFailures:
    async def test_rate_limits_are_infrastructure_failures(
        self, workspace: Path, fake_claude
    ) -> None:
        command, _ = fake_claude(result="rate limit exceeded", is_error=True, api_error_status=429)
        stage = ImplementationStage(None, ClaudeCodeRunner(command=command))  # type: ignore[arg-type]
        with pytest.raises(InfraFailure):
            await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))

    async def test_a_missing_claude_binary_is_an_environment_failure(self, workspace: Path) -> None:
        runner = ClaudeCodeRunner(command=["definitely-not-a-real-claude-binary"])
        stage = ImplementationStage(None, runner)  # type: ignore[arg-type]
        with pytest.raises(EnvFailure) as info:
            await stage.run_session(STORIES["1.1"], ARCHITECTURE, COMMAND, RunContext("i", workspace_path=workspace))
        assert info.value.category is FailureCategory.ENVIRONMENT

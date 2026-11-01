"""Tests for foundry.console visual output."""

from __future__ import annotations

import io
import tomllib
from unittest.mock import MagicMock

from rich.console import Console

from foundry import console
from foundry.progress import progress_from_lock
from foundry.status import rich_render_status


def _capture_output(func, *args, **kwargs) -> str:
    buf = io.StringIO()
    orig = console.console
    try:
        console.console = Console(file=buf, force_terminal=False, width=80)
        func(*args, **kwargs)
        return buf.getvalue()
    finally:
        console.console = orig


def test_print_phase_start():
    out = _capture_output(console.print_phase_start, "Brainstorming", "Applying first principles...")
    assert "Phase: Brainstorming" in out
    assert "Applying first principles..." in out


def test_print_agent_chunk():
    out1 = _capture_output(console.print_agent_chunk, "brainstorm-agent", "hello world\n")
    assert "[brainstorm-agent]" in out1
    assert "hello world" in out1

    out2 = _capture_output(console.print_agent_chunk, "user-agent", "user prompt\n")
    assert "[user-agent]" in out2
    assert "user prompt" in out2

    out3 = _capture_output(console.print_agent_chunk, "implementation", "code output\n")
    assert "[implementation]" in out3
    assert "code output" in out3


def test_print_technique_separator():
    out = _capture_output(console.print_technique_separator, "Kill the Crown Jewel")
    assert "Technique: Kill the Crown Jewel" in out


def test_print_phase_complete():
    out = _capture_output(console.print_phase_complete, "Brainstorming")
    assert "Brainstorming complete" in out
    assert "✓" in out


def test_print_story_start():
    out = _capture_output(console.print_story_start, "2.3", "Add user authentication", 2, 3, 8)
    assert "Epic 2" in out
    assert "Story 2.3 of 8" in out
    assert '"Add user authentication"' in out


def test_print_test_running():
    out = _capture_output(console.print_test_running)
    assert "Running tests..." in out


def test_print_test_result():
    out_pass = _capture_output(console.print_test_result, True)
    assert "Tests passed" in out_pass

    out_fail = _capture_output(console.print_test_result, False, "AssertionError: expected True\nline 2")
    assert "Tests failed" in out_fail
    assert "AssertionError: expected True" in out_fail


def test_print_error():
    out = _capture_output(
        console.print_error,
        "Environment Failure",
        "GITHUB_TOKEN is not set",
        "export GITHUB_TOKEN=your_token",
    )
    assert "Environment Failure" in out
    assert "GITHUB_TOKEN is not set" in out
    assert "Fix: export GITHUB_TOKEN=your_token" in out


def test_print_status():
    progress_data = {
        "run_id": "r-1234",
        "idea": "Build a test app",
        "status_text": "in progress",
        "phase_text": "planning",
        "stories_text": "2 of 5 completed",
        "epics_text": "1 of 2 merged",
        "model": "claude-sonnet-4-5",
    }
    out = _capture_output(console.print_status, progress_data)
    assert "Run r-1234" in out
    assert "Idea:        Build a test app" in out
    assert "Status:      in progress" in out
    assert "Phase:       planning" in out
    assert "Stories:     2 of 5 completed" in out
    assert "Epics:       1 of 2 merged" in out
    assert "Model:       claude-sonnet-4-5" in out


def test_rich_render_status():
    lock_toml = """
[run]
run_id = "run-42"
idea = "My cool tool"
model = "claude-3-5"
started_at = "2026-10-30T10:00:00Z"
status = "completed"

[completed]
completed_at = "2026-10-30T10:10:00Z"
"""
    data = tomllib.loads(lock_toml)
    progress = progress_from_lock(data)
    assert progress is not None
    out = _capture_output(rich_render_status, progress)
    assert "Run run-42" in out
    assert "Status:      completed" in out
    assert "Phase:       finished" in out

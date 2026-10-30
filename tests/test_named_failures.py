"""Tests for format_failure_line and named failure output — Story 8.2."""

from __future__ import annotations

import re

import pytest

from foundry.context import (
    CodeFailure,
    EnvFailure,
    FoundryError,
    InfraFailure,
    format_failure_line,
)

FAILURE_LINE_PATTERN = re.compile(r"^\[.+\] failed: .+")


class TestFormatFailureLine:
    def test_phase_only_no_story(self) -> None:
        err = InfraFailure("agent 'brainstorm' timed out after 1800s")
        err.phase = "brainstorm"
        line = format_failure_line(err)
        assert line == "[brainstorm / -] failed: agent 'brainstorm' timed out after 1800s"

    def test_phase_and_story(self) -> None:
        err = CodeFailure("agent exited with status 1")
        err.phase = "planning"
        err.story = "story 1.2"
        line = format_failure_line(err)
        assert line == "[planning / story 1.2] failed: agent exited with status 1"

    def test_empty_output_distinct_message(self) -> None:
        err = CodeFailure("agent returned no output")
        err.phase = "planning"
        err.story = "story 1.2"
        line = format_failure_line(err)
        assert "agent returned no output" in line
        assert "[planning / story 1.2]" in line

    def test_timeout_distinct_from_nonzero_exit(self) -> None:
        timeout_err = InfraFailure("agent 'brainstorm' timed out after 1800s")
        timeout_err.phase = "brainstorm"
        exit_err = CodeFailure("agent exited with status 1")
        exit_err.phase = "brainstorm"

        timeout_line = format_failure_line(timeout_err)
        exit_line = format_failure_line(exit_err)

        assert "timed out" in timeout_line
        assert "exited with status" in exit_line
        assert timeout_line != exit_line

    def test_env_failure_with_fix_hint(self) -> None:
        err = EnvFailure("config key missing", fix="Set [run.model] in foundry.lock.")
        err.phase = "setup"
        line = format_failure_line(err)
        assert "[setup / -] failed: config key missing" in line
        assert "To fix: Set [run.model] in foundry.lock." in line

    def test_fix_hint_on_second_line(self) -> None:
        err = EnvFailure("something broke", fix="Do the thing.")
        err.phase = "brainstorm"
        line = format_failure_line(err)
        parts = line.split("\n")
        assert len(parts) == 2
        assert parts[0].startswith("[brainstorm / -] failed:")
        assert parts[1] == "To fix: Do the thing."

    def test_no_phase_uses_dash(self) -> None:
        err = InfraFailure("something went wrong")
        # phase and story are both None — both default to '-'
        line = format_failure_line(err)
        assert line.startswith("[- / -] failed:")

    def test_first_line_matches_failure_pattern(self) -> None:
        """The first line of format_failure_line always matches the regex."""
        for err_cls in (CodeFailure, InfraFailure, EnvFailure):
            err = err_cls("some reason")
            err.phase = "test-phase"
            first_line = format_failure_line(err).splitlines()[0]
            assert FAILURE_LINE_PATTERN.match(first_line), f"bad line: {first_line!r}"

    def test_phase_and_story_propagation(self) -> None:
        """phase and story survive being attached after construction."""
        err: FoundryError = CodeFailure("oops")
        assert err.phase is None
        assert err.story is None
        err.phase = "epic-review"
        err.story = "story 3.4"
        line = format_failure_line(err)
        assert "epic-review" in line
        assert "story 3.4" in line

    def test_no_embedded_newline_in_failure_header(self) -> None:
        """The [phase] failed: <reason> part never spans multiple lines."""
        err = CodeFailure("agent returned no output")
        err.phase = "planning"
        err.story = "story 1.1"
        first_line = format_failure_line(err).splitlines()[0]
        assert "\n" not in first_line
        assert FAILURE_LINE_PATTERN.match(first_line)

    def test_env_failure_without_fix_no_second_line(self) -> None:
        err = EnvFailure("disk full")
        err.phase = "implementation"
        line = format_failure_line(err)
        assert "\n" not in line

    def test_multiline_message_sanitized_to_first_line(self) -> None:
        """The [phase / story] header never contains an embedded newline even when message does."""
        err = CodeFailure("first line\nsecond line\nthird line")
        err.phase = "planning"
        err.story = "story 2.1"
        first_line = format_failure_line(err).splitlines()[0]
        assert "first line" in first_line
        assert "second line" not in first_line
        assert "\n" not in first_line

    def test_timeout_scenario_stderr_pattern(self) -> None:
        """Simulated timeout produces correct final-stderr-line format."""
        err = InfraFailure("agent 'brainstorm' timed out after 1800s")
        err.phase = "brainstorm"
        # story defaults to '-' when None
        line = format_failure_line(err)
        assert re.match(r"^\[brainstorm / -\] failed: .+timed out.+1800s", line)

"""Tests for SpecReadback — Story 8.1."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from foundry.context import EnvFailure, RunContext
from foundry.readback import SpecReadback


def _ctx(idea: str, yes: bool = True) -> RunContext:
    return RunContext(idea=idea, yes=yes)


class TestReadbackContent:
    def test_prints_project_name_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a todo app"))
        out = capsys.readouterr().out
        assert "Project name" in out

    def test_prints_language_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a todo app with FastAPI"))
        out = capsys.readouterr().out
        assert "Language/stack" in out
        assert "FastAPI" in out

    def test_prints_test_runner_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a todo app with FastAPI using pytest"))
        out = capsys.readouterr().out
        assert "Test runner" in out
        assert "pytest" in out

    def test_story_count_always_tbd(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a React dashboard"))
        out = capsys.readouterr().out
        assert "TBD" in out

    def test_inferred_language_marked_with_tilde(self, capsys: pytest.CaptureFixture[str]) -> None:
        # idea has no language keyword — language should be marked ~
        SpecReadback().present(_ctx("build a todo app"))
        out = capsys.readouterr().out
        # language line should mention ~ since nothing was stated
        lang_line = [l for l in out.splitlines() if "Language/stack" in l][0]
        assert "~" in lang_line

    def test_detected_language_not_inferred(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build an API using Flask"))
        out = capsys.readouterr().out
        lang_line = [l for l in out.splitlines() if "Language/stack" in l][0]
        # Flask was stated explicitly → should NOT be marked ~
        assert "~" not in lang_line

    def test_detected_runner_not_inferred(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("a project using pytest for testing"))
        out = capsys.readouterr().out
        runner_line = [l for l in out.splitlines() if "Test runner" in l][0]
        assert "~" not in runner_line

    def test_undetected_runner_marked_inferred(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a CLI app"))
        out = capsys.readouterr().out
        runner_line = [l for l in out.splitlines() if "Test runner" in l][0]
        assert "~" in runner_line

    def test_inferred_fields_explanation_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a todo app"))
        out = capsys.readouterr().out
        assert "~" in out  # the explanation mentions ~

    def test_various_frameworks_detected(self, capsys: pytest.CaptureFixture[str]) -> None:
        for keyword, expected in [
            ("React", "React"),
            ("Django", "Django"),
            ("Express", "Express"),
            ("Vue", "Vue"),
        ]:
            SpecReadback().present(_ctx(f"build a thing with {keyword}"))
            out = capsys.readouterr().out
            assert expected in out


class TestYesBypass:
    def test_yes_prints_and_returns_without_input(self, capsys: pytest.CaptureFixture[str]) -> None:
        # With yes=True there should be no call to input(); if there were, it would block.
        SpecReadback().present(_ctx("build a todo app", yes=True))
        out = capsys.readouterr().out
        assert "Project name" in out

    def test_yes_does_not_print_keypress_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        SpecReadback().present(_ctx("build a todo app", yes=True))
        out = capsys.readouterr().out
        assert "Press Enter" not in out


class TestAbortPath:
    def test_ctrl_c_raises_env_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        monkeypatch.setattr("sys.stdin", type("FakeTTYStdin", (), {"isatty": lambda self: True})())
        ctx = RunContext(idea="build a todo app", yes=False)
        with pytest.raises(EnvFailure, match="run aborted at readback"):
            SpecReadback().present(ctx)

    def test_no_lock_mutation_on_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Aborting at readback must not create or modify foundry.lock."""
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        monkeypatch.setattr("sys.stdin", type("FakeTTYStdin", (), {"isatty": lambda self: True})())
        ctx = RunContext(idea="build a todo app", yes=False, workspace_path=tmp_path)
        with pytest.raises(EnvFailure):
            SpecReadback().present(ctx)
        assert not (tmp_path / "foundry.lock").exists()

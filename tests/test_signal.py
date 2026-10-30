"""Tests for PhaseSignal — Story 8.3."""

from __future__ import annotations

import sys

import pytest

from foundry.context import RunContext
from foundry.signal import PhaseSignal


def _ctx(yes: bool = False) -> RunContext:
    return RunContext(idea="test idea", yes=yes)


def _present(ctx: RunContext) -> None:
    PhaseSignal().present(
        completed="brainstorm",
        outcome="completed",
        upcoming="prd",
        description="produce PRD artifact",
        ctx=ctx,
    )


class TestOutputFormat:
    def test_completed_line_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        _present(_ctx(yes=True))
        out = capsys.readouterr().out
        assert "brainstorm" in out
        assert "completed" in out

    def test_upcoming_line_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        _present(_ctx(yes=True))
        out = capsys.readouterr().out
        assert "prd" in out
        assert "produce PRD artifact" in out

    def test_checkmark_on_completed_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        _present(_ctx(yes=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        completed_line = next(l for l in lines if "brainstorm" in l)
        assert "✓" in completed_line  # ✓

    def test_arrow_on_upcoming_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        _present(_ctx(yes=True))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        upcoming_line = next(l for l in lines if "prd" in l)
        assert "→" in upcoming_line  # →


class TestYesBypass:
    def test_yes_prints_and_does_not_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Should return immediately without calling input().
        _present(_ctx(yes=True))
        out = capsys.readouterr().out
        assert "brainstorm" in out

    def test_yes_does_not_call_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = []
        monkeypatch.setattr("builtins.input", lambda *a: called.append(True))
        _present(_ctx(yes=True))
        assert called == []


class TestNoTTYBypass:
    def test_no_tty_does_not_call_input(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # sys.stdin.isatty() returns False in test environments; verify no input() is called.
        called = []
        monkeypatch.setattr("builtins.input", lambda *a: called.append(True))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        _present(_ctx(yes=False))
        assert called == []

    def test_no_tty_still_prints(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda *a: None)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        _present(_ctx(yes=False))
        out = capsys.readouterr().out
        assert "brainstorm" in out


class TestAbortPath:
    def test_eof_proceeds_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EOFError from piped stdin is caught — present returns without raising."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
        _present(_ctx(yes=False))  # must not raise

    def test_ctrl_c_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt is not swallowed — it propagates to the caller."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            _present(_ctx(yes=False))


class TestQuickModeCompatibility:
    def test_quick_mode_behaves_identically(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = RunContext(idea="a quick idea", mode="quick", yes=True)
        PhaseSignal().present(
            completed="spec",
            outcome="one-page spec approved",
            upcoming="implementation",
            description="write and test code increments",
            ctx=ctx,
        )
        out = capsys.readouterr().out
        assert "spec" in out
        assert "implementation" in out

    def test_quick_mode_no_tty_no_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = []
        monkeypatch.setattr("builtins.input", lambda *a: called.append(True))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        ctx = RunContext(idea="a quick idea", mode="quick", yes=False)
        PhaseSignal().present(
            completed="spec",
            outcome="one-page spec approved",
            upcoming="implementation",
            description="write and test code increments",
            ctx=ctx,
        )
        assert called == []

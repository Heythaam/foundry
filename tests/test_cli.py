from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry.cli import app

runner = CliRunner()


class TestHelp:
    def test_help_lists_all_five_subcommands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("pro", "quick", "resume", "status", "cancel"):
            assert command in result.output

    def test_no_args_shows_help(self) -> None:
        result = runner.invoke(app, [])
        assert "pro" in result.output and "cancel" in result.output


class TestStartup:
    def test_help_starts_in_under_two_seconds(self) -> None:
        # Best of several runs: the budget is about what our imports cost, not about a one-off
        # OS stall (antivirus, cold disk cache) landing on a single measurement.
        timings = []
        for _ in range(4):
            started = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "-m", "foundry", "--help"], capture_output=True, text=True, timeout=30
            )
            timings.append(time.perf_counter() - started)
            assert proc.returncode == 0, proc.stderr
            assert "pro" in proc.stdout
        assert min(timings) < 2.0, f"startup took {min(timings):.2f}s at best (NFR-1): {timings}"

    def test_help_does_not_import_the_pipeline_stack(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; from foundry.cli import app; "
             "print(sorted(m for m in ('foundry.orchestrator','foundry.agent','aiosqlite','rich') if m in sys.modules))"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.stdout.strip() == "[]", proc.stdout


class TestStubs:
    @pytest.mark.parametrize("args", [["quick", "an idea"], ["resume"], ["status"]])
    def test_non_pro_commands_are_stubs(self, args: list[str]) -> None:
        result = runner.invoke(app, args)
        assert result.exit_code == 1
        assert "not implemented" in result.output


class TestPro:
    def test_pro_runs_the_pipeline_and_reports_run_id(self, workspace: Path) -> None:
        result = runner.invoke(app, ["pro", "build a todo app"])
        assert result.exit_code == 0, result.output
        assert "Run " in result.output and "finished" in result.output


class TestProCreatesLock:
    def test_lock_is_created_in_the_workspace_with_run_metadata(self, workspace: Path) -> None:
        import tomllib

        result = runner.invoke(app, ["pro", "build a todo app"])
        assert result.exit_code == 0, result.output
        data = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
        assert data["run"]["idea"] == "build a todo app"
        assert data["run"]["status"] == "in_progress"
        assert data["run"]["run_id"] in result.output

    def test_second_run_while_in_progress_prints_message_and_exits(self, workspace: Path) -> None:
        from foundry.context import RunContext
        from foundry.lock import LockFile

        LockFile.for_workspace(workspace).init_run(RunContext(idea="first", workspace_path=workspace))
        before = (workspace / "foundry.lock").read_bytes()

        result = runner.invoke(app, ["pro", "second idea"])

        assert result.exit_code == 1
        assert "Run in progress. Use `foundry resume` to continue." in result.output
        assert (workspace / "foundry.lock").read_bytes() == before


class TestModelPinning:
    def test_exact_model_id_from_env_is_recorded_in_run_section(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tomllib

        monkeypatch.setenv("FOUNDRY_MODEL", "claude-opus-4-1-20250805")
        result = runner.invoke(app, ["pro", "an idea"])
        assert result.exit_code == 0, result.output
        data = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
        assert data["run"]["model"] == "claude-opus-4-1-20250805"

    def test_default_model_is_recorded_when_env_unset(self, workspace: Path) -> None:
        import tomllib

        runner.invoke(app, ["pro", "an idea"])
        data = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
        assert data["run"]["model"] == "claude-sonnet-4-5"

    def test_alias_prints_warning_and_exits_before_touching_anything(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOUNDRY_MODEL", "claude-sonnet")
        result = runner.invoke(app, ["pro", "an idea"])
        assert result.exit_code == 1
        assert "Warning" in result.output and "version suffix" in result.output
        assert list(workspace.iterdir()) == []  # no lock, no .git, no .foundry

    def test_recorded_model_is_readable_for_the_resume_flow(self, workspace: Path) -> None:
        from foundry.lock import LockFile

        runner.invoke(app, ["pro", "an idea"])
        assert LockFile.for_workspace(workspace).read()["run"]["model"] == "claude-sonnet-4-5"

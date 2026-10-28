from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry.cancel import (
    clear_cancel,
    is_cancel_requested,
    request_cancel,
    sentinel_path,
)
from foundry.cli import app
from foundry.context import Artifact, RunContext
from foundry.lock import STATUS_CANCELLED, LockFile
from foundry.orchestrator import Orchestrator, RunStatus

runner = CliRunner()


def git_subjects(workspace: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, encoding="utf-8"
    ).stdout.splitlines()


def lock_data(workspace: Path) -> dict:
    return tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))


class Phase:
    checkpoint = False

    def __init__(self, name: str, log: list[str], on_run=None) -> None:
        self.name, self.log, self.on_run = name, log, on_run

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        self.log.append(self.name)
        if self.on_run:
            self.on_run(ctx)
        return input


def start_in_progress_run(workspace: Path) -> LockFile:
    lock = LockFile.for_workspace(workspace)
    lock.init_run(RunContext(idea="x", workspace_path=workspace))
    return lock


class TestSentinel:
    def test_sentinel_is_dot_foundry_cancel(self, tmp_path: Path) -> None:
        assert sentinel_path(tmp_path) == tmp_path / ".foundry" / "cancel"

    def test_request_without_a_run_does_nothing(self, workspace: Path) -> None:
        assert request_cancel(workspace) is False
        assert not is_cancel_requested(workspace)
        assert not (workspace / ".foundry").exists()

    def test_request_against_a_finished_run_does_nothing(self, workspace: Path) -> None:
        lock = start_in_progress_run(workspace)
        lock.append_section("completed", {"completed_at": "2026-10-28T00:00:00Z"})
        assert request_cancel(workspace) is False

    def test_request_creates_sentinel_atomically_without_leftovers(self, workspace: Path) -> None:
        start_in_progress_run(workspace)
        assert request_cancel(workspace) is True
        assert is_cancel_requested(workspace)
        assert [p.name for p in (workspace / ".foundry").iterdir()] == ["cancel"]

    def test_requesting_twice_is_harmless(self, workspace: Path) -> None:
        start_in_progress_run(workspace)
        assert request_cancel(workspace) and request_cancel(workspace)

    def test_clear_removes_the_sentinel_and_tolerates_absence(self, workspace: Path) -> None:
        start_in_progress_run(workspace)
        request_cancel(workspace)
        clear_cancel(workspace)
        assert not is_cancel_requested(workspace)
        clear_cancel(workspace)

    def test_request_never_touches_the_lock(self, workspace: Path) -> None:
        lock = start_in_progress_run(workspace)
        before = lock.path.read_bytes()
        request_cancel(workspace)
        assert lock.path.read_bytes() == before


class TestOrchestratorPolling:
    async def test_cancel_during_a_phase_stops_before_the_next_phase(self, workspace: Path) -> None:
        log: list[str] = []
        phases = [
            Phase("one", log),
            Phase("two", log, on_run=lambda ctx: request_cancel(ctx.workspace_path)),
            Phase("three", log),
        ]
        result = await Orchestrator(phases).run(RunContext(idea="x"))

        assert result.status is RunStatus.CANCELLED
        assert log == ["one", "two"]  # two finished its atomic operation; three never started

    async def test_cancel_leaves_a_cancelled_commit_and_lock_entry(self, workspace: Path) -> None:
        phases = [Phase("one", [], on_run=lambda ctx: request_cancel(ctx.workspace_path)), Phase("two", [])]
        await Orchestrator(phases).run(RunContext(idea="x"))

        assert git_subjects(workspace)[0] == "foundry: cancelled — before phase two"
        data = lock_data(workspace)
        assert data["cancelled"]["before"] == "phase two"
        assert data["cancelled"]["cancelled_at"].endswith("Z")
        assert "completed" not in data

    async def test_lock_and_history_are_consistent_after_cancel(self, workspace: Path) -> None:
        phases = [Phase("one", [], on_run=lambda ctx: request_cancel(ctx.workspace_path)), Phase("two", [])]
        ctx = RunContext(idea="x")
        await Orchestrator(phases).run(ctx)

        lock = LockFile.for_workspace(workspace)
        assert lock.status() == STATUS_CANCELLED and not lock.is_in_progress()
        assert lock_data(workspace)["run"]["status"] == "in_progress"  # [run] never edited
        assert lock_data(workspace)["run"]["run_id"] == ctx.run_id
        committed = subprocess.run(
            ["git", "show", "HEAD:foundry.lock"], cwd=workspace, capture_output=True, check=True
        ).stdout
        assert tomllib.loads(committed.decode("utf-8")) == lock_data(workspace)
        assert subprocess.run(
            ["git", "status", "--porcelain", "--", "foundry.lock"], cwd=workspace, capture_output=True, text=True
        ).stdout == ""
        assert not is_cancel_requested(workspace)

    async def test_no_partial_state_pending_ops_are_all_settled(self, workspace: Path) -> None:
        from foundry.state import StateBuffer, state_db_path

        phases = [Phase("one", [], on_run=lambda ctx: request_cancel(ctx.workspace_path)), Phase("two", [])]
        await Orchestrator(phases).run(RunContext(idea="x"))
        async with StateBuffer(state_db_path(workspace)) as buffer:
            assert await buffer.pending() == []

    async def test_cancel_during_the_last_phase_is_still_honoured(self, workspace: Path) -> None:
        phases = [Phase("only", [], on_run=lambda ctx: request_cancel(ctx.workspace_path))]
        result = await Orchestrator(phases).run(RunContext(idea="x"))
        assert result.status is RunStatus.CANCELLED
        assert git_subjects(workspace)[0] == "foundry: cancelled — before completion"

    async def test_without_cancel_the_run_completes_and_clears_nothing_extra(self, workspace: Path) -> None:
        result = await Orchestrator([Phase("one", [])]).run(RunContext(idea="x"))
        assert result.status is RunStatus.COMPLETED
        assert "cancelled" not in lock_data(workspace)

    async def test_stale_sentinel_from_a_dead_run_does_not_cancel_a_new_run(self, workspace: Path) -> None:
        sentinel_path(workspace).parent.mkdir()
        sentinel_path(workspace).write_text("stale", encoding="utf-8")
        log: list[str] = []
        result = await Orchestrator([Phase("one", log)]).run(RunContext(idea="x"))
        assert result.status is RunStatus.COMPLETED and log == ["one"]

    async def test_cancelled_run_can_be_followed_by_a_clear_error_not_a_clobber(self, workspace: Path) -> None:
        from foundry.context import EnvFailure

        phases = [Phase("one", [], on_run=lambda ctx: request_cancel(ctx.workspace_path))]
        await Orchestrator(phases).run(RunContext(idea="x"))
        before = (workspace / "foundry.lock").read_bytes()
        with pytest.raises(EnvFailure, match="cancelled"):
            await Orchestrator([]).run(RunContext(idea="again"))
        assert (workspace / "foundry.lock").read_bytes() == before


class TestCancelCommand:
    def test_no_run_in_progress(self, workspace: Path) -> None:
        result = runner.invoke(app, ["cancel"])
        assert result.exit_code == 1
        assert "No run in progress." in result.output

    def test_cancel_from_another_terminal_stops_a_running_pro(self, workspace: Path, monkeypatch) -> None:
        log: list[str] = []

        def other_terminal(ctx: RunContext) -> None:
            outcome = runner.invoke(app, ["cancel"])
            assert outcome.exit_code == 0, outcome.output
            assert "Cancel requested" in outcome.output

        monkeypatch.setattr(
            "foundry.cli._default_phases",
            lambda: [Phase("one", log, on_run=other_terminal), Phase("two", log)],
        )
        result = runner.invoke(app, ["pro", "build a todo app"])

        assert result.exit_code == 1
        assert "cancelled" in result.output
        assert log == ["one"]
        assert git_subjects(workspace)[0].startswith("foundry: cancelled")
        assert LockFile.for_workspace(workspace).status() == STATUS_CANCELLED

    def test_cancel_with_a_run_in_progress_drops_the_sentinel(self, workspace: Path) -> None:
        lock = start_in_progress_run(workspace)
        before = lock.path.read_bytes()
        result = runner.invoke(app, ["cancel"])
        assert result.exit_code == 0
        assert is_cancel_requested(workspace)
        assert lock.path.read_bytes() == before

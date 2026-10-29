"""The `foundry resume` command and the lock's resume-aware status."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli
from foundry.gitops import GitOps
from foundry.implementation import ImplementationStage
from foundry.lock import LockFile, names_of, status_of
from foundry.repo import RepoStage
from tests.fake_github import FakeGitHub
from tests.planned import ImplementingRunner, ScriptedNamePrompt, quiet
from tests.test_implementation_loop import whole_run
from tests.test_resume import CancelsDuring

cli_runner = CliRunner()


def use_stages(monkeypatch: pytest.MonkeyPatch, runner: ImplementingRunner) -> None:
    """`foundry resume` with a scripted agent instead of `claude`."""

    def build(workspace: Path, private: bool):
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        stage = ImplementationStage(
            gitops, runner, on_test_output=lambda t: None, say=lambda t: None
        )
        return [RepoStage(gitops, ScriptedNamePrompt()), stage], gitops

    monkeypatch.setattr(cli, "_build_stages", build)


class TestTheResumeCommand:
    def test_nothing_to_resume_is_an_error_that_says_what_to_do(self, workspace: Path) -> None:
        result = cli_runner.invoke(cli.app, ["resume"])
        assert result.exit_code == 1
        assert "no Foundry run to resume" in result.output
        assert "To fix:" in result.output

    def test_a_cancelled_run_is_resumed_to_the_end(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asyncio.run(whole_run(workspace, tmp_path, CancelsDuring("1.1")))
        runner = ImplementingRunner()
        use_stages(monkeypatch, runner)

        result = cli_runner.invoke(cli.app, ["resume"])

        assert result.exit_code == 0, result.output
        assert "Resuming run" in result.output and "story 1.2 (1 of 3 done)" in result.output
        assert "finished" in result.output
        assert runner.stories == ["1.2", "2.1"]
        assert LockFile.for_workspace(workspace).status() == "completed"

    def test_a_completed_run_cannot_be_resumed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asyncio.run(whole_run(workspace, tmp_path, ImplementingRunner()))
        use_stages(monkeypatch, ImplementingRunner())
        result = cli_runner.invoke(cli.app, ["resume"])
        assert result.exit_code == 1 and "already completed" in result.output

    def test_a_run_cancelled_again_says_it_can_be_resumed_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asyncio.run(whole_run(workspace, tmp_path, CancelsDuring("1.1")))
        use_stages(monkeypatch, CancelsDuring("1.2"))
        result = cli_runner.invoke(cli.app, ["resume"])
        assert result.exit_code == 1
        assert "cancelled" in result.output and "foundry resume" in result.output

    def test_cancel_works_on_a_resumed_run(self, workspace: Path) -> None:
        from foundry.cancel import request_cancel

        (workspace / "foundry.lock").write_text(
            '[run]\nrun_id = "r"\nidea = "i"\nmodel = "m-1"\nstarted_at = "2026-10-28T10:00:00Z"\n'
            'status = "in_progress"\n\n[cancelled]\nbefore = "x"\n', encoding="utf-8"
        )
        assert request_cancel(workspace) is False  # cancelled, nothing running
        with (workspace / "foundry.lock").open("a", encoding="utf-8") as handle:
            handle.write('\n[resumes.resume-1]\ncancellations_seen = 1\n')
        assert request_cancel(workspace) is True  # resumed, so running again


RUN = '[run]\nrun_id = "r"\nidea = "i"\nmodel = "m-1"\nstarted_at = "2026-10-28T10:00:00Z"\nstatus = "in_progress"\n'


class TestLockStatusAcrossResumes:
    def status(self, text: str) -> str | None:
        return status_of(tomllib.loads(text))

    def test_cancelled_then_resumed_is_in_progress_again(self) -> None:
        assert self.status(RUN + "\n[cancelled]\nbefore = 'a'\n") == "cancelled"
        resumed = RUN + "\n[cancelled]\nbefore = 'a'\n\n[resumes.resume-1]\ncancellations_seen = 1\n"
        assert self.status(resumed) == "in_progress"

    def test_cancelled_again_after_a_resume_is_cancelled(self) -> None:
        text = (
            RUN + "\n[cancelled]\nbefore = 'a'\n\n[resumes.resume-1]\ncancellations_seen = 1\n"
            "\n[cancelled.cancel-2]\nbefore = 'b'\n"
        )
        assert self.status(text) == "cancelled"
        assert self.status(text + "\n[resumes.resume-2]\ncancellations_seen = 2\n") == "in_progress"

    def test_completed_wins(self) -> None:
        assert self.status(RUN + "\n[cancelled]\nbefore = 'a'\n\n[completed]\ncompleted_at = 'z'\n") == "completed"

    def test_a_halt_does_not_change_the_status(self) -> None:
        assert self.status(RUN + "\n[halts.halt-1]\nmessage = 'x'\n") == "in_progress"


class TestLockHelpers:
    def lock(self, workspace: Path, text: str) -> LockFile:
        (workspace / "foundry.lock").write_text(text, encoding="utf-8")
        return LockFile.for_workspace(workspace)

    def test_section_names_are_never_reused(self, workspace: Path) -> None:
        lock = self.lock(workspace, RUN + "\n[failures.epic-1.story-1]\nretries = 1\n")
        assert lock.next_name("failures.epic-1.story-1") == "failures.epic-1.story-1.run-2"
        assert lock.next_name("failures.epic-1.story-2") == "failures.epic-1.story-2"
        lock.append_section("failures.epic-1.story-1.run-2", {"retries": 1})
        assert lock.next_name("failures.epic-1.story-1") == "failures.epic-1.story-1.run-3"

    def test_the_model_in_force_is_the_latest_change_else_the_pinned_one(self, workspace: Path) -> None:
        lock = self.lock(workspace, RUN)
        assert lock.current_model() == "m-1"
        lock.append_section("model_changes.change-1", {"from": "m-1", "to": "m-2"})
        lock.append_section("model_changes.change-2", {"from": "m-2", "to": "m-3"})
        assert lock.current_model() == "m-3"
        assert lock.read()["run"]["model"] == "m-1"  # never edited

    def test_names_of_lists_sections_that_hold_values(self) -> None:
        data = tomllib.loads(RUN + "\n[epics.epic-1]\nbranch = 'b'\n\n[epics.epic-1.merged]\npr = 1\n")
        assert names_of(data) == ["run", "epics.epic-1", "epics.epic-1.merged"]

    def test_a_resumed_run_can_hold_a_second_cancellation_and_failure_record(self, workspace: Path) -> None:
        lock = self.lock(workspace, RUN)
        lock.append_section("cancelled", {"before": "a"})
        lock.append_section("cancelled.cancel-2", {"before": "b"})
        assert lock.status() == "cancelled"

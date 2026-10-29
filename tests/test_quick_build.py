"""Story 7.2: a quick run builds its spec in tested increments, one commit each, then a Dockerfile."""

from __future__ import annotations

import asyncio
import dataclasses
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foundry import cli
from foundry.context import CodeFailure, EnvFailure
from foundry.dockerfile import DockerfileStage
from foundry.failures import RetryingFailureHandler
from foundry.gitops import GitOps
from foundry.lock import LockFile
from foundry.orchestrator import RunStatus
from foundry.provenance import ProvenanceRecorder
from foundry.quick import (
    PLAN_PATH,
    Increment,
    QuickImplementationStage,
    build_increment_prompt,
    build_plan_prompt,
    build_quick_pipeline,
    parse_increments,
    quick_progress,
)
from tests.fake_github import FakeGitHub
from tests.helpers import ScriptedCheckpoint
from tests.planned import (
    MULTI_STAGE,
    NODE_PACKAGE,
    PLAN,
    SINGLE_STAGE,
    Answers,
    QuickRunner,
    ScriptedNamePrompt,
    bad,
    checking_command,
    count_command,
    good,
    instant_backoff,
    make_spec,
    quick_run,
    quiet,
)

REPO = "link-shortener"
SUBJECTS = [
    "foundry: story Q.1 implemented — Data model",
    "foundry: story Q.2 implemented — Shortening logic",
    "foundry: story Q.3 implemented — Command line",
]
DOCKER = "foundry: dockerfile generated — python:3.12-slim, 8000"
cli_runner = CliRunner()


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


def subjects(workspace: Path) -> list[str]:
    return git(workspace, "log", "--format=%s").splitlines()


# --------------------------------------------------------------------- the increment plan


class TestParsingIncrements:
    def test_the_headings_a_planner_writes(self) -> None:
        text = (
            "### Increment 1: Data model\n\nBuild it.\n\n"
            "## Increment 2 - Logic\n\nMore.\n\n"
            "#### Story Q.3: Interface\n\nLast.\n"
        )
        found = parse_increments(text)
        assert [(i.id, i.title) for i in found] == [("Q.1", "Data model"), ("Q.2", "Logic"), ("Q.3", "Interface")]
        assert found[0].raw == "### Increment 1: Data model\n\nBuild it."

    def test_they_are_numbered_by_position_whatever_numbers_were_written(self) -> None:
        found = parse_increments("### Increment 5: A\n\nx\n\n### Increment 5: B\n\ny\n\n### Increment 1: C\n")
        assert [(i.id, i.title) for i in found] == [("Q.1", "A"), ("Q.2", "B"), ("Q.3", "C")]

    def test_text_around_the_increments_is_not_part_of_any_of_them(self) -> None:
        text = "# Plan\n\nIntro.\n\n### Increment 1: A\n\nBody A.\n\n---\n\n## Notes\n\nNot an increment.\n"
        (only,) = parse_increments(text)
        assert only.raw == "### Increment 1: A\n\nBody A."

    def test_no_increments_is_an_empty_list(self) -> None:
        assert parse_increments("Here is my plan: do everything.") == []
        assert parse_increments("") == []

    def test_an_increment_is_a_story_with_a_q_id_and_its_own_lock_key(self) -> None:
        increment = Increment(0, 2, "Logic", "### Increment 2: Logic")
        assert increment.id == "Q.2" and increment.lock_key == "quick.story-2"
        assert dataclasses.replace(increment, raw="edited").id == "Q.2"  # escalation edits keep the id

    def test_crlf_plans_parse(self) -> None:
        assert len(parse_increments("### Increment 1: A\r\n\r\nx\r\n\r\n### Increment 2: B\r\n\r\ny\r\n")) == 2


class TestThePrompts:
    def test_the_plan_prompt_sets_the_rules(self) -> None:
        prompt = build_plan_prompt("# Spec\n\nIt shortens.", "pytest -q")
        for expected in ("### Increment N: Title", "Between three and eight", "whole test suite must pass",
                         "sets the project up", "Not building", "`pytest -q`", "# Spec"):
            assert expected in prompt, expected
        assert "Problem with your previous answer" not in prompt
        assert "skill" not in prompt.lower()

    def test_a_correction_is_appended(self) -> None:
        assert "Problem with your previous answer" in build_plan_prompt("s", "t", "It had no increments.")

    def test_the_increment_prompt_says_where_the_increment_sits(self) -> None:
        plan = parse_increments(PLAN)
        prompt = build_increment_prompt(plan[1], "# Spec\n\nIt shortens.", plan, "pytest -q")
        assert "implement increment Q.2: Shortening logic" in prompt
        assert "- Q.1 Data model (already committed)" in prompt
        assert "- Q.2 Shortening logic (THIS ONE)" in prompt
        assert "- Q.3 Command line (later)" in prompt
        assert "## Spec" in prompt and "It shortens." in prompt and "`pytest -q`" in prompt
        for rule in ("git commit", "foundry.lock", "Do only this increment", "Write tests"):
            assert rule in prompt, rule

    def test_the_progress_of_a_lock(self) -> None:
        data = tomllib.loads(
            '[increments]\nids = ["Q.1", "Q.2", "Q.3"]\n'
            "[implementation.quick.story-1]\nstory_id = 'Q.1'\n"
            "[deferred.quick.story-2]\nstory_id = 'Q.2'\n"
        )
        progress = quick_progress(data)
        assert progress.planned == ("Q.1", "Q.2", "Q.3")
        assert progress.implemented == ("Q.1",) and progress.deferred == ("Q.2",)
        assert progress.remaining == ("Q.3",) and progress.planning_done
        assert not quick_progress({}).planning_done


# -------------------------------------------------------------------------- the build


class TestIncrementalCommits:
    async def test_each_increment_is_its_own_commit_in_the_required_format(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner()
        _, result = await quick_run(workspace, tmp_path, runner, through="implementation")

        assert result.status is RunStatus.COMPLETED
        log = subjects(workspace)
        assert [s for s in log if "story Q." in s] == list(reversed(SUBJECTS))
        for subject in SUBJECTS:
            assert log.count(subject) == 1
        # one session each, in order, after the spec and the plan
        assert runner.sessions == ["spec", "increment plan", "story Q.1", "story Q.2", "story Q.3"]

    async def test_a_commit_holds_that_increments_files_and_its_lock_entry_only(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        for number, subject in enumerate(SUBJECTS, start=1):
            files = git(workspace, "show", "--name-only", "--format=", f":/{subject}").splitlines()
            assert sorted(files) == ["foundry.lock", f"src/story_Q_{number}.py"]

    async def test_increments_land_on_main_with_no_branches_or_pull_requests(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        assert git(workspace, "branch", "--list").replace("*", "").split() == ["main"]
        assert github.remote_branches(REPO) == ["main"]
        assert not [name for name in lock(workspace) if name == "epics"]

    async def test_every_increment_is_pushed_as_soon_as_it_is_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        pushed: dict[str, list[str]] = {}

        class Watching(QuickRunner):
            async def run(self, request, ctx):
                if request.name.startswith("story Q."):
                    pushed[request.name] = github.remote_log(REPO, "main")
                return await super().run(request, ctx)

        await quick_run(workspace, tmp_path, Watching(), through="implementation")

        assert SUBJECTS[0] not in pushed["story Q.1"]
        assert SUBJECTS[0] in pushed["story Q.2"] and SUBJECTS[1] not in pushed["story Q.2"]
        assert SUBJECTS[1] in pushed["story Q.3"]
        assert SUBJECTS[2] in github.remote_log(REPO, "main")

    async def test_the_session_for_an_increment_runs_on_main_with_the_spec_and_plan(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner()
        await quick_run(workspace, tmp_path, runner, through="implementation")
        assert set(runner.branches) == {"main"}
        second = next(p for p in runner.prompts if "increment Q.2" in p)
        assert "Link(code, url, created_at)" in second  # the spec, inlined (AD-7)
        assert "(already committed)" in second

    async def test_it_is_not_one_big_commit(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        added = [s for s in subjects(workspace) if s.startswith("foundry: story Q.")]
        assert len(added) == 3
        tree = git(workspace, "ls-files", "src").splitlines()
        assert tree == ["src/story_Q_1.py", "src/story_Q_2.py", "src/story_Q_3.py"]


class TestThePlan:
    async def test_the_plan_is_committed_and_recorded(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")

        subject = "foundry: increments planned — 3 increments"
        assert subjects(workspace).count(subject) == 1
        files = git(workspace, "show", "--name-only", "--format=", f":/{subject}").splitlines()
        assert sorted(files) == [PLAN_PATH, "foundry.lock"]
        entry = lock(workspace)["increments"]
        assert entry["count"] == 3 and entry["ids"] == ["Q.1", "Q.2", "Q.3"]
        assert entry["titles"] == ["Data model", "Shortening logic", "Command line"]
        assert entry["attempts"] == 1 and len(entry["plan_hash"]) > 20
        assert "Build the Link model" in github.remote_show(REPO, "main", PLAN_PATH)
        assert "Build the Link model" not in (workspace / "foundry.lock").read_text(encoding="utf-8")

    async def test_the_plan_comes_after_the_scaffold_and_before_the_first_increment(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        order = [s.split(" — ")[0] for s in reversed(subjects(workspace))]
        assert order.index("foundry: scaffold") < order.index("foundry: increments planned") < order.index("foundry: story Q.1 implemented")

    async def test_a_plan_with_no_increments_is_asked_for_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(plans=["I would just write it all.", PLAN])
        await quick_run(workspace, tmp_path, runner, through="implementation")
        assert len(runner.plan_prompts) == 2
        assert "Problem with your previous answer" in runner.plan_prompts[1]
        assert lock(workspace)["increments"]["attempts"] == 2

    async def test_two_useless_plans_stop_the_run_before_any_code_is_written(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(plans=["no headings here"])
        with pytest.raises(CodeFailure, match="no increments after 2 attempts"):
            await quick_run(workspace, tmp_path, runner, through="implementation")
        assert "increments" not in lock(workspace)
        assert not [s for s in runner.sessions if s.startswith("story")]

    async def test_planning_uses_its_own_read_only_runner_in_production(self, workspace: Path) -> None:
        stage = QuickImplementationStage(quiet(GitOps(workspace)))
        assert stage.planner.allowed_tools == ["Read"]
        assert "Write" in stage.runner.allowed_tools

    async def test_running_the_stage_again_replans_and_redoes_nothing(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, _ = await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        before = subjects(workspace)

        again = QuickRunner()
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        stage = QuickImplementationStage(gitops, again, on_test_output=lambda t: None, say=lambda t: None)
        await stage.run(ctx, ProvenanceRecorder(workspace, gitops))

        assert again.sessions == [] and subjects(workspace) == before


class TestTestsComeFirst:
    async def test_a_red_increment_is_not_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(story_steps={"Q.2": [bad("Q.2")]})
        with pytest.raises(CodeFailure, match="story Q.2 .* failed its tests"):
            await quick_run(workspace, tmp_path, runner, command=checking_command(tmp_path), through="implementation")

        log = subjects(workspace)
        assert SUBJECTS[0] in log and not any("story Q.2" in s for s in log)
        assert "story-2" not in lock(workspace).get("implementation", {}).get("quick", {})
        assert not any("story Q.2" in s for s in github.remote_log(REPO, "main"))
        assert runner.sessions[-1] == "story Q.2"  # and Q.3 never started

    async def test_one_automatic_retry_with_the_failing_output_then_the_auto_fixed_commit(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(story_steps={"Q.2": [bad("Q.2"), good("Q.2")]})
        await quick_run(
            workspace, tmp_path, runner, command=checking_command(tmp_path), through="implementation",
            failures=RetryingFailureHandler(), backoff=instant_backoff(),
        )

        assert runner.sessions.count("story Q.2") == 2
        retry_prompt = [p for p in runner.prompts if "increment Q.2" in p][1]
        assert "FAILED story_Q_2.py" in retry_prompt  # the failing test output reaches the session
        assert "foundry: story Q.2 auto-fixed — Shortening logic" in subjects(workspace)
        failure = lock(workspace)["failures"]["quick"]["story-2"]
        assert failure["category"] == "code" and failure["retry_outcome"] == "passed"
        assert lock(workspace)["implementation"]["quick"]["story-2"]["retries"] == 1

    async def test_a_human_can_skip_an_increment_and_the_run_goes_on(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("skip")
        runner = QuickRunner(story_steps={"Q.2": [bad("Q.2")]})
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-story-Q.2": human}))
        _, result = await quick_run(
            workspace, tmp_path, runner, command=checking_command(tmp_path),
            failures=failures, backoff=instant_backoff(),
        )

        assert result.status is RunStatus.COMPLETED
        assert "story Q.3" in runner.sessions and "dockerfile" in runner.sessions
        log = subjects(workspace)
        assert not any(s.startswith("foundry: story Q.2 implemented") for s in log)
        assert "foundry: story Q.2 deferred — Shortening logic" in log  # the skip is on record
        data = lock(workspace)
        assert "story-2" in data["deferred"]["quick"] and "story-2" in data["interventions"]["quick"]
        assert "foundry: story Q.2 deferred" in git(workspace, "stash", "list")
        assert quick_progress(data).remaining == ()

    async def test_an_increment_that_changes_nothing_is_a_failure_not_a_pass(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(story_steps={"Q.1": [{}]})
        with pytest.raises(CodeFailure, match="produced no changes"):
            await quick_run(workspace, tmp_path, runner, through="implementation")
        assert not any("story Q.1" in s for s in subjects(workspace))


class TestProvenance:
    async def test_every_increment_has_an_entry_in_the_lock(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        entries = lock(workspace)["implementation"]["quick"]
        assert list(entries) == ["story-1", "story-2", "story-3"]
        for number, entry in enumerate(entries.values(), start=1):
            assert entry["story_id"] == f"Q.{number}"
            assert entry["title"] == ["Data model", "Shortening logic", "Command line"][number - 1]
            assert entry["artifact_hash"].startswith("sha256:") or len(entry["artifact_hash"]) >= 64
            assert entry["tests_passed"] == number and entry["tests_counted"] is True
            assert entry["retries"] == 0 and entry["completed_at"].endswith("Z")
            assert entry["duration_seconds"] >= 0

    async def test_entries_are_appended_in_order_after_the_plan(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        sections = list(lock(workspace))
        assert sections.index("increments") < sections.index("implementation")

    async def test_pro_mode_sections_are_untouched(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await quick_run(workspace, tmp_path, QuickRunner(), through="implementation")
        implementation = lock(workspace)["implementation"]
        assert list(implementation) == ["quick"]  # no `epic-N` keys that Pro mode's resume would read


# --------------------------------------------------------------------- the Dockerfile


class TestFinalization:
    async def test_the_dockerfile_follows_the_last_increment_and_the_run_is_marked_complete(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner()
        _, result = await quick_run(workspace, tmp_path, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.sessions[-1] == "dockerfile" and runner.sessions[-2] == "story Q.3"
        log = list(reversed(subjects(workspace)))
        assert log.index(SUBJECTS[2]) < log.index(DOCKER) < next(
            i for i, s in enumerate(log) if s.startswith("foundry: run completed")
        )
        assert (workspace / "Dockerfile").read_text(encoding="utf-8") == SINGLE_STAGE
        assert "Dockerfile" in github.remote_files(REPO, "main") and DOCKER in github.remote_log(REPO, "main")

        data = lock(workspace)
        assert data["dockerfile"]["port"] == 8000 and data["dockerfile"]["stages"] == 1
        sections = list(data)
        assert sections.index("dockerfile") < sections.index("completed") == len(sections) - 1
        assert LockFile.for_workspace(workspace).status() == "completed"
        assert git(workspace, "status", "--porcelain") == ""

    async def test_the_dockerfile_is_written_from_the_spec_not_an_architecture(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner()
        await quick_run(workspace, tmp_path, runner)
        prompt = runner.dockerfile_prompts[0]
        assert "## Spec" in prompt and "Link(code, url, created_at)" in prompt
        assert "## Architecture" not in prompt and "stack the spec below" in prompt

    async def test_a_build_step_in_the_increments_gives_a_multi_stage_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        files = {"Q.1": {"src/story_Q_1.py": "ID = 'Q.1'\n", "package.json": NODE_PACKAGE}}
        runner = QuickRunner(story_steps={k: [v] for k, v in files.items()}, dockerfiles=[{"Dockerfile": MULTI_STAGE}])
        await quick_run(workspace, tmp_path, runner)
        assert "MULTI-STAGE" in runner.dockerfile_prompts[0]
        assert lock(workspace)["dockerfile"]["multi_stage"] is True

    async def test_skipped_increments_do_not_block_it(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(story_steps={"Q.3": [bad("Q.3")]})
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-story-Q.3": Answers("skip")}))
        _, result = await quick_run(
            workspace, tmp_path, runner, command=checking_command(tmp_path),
            failures=failures, backoff=instant_backoff(),
        )
        assert result.status is RunStatus.COMPLETED and DOCKER in subjects(workspace)

    async def test_it_refuses_to_run_before_every_increment_is_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(cancel_after="Q.1")
        ctx, result = await quick_run(workspace, tmp_path, runner)
        assert result.status is RunStatus.CANCELLED

        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        stage = DockerfileStage(gitops, QuickRunner(), say=lambda t: None)
        with pytest.raises(EnvFailure, match=r"increment Q\.2 is not done") as refused:
            await stage.run(ctx, ProvenanceRecorder(workspace, gitops))
        assert refused.value.fix and "cannot be resumed" in refused.value.fix

    async def test_it_refuses_to_run_before_the_increments_are_planned(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, _ = await quick_run(workspace, tmp_path, QuickRunner(), through="repo")
        gitops = quiet(GitOps.from_env(workspace, require_token=True))
        stage = DockerfileStage(gitops, QuickRunner(), say=lambda t: None)
        with pytest.raises(EnvFailure, match="have not been planned"):
            await stage.run(ctx, ProvenanceRecorder(workspace, gitops))


# ---------------------------------------------------------------- cancel, resume, status


class TestCancelResumeAndStatus:
    async def test_a_cancel_stops_between_increments_with_the_finished_ones_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = QuickRunner(cancel_after="Q.1")
        _, result = await quick_run(workspace, tmp_path, runner)

        assert result.status is RunStatus.CANCELLED
        assert lock(workspace)["cancelled"]["before"] == "increment Q.2"
        assert SUBJECTS[0] in subjects(workspace) and not any("story Q.2" in s for s in subjects(workspace))
        assert LockFile.for_workspace(workspace).status() == "cancelled"

    def test_resume_says_a_quick_run_cannot_be_resumed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        asyncio.run(quick_run(workspace, tmp_path, QuickRunner(cancel_after="Q.1")))
        result = cli_runner.invoke(cli.app, ["resume"])
        assert result.exit_code == 1
        assert "quick run" in result.output and "foundry quick" in result.output
        assert "planning cannot be resumed" not in result.output  # not the Pro mode message

    def test_status_describes_a_cancelled_quick_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        asyncio.run(quick_run(workspace, tmp_path, QuickRunner(cancel_after="Q.1")))
        result = cli_runner.invoke(cli.app, ["status"])

        assert result.exit_code == 0, result.output
        assert "Mode:        quick" in result.output
        assert "cancelled (a quick run cannot be resumed)" in result.output
        assert "implementation, next increment Q.2" in result.output
        assert "Increments:  1 of 3 committed" in result.output
        assert "Epics:" not in result.output and "Stories:" not in result.output

    def test_status_describes_a_finished_quick_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        asyncio.run(quick_run(workspace, tmp_path, QuickRunner()))
        output = cli_runner.invoke(cli.app, ["status"]).output
        assert "Status:      completed" in output and "Phase:       finished" in output
        assert "Increments:  3 of 3 committed" in output
        assert "Dockerfile:  generated (python:3.12-slim, port 8000)" in output

    def test_status_while_waiting_on_the_spec(self, workspace: Path) -> None:
        (workspace / "foundry.lock").write_text(
            '[run]\nrun_id = "r"\nidea = "i"\nmode = "quick"\nmodel = "m-1"\n'
            'started_at = "2026-10-28T10:00:00Z"\nstatus = "in_progress"\n', encoding="utf-8"
        )
        output = cli_runner.invoke(cli.app, ["status"]).output
        assert "Mode:        quick" in output and "writing the spec" in output

    def test_status_of_a_pro_run_is_unchanged(self, workspace: Path) -> None:
        (workspace / "foundry.lock").write_text(
            '[run]\nrun_id = "r"\nidea = "i"\nmodel = "m-1"\nstarted_at = "2026-10-28T10:00:00Z"\n'
            'status = "in_progress"\n', encoding="utf-8"
        )
        output = cli_runner.invoke(cli.app, ["status"]).output
        assert "Mode:" not in output and "planning" in output


# ------------------------------------------------------------------------ the command


class TestTheCommand:
    def test_foundry_quick_goes_from_an_idea_to_a_committed_dockerfile(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, make_editor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = QuickRunner(spec=make_spec(count_command(tmp_path)))
        editor = make_editor(
            "text = path.read_text(encoding='utf-8')\n"
            "path.write_text(text.replace('standard library only', 'standard library and argparse'), encoding='utf-8')"
        )
        monkeypatch.setenv("EDITOR", subprocess.list2cmdline(editor))

        def build_stages(directory: Path, private: bool, quick: bool = False):
            assert quick is True
            from foundry.repo import RepoStage

            ops = quiet(GitOps.from_env(directory, require_token=True))
            return [
                RepoStage(ops, ScriptedNamePrompt()),
                QuickImplementationStage(ops, runner, on_test_output=lambda t: None, say=lambda t: None),
                DockerfileStage(ops, runner, on_test_output=lambda t: None, say=lambda t: None),
            ], ops

        monkeypatch.setattr(cli, "_build_stages", build_stages)
        monkeypatch.setattr(cli, "_quick_phases", lambda: build_quick_pipeline(runner))

        result = cli_runner.invoke(cli.app, ["quick", "a link shortener"])

        assert result.exit_code == 0, result.output
        assert "finished" in result.output
        data = lock(workspace)
        assert data["run"]["mode"] == "quick" and "completed" in data
        assert "argparse" in github.remote_show(REPO, "main", "docs/planning/spec.md")  # your edit, via $EDITOR
        log = subjects(workspace)
        assert all(s in log for s in (*SUBJECTS, DOCKER))
        assert runner.sessions[0] == "spec" and all(not s.startswith("bmad") for s in runner.sessions)

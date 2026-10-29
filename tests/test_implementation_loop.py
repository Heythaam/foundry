"""Epic 3 end to end: the loop over epics and stories, review, cancel, failures, and a full run."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from foundry.cancel import request_cancel
from foundry.context import CodeFailure, EnvFailure, InfraFailure, RunContext
from foundry.epics import parse_epics
from foundry.gitops import GitOps
from foundry.implementation import CIFailure, ImplementationStage
from foundry.lock import LockFile
from foundry.orchestrator import Orchestrator, RunStatus
from foundry.planning import ArchitecturePhase, BrainstormPhase, EpicsPhase, PrdPhase
from foundry.preflight import PreflightPhase
from foundry.repo import RepoStage
from tests.fake_github import FakeGitHub, failing
from tests.helpers import FakeRunner, ScriptedCheckpoint
from tests.planned import (
    ARCHITECTURE,
    EPICS,
    ImplementingRunner,
    ScriptedNamePrompt,
    count_command,
    make_scaffolded,
    quiet,
)

REPO = "todo-tracker"


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def stage_for(gitops: GitOps, runner: ImplementingRunner, **kwargs) -> ImplementationStage:
    return ImplementationStage(
        quiet(gitops), runner, on_test_output=lambda t: None, say=lambda t: None, **kwargs
    )


class TestRun:
    async def test_every_epic_gets_a_branch_a_pr_and_a_merge_and_every_story_a_commit(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        runner = ImplementingRunner()
        await stage_for(gitops, runner).run(ctx, recorder)

        # Sessions: one per story, in plan order, each on its own epic's branch.
        assert runner.stories == ["1.1", "1.2", "2.1"]
        assert runner.branches == ["epic-1-core-storage"] * 2 + ["epic-2-command-line"]

        # GitHub: two PRs, both merged, both branches pushed; main has all the work.
        assert [(p["head"], p["merged"]) for p in github.pulls.values()] == [
            ("epic-1-core-storage", True),
            ("epic-2-command-line", True),
        ]
        assert set(github.remote_branches(REPO)) == {
            "main", "epic-1-core-storage", "epic-2-command-line",
        }
        files = github.remote_files(REPO, "main")
        assert {"src/story_1_1.py", "src/story_1_2.py", "src/story_2_1.py"} <= set(files)

        # Git history on main: scaffold ... epic 1 ... epic 2, ending with the merge record.
        subjects = github.remote_log(REPO, "main")
        assert subjects[0] == "foundry: epic 2 merged — PR #2"
        assert "foundry: epic 1 merged — PR #1" in subjects
        assert "foundry: story 2.1 implemented — Add Command" in subjects
        assert "foundry: story 1.1 implemented — Todo Model" in subjects
        assert subjects.index("foundry: story 1.1 implemented — Todo Model") > subjects.index(
            "foundry: epic 1 merged — PR #1"
        )

        # Lock: an entry for each story, each branch and each merge.
        lock = LockFile.for_workspace(workspace).read()
        assert set(lock["implementation"]["epic-1"]) == {"story-1", "story-2"}
        assert set(lock["implementation"]["epic-2"]) == {"story-1"}
        assert lock["epics"]["epic-1"]["merged"]["pull_request"] == 1
        assert lock["epics"]["epic-2"]["merged"]["pull_request"] == 2
        assert lock["implementation"]["epic-1"]["story-1"]["tests_passed"] == 1
        assert lock["implementation"]["epic-1"]["story-2"]["tests_passed"] == 2  # count grows

        # The tree ends clean on main, equal to the remote.
        assert git(workspace, "status", "--porcelain") == ""
        assert git(workspace, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert git(workspace, "rev-parse", "main") == git(github.bare(REPO), "rev-parse", "main")

    async def test_each_prompt_is_built_from_the_approved_documents_on_disk(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        runner = ImplementingRunner()
        await stage_for(gitops, runner).run(ctx, recorder)

        assert all(ARCHITECTURE.strip() in prompt for prompt in runner.prompts)
        assert "Todo Model" in runner.prompts[0] and "Persistence" not in runner.prompts[0]
        assert "Add Command" in runner.prompts[2]
        assert all(f"`{recorder.lock.test_runner()}`" in prompt for prompt in runner.prompts)

    async def test_a_story_sees_the_code_committed_by_earlier_stories(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        seen: list[list[str]] = []

        class Looking(ImplementingRunner):
            async def run(self, request, ctx):
                seen.append(sorted(p.name for p in (ctx.workspace_path / "src").glob("*.py"))
                            if (ctx.workspace_path / "src").exists() else [])
                return await super().run(request, ctx)

        await stage_for(gitops, Looking()).run(ctx, recorder)
        assert seen == [[], ["story_1_1.py"], ["story_1_1.py", "story_1_2.py"]]

    async def test_needs_the_test_runner_and_the_artifacts(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        (workspace / ".foundry" / "artifacts" / "epics.md").unlink()
        with pytest.raises(EnvFailure, match="epics"):
            await stage_for(gitops, ImplementingRunner()).run(ctx, recorder)


class TestReviewEpics:
    async def test_without_the_flag_no_review_checkpoint_is_opened(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        checkpoint = ScriptedCheckpoint()
        await stage_for(gitops, ImplementingRunner(), checkpoint=checkpoint).run(ctx, recorder)
        assert checkpoint.phases == []
        assert "checkpoints" not in LockFile.for_workspace(workspace).read() or not any(
            k.endswith("-review") for k in LockFile.for_workspace(workspace).read()["checkpoints"]
        )

    async def test_with_the_flag_each_epic_is_reviewed_before_its_branch_is_created(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        ctx.review_epics = True
        order: list[str] = []

        class Watching(ScriptedCheckpoint):
            async def present(self, artifact, metadata=None):
                order.append(f"review:{(metadata or {})['phase']}:branches={len(github.remote_branches(REPO))}")
                return await super().present(artifact, metadata)

        runner = ImplementingRunner()
        await stage_for(gitops, runner, checkpoint=Watching()).run(ctx, recorder)

        # Reviewed with only main on the remote for epic 1, and epic 1's branch for epic 2.
        assert order == ["review:epic-1-review:branches=1", "review:epic-2-review:branches=2"]
        lock = LockFile.for_workspace(workspace).read()
        assert {"epic-1-review", "epic-2-review"} <= set(lock["checkpoints"])
        assert runner.stories == ["1.1", "1.2", "2.1"]

    async def test_a_skipped_story_is_not_implemented_and_main_stays_in_step_with_the_remote(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        ctx.review_epics = True

        def drop_story_1_2(text: str) -> str:
            head, _, rest = text.partition("### Story 1.2")
            return head

        checkpoint = ScriptedCheckpoint({"epic-1-review": drop_story_1_2})
        runner = ImplementingRunner()
        await stage_for(gitops, runner, checkpoint=checkpoint).run(ctx, recorder)

        assert runner.stories == ["1.1", "2.1"]
        review = LockFile.for_workspace(workspace).read()["checkpoints"]["epic-1-review"]
        assert review["skipped"] == ["1.2"]
        assert "src/story_1_2.py" not in github.remote_files(REPO, "main")
        # The review commit was published, so the post-merge fast-forward worked.
        assert git(workspace, "rev-parse", "main") == git(github.bare(REPO), "rev-parse", "main")

    async def test_an_epic_whose_stories_are_all_skipped_is_left_alone(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        ctx.review_epics = True
        checkpoint = ScriptedCheckpoint({"epic-2-review": lambda t: t.partition("### Story 2.1")[0]})
        runner = ImplementingRunner()
        await stage_for(gitops, runner, checkpoint=checkpoint).run(ctx, recorder)

        assert runner.stories == ["1.1", "1.2"]
        assert "epic-2-command-line" not in github.remote_branches(REPO)
        assert len(github.pulls) == 1


class TestFailures:
    async def test_an_agent_failure_stops_the_run_after_the_stories_already_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        runner = ImplementingRunner()
        runner.errors["1.2"] = InfraFailure("agent call failed: rate limit")
        with pytest.raises(InfraFailure):
            await stage_for(gitops, runner).run(ctx, recorder)

        lock = LockFile.for_workspace(workspace).read()
        assert set(lock["implementation"]["epic-1"]) == {"story-1"}
        assert runner.stories == ["1.1", "1.2"]
        assert github.pulls == {}  # nothing is merged half-done

    async def test_red_tests_stop_the_run_and_nothing_is_committed_for_that_story(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        script = tmp_path / "red_on_1_2.py"
        script.write_text(
            "import pathlib, sys\n"
            "if pathlib.Path('src/story_1_2.py').exists():\n"
            "    print('FAILED test_persistence - AssertionError')\n"
            "    sys.exit(1)\n"
            "print('1 passed')\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(script)])
        ctx, recorder, gitops = await make_scaffolded(workspace, command)

        with pytest.raises(CodeFailure) as info:
            await stage_for(gitops, ImplementingRunner()).run(ctx, recorder)

        assert "story 1.2" in info.value.message and "AssertionError" in info.value.message
        lock = LockFile.for_workspace(workspace).read()
        assert set(lock["implementation"]["epic-1"]) == {"story-1"}
        assert git(workspace, "log", "-1", "--format=%s") == (
            "foundry: story 1.1 implemented — Todo Model"
        )
        assert github.pulls == {}

    async def test_a_dirty_tree_before_a_story_is_refused(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        stage = stage_for(gitops, ImplementingRunner())
        epic = parse_epics(EPICS)[0]
        await stage.start_epic(epic, ctx, recorder)
        (workspace / "leftover.txt").write_text("x\n", encoding="utf-8")

        with pytest.raises(EnvFailure, match="leftover.txt"):
            await stage.implement_story(epic.stories[0], "arch", "cmd", ctx, recorder)

    async def test_red_ci_stops_the_run_with_the_pr_left_open(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        ctx, recorder, gitops = await make_scaffolded(workspace, count_command(tmp_path))
        github.ci = [[failing("test")]]
        runner = ImplementingRunner()
        with pytest.raises(CIFailure):
            await stage_for(gitops, runner).run(ctx, recorder)

        assert runner.stories == ["1.1", "1.2"]  # epic 2 never started
        assert github.pulls[1]["merged"] is False


# ------------------------------------------------------------------ through the orchestrator

PRD = "# Todo Tracker PRD\n\nUsers log in; errors are retried; data is persisted; events are logged.\n"
CLEAN = '{"contradictions": [], "traceability_gaps": []}'


def planning(runner: FakeRunner) -> list:
    return [
        BrainstormPhase(runner),
        PrdPhase(runner),
        PreflightPhase(runner),
        ArchitecturePhase(runner),
        EpicsPhase(runner),
    ]


async def whole_run(
    workspace: Path,
    tmp_path: Path,
    runner: ImplementingRunner,
    command: str | None = None,
    **kwargs,
):
    test_command = command or count_command(tmp_path)
    architecture = f"# Architecture\n\n## Test Runner\n\n```bash\n{test_command}\n```\n"
    planner = FakeRunner("# Brainstorm\n\nIdeas.\n", PRD, CLEAN, architecture, EPICS)
    gitops = quiet(GitOps.from_env(workspace, require_token=True))
    stages = [
        RepoStage(gitops, ScriptedNamePrompt()),
        ImplementationStage(
            gitops, runner, on_test_output=lambda t: None, say=lambda t: None, **kwargs
        ),
    ]
    ctx = RunContext(idea="a todo tracker for the command line", workspace_path=workspace)
    orchestrator = Orchestrator(
        planning(planner), checkpoint=ScriptedCheckpoint(), stages=stages, gitops=gitops
    )
    return ctx, await orchestrator.run(ctx)


class TestThroughTheOrchestrator:
    async def test_idea_to_merged_epics_in_one_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        ctx, result = await whole_run(workspace, tmp_path, runner)

        assert result.status is RunStatus.COMPLETED
        assert runner.stories == ["1.1", "1.2", "2.1"]
        lock = tomllib.loads((workspace / "foundry.lock").read_text(encoding="utf-8"))
        assert LockFile.for_workspace(workspace).status() == "completed"
        # [completed] is the very last thing recorded: after both epics merged, not after planning.
        sections = list(lock)
        assert sections.index("completed") > sections.index("epics")
        assert "merged" in lock["epics"]["epic-2"]
        assert git(workspace, "log", "-1", "--format=%s").startswith("foundry: run completed")
        # The remote main reaches the same place, with the planning docs the scaffold carried.
        files = github.remote_files(REPO, "main")
        assert "docs/planning/architecture.md" in files and "src/story_2_1.py" in files

    async def test_cancel_between_stories_stops_cleanly_with_work_committed(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        class CancelsDuringStory(ImplementingRunner):
            async def run(self, request, ctx):
                result = await super().run(request, ctx)
                if request.name == "story 1.1":
                    request_cancel(ctx.workspace_path)
                return result

        runner = CancelsDuringStory()
        ctx, result = await whole_run(workspace, tmp_path, runner)

        assert result.status is RunStatus.CANCELLED
        assert runner.stories == ["1.1"]
        lock = LockFile.for_workspace(workspace)
        assert lock.status() == "cancelled"
        assert lock.read()["cancelled"]["before"] == "story 1.2"
        assert set(lock.read()["implementation"]["epic-1"]) == {"story-1"}
        assert git(workspace, "status", "--porcelain") == ""  # nothing half-done on disk
        assert github.pulls == {}

    async def test_a_failure_leaves_the_run_in_progress_for_resume(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = ImplementingRunner()
        runner.errors["1.1"] = CodeFailure("agent returned no output")
        with pytest.raises(CodeFailure):
            await whole_run(workspace, tmp_path, runner)
        assert LockFile.for_workspace(workspace).status() == "in_progress"

"""Story 4.4: persistent failures are escalated to the human in $EDITOR."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.checkpoint import EditorCheckpoint
from foundry.context import CodeFailure, InfraFailure
from foundry.failures import (
    EPIC_ACTIONS,
    STORY_ACTIONS,
    EscalationCancelled,
    RetryingFailureHandler,
    parse_decision,
    render_escalation,
)
from foundry.lock import LockFile
from foundry.orchestrator import Orchestrator, RunStatus
from tests.fake_github import FakeGitHub, failing, passing
from tests.helpers import ScriptedCheckpoint
from tests.planned import (
    Answers,
    SequenceRunner,
    bad,
    good,
    instant_backoff,
    set_action,
    started_epic,
    with_story_text,
    writing,
)


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def lock(workspace: Path) -> dict:
    return LockFile.for_workspace(workspace).read()


def handler(human: Answers, story: str = "1.1") -> RetryingFailureHandler:
    return RetryingFailureHandler(ScriptedCheckpoint({f"escalation-story-{story}": human}))


async def stuck_story(workspace, tmp_path, human: Answers, runner: SequenceRunner | None = None, **kw):
    """Story 1.1 whose sessions all fail: the retry fails too, so the human is asked."""
    runner = runner or SequenceRunner({"1.1": [bad("1.1")]})
    ctx, recorder, gitops, stage, epic, branch = await started_epic(
        workspace, tmp_path, runner=runner, failures=handler(human), **kw
    )
    return ctx, recorder, gitops, stage, epic, branch, runner


async def run_story(stage, epic, recorder, ctx, index: int = 0):
    return await stage.implement_story(
        epic.stories[index], "arch", recorder.lock.test_runner(), ctx, recorder
    )


class TestTheDocument:
    def make(self, **kwargs) -> str:
        defaults = dict(
            label="story 1.1: Todo Model",
            what_happened="It failed twice.",
            output="FAILED story_1_1.py\n1 failed",
            diff="diff --git a/src/x.py b/src/x.py\n+ID = 1",
            story_text="### Story 1.1: Todo Model\n\nAs a user...",
            allowed=STORY_ACTIONS,
        )
        return render_escalation(**{**defaults, **kwargs})

    def test_contains_the_story_the_error_the_test_output_and_the_diff(self) -> None:
        text = self.make()
        assert "As a user..." in text  # the story text
        assert "It failed twice." in text  # the error
        assert "FAILED story_1_1.py" in text  # the failing test output
        assert "+ID = 1" in text  # the current codebase diff

    def test_lists_the_four_choices(self) -> None:
        text = self.make()
        for action in ("retry", "continue", "skip", "cancel"):
            assert f"- `{action}`:" in text
        assert text.rstrip().endswith("action:")

    def test_epic_level_escalations_offer_only_retry_and_cancel(self) -> None:
        text = self.make(story_text=None, allowed=EPIC_ACTIONS)
        assert "- `retry`:" in text and "- `cancel`:" in text
        assert "`skip`" not in text and "`continue`" not in text
        assert "## Story" not in text

    def test_output_containing_code_fences_cannot_break_the_layout(self) -> None:
        text = self.make(output="before\n```python\ncode\n```\nafter")
        assert "````" in text  # the surrounding fence is longer than the one inside
        assert parse_decision(set_action(text, "skip"), STORY_ACTIONS)[0] == "skip"

    def test_a_huge_diff_is_truncated(self) -> None:
        text = self.make(diff="\n".join(f"+line {n}" for n in range(5000)))
        assert "+line 0" in text and "+line 4999" not in text
        assert "more lines" in text


class TestParsingTheDecision:
    @pytest.mark.parametrize("action", ["retry", "continue", "skip", "cancel"])
    def test_each_action_is_understood(self, action: str) -> None:
        text = render_escalation(
            label="x", what_happened="x", output="", diff="", story_text="s", allowed=STORY_ACTIONS
        )
        assert parse_decision(set_action(text, action), STORY_ACTIONS) == (action, "s", None)

    def test_case_and_spacing_do_not_matter(self) -> None:
        assert parse_decision("## Decision\n\naction:    SKIP  \n", STORY_ACTIONS)[0] == "skip"

    def test_no_choice_means_cancel(self) -> None:
        assert parse_decision("## Decision\n\naction: \n", STORY_ACTIONS)[0] == "cancel"

    def test_an_unknown_word_is_a_problem_not_a_guess(self) -> None:
        action, _, problem = parse_decision("## Decision\n\naction: retyr\n", STORY_ACTIONS)
        assert action is None and "`retyr` is not one of" in problem

    def test_an_action_line_inside_the_test_output_is_not_mistaken_for_the_answer(self) -> None:
        text = "output: \naction: skip\n\n## Decision\n\naction: retry\n"
        assert parse_decision(text, STORY_ACTIONS)[0] == "retry"

    def test_an_action_not_offered_is_a_problem(self) -> None:
        assert parse_decision("## Decision\naction: skip\n", EPIC_ACTIONS)[0] is None


class TestOpeningTheEditor:
    async def test_the_real_editor_checkpoint_shows_the_file_and_reads_the_answer_back(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, make_editor
    ) -> None:
        seen = tmp_path / "seen.md"
        editor = make_editor(
            f"""
            import re
            text = path.read_text(encoding="utf-8")
            pathlib.Path({str(seen)!r}).write_text(text, encoding="utf-8")
            path.write_text(re.sub(r"^action:.*$", "action: skip", text, flags=re.M), encoding="utf-8")
            """
        )
        failures = RetryingFailureHandler(EditorCheckpoint(editor))
        runner = SequenceRunner({"1.1": [bad("1.1")]})
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )

        assert not await run_story(stage, epic, recorder, ctx)

        shown = seen.read_text(encoding="utf-8")
        assert shown.startswith("# Foundry needs you: story 1.1: Todo Model")
        assert "FAILED story_1_1.py: assertion error" in shown
        assert "src/story_1_1.py" in shown  # the diff
        assert "As a user, I want todos, So that I can track work." in shown

    async def test_an_unknown_word_reopens_the_file_with_the_problem_first(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("maybe", "skip")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        assert not await run_story(stage, epic, recorder, ctx)

        assert len(human.documents) == 2
        assert "`maybe` is not one of: retry, continue, skip, cancel" in human.documents[1]
        assert "maybe" not in human.documents[0]

    async def test_three_unknown_answers_cancel_the_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("what", "huh", "eh")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        with pytest.raises(EscalationCancelled):
            await run_story(stage, epic, recorder, ctx)
        assert len(human.documents) == 3

    async def test_leaving_the_action_empty_cancels(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers(lambda document: document)  # saved without choosing
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        with pytest.raises(EscalationCancelled):
            await run_story(stage, epic, recorder, ctx)


class TestRetryAction:
    async def test_the_edited_story_is_what_the_new_session_implements(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers(
            with_story_text("retry", "I want todos saved", "I want todos saved to a JSON file")
        )
        runner = SequenceRunner({"1.2": [bad("1.2"), bad("1.2"), good("1.2")]})
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=handler(human, "1.2")
        )
        await run_story(stage, epic, recorder, ctx, 0)
        assert await run_story(stage, epic, recorder, ctx, 1)

        prompts = runner.prompts_for("1.2")
        assert len(prompts) == 3  # session, automatic retry, the human's retry
        assert "saved to a JSON file" not in prompts[0] + prompts[1]
        assert "saved to a JSON file" in prompts[2]
        assert "The previous attempt failed" in prompts[2]
        section = lock(workspace)["interventions"]["epic-1"]["story-2"]
        assert section["actions"] == ["retry"] and section["story_edited"] is True
        assert section["story_hash"].startswith("sha256:")
        assert section["code_edited"] is False
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.2 implemented — Persistence"

    async def test_retrying_without_changes_is_a_plain_new_session(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1"), bad("1.1"), good("1.1")]})
        human = Answers("retry")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human, runner)
        assert await run_story(stage, epic, recorder, ctx)

        assert len(runner.prompts_for("1.1")) == 3
        assert lock(workspace)["interventions"]["epic-1"]["story-1"]["story_edited"] is False
        assert "story_hash" not in lock(workspace)["interventions"]["epic-1"]["story-1"]

    async def test_a_retry_that_fails_again_asks_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [bad("1.1")] * 3 + [good("1.1")]})
        human = Answers("retry", "retry")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human, runner)
        assert await run_story(stage, epic, recorder, ctx)

        assert len(human.documents) == 2
        assert "still fail after your retry" in human.documents[1]
        section = lock(workspace)["interventions"]["epic-1"]["story-1"]
        assert section["actions"] == ["retry", "retry"] and section["rounds"] == 2
        assert section["final_action"] == "retry"


class TestContinueAction:
    async def test_the_humans_code_is_tested_and_committed_as_a_human_intervention(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        fix = writing(good("1.1"), "continue", workspace)
        human = Answers(fix)
        ctx, recorder, _, stage, epic, _, runner = await stuck_story(workspace, tmp_path, human)
        assert await run_story(stage, epic, recorder, ctx)

        assert len(runner.sessions) == 2  # no further session: the human did the work
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.1 — human intervention"
        assert "BROKEN" not in git(workspace, "show", "HEAD:src/story_1_1.py")
        section = lock(workspace)["interventions"]["epic-1"]["story-1"]
        assert section["actions"] == ["continue"] and section["code_edited"] is True
        assert "implementation" in lock(workspace)
        files = git(workspace, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert "foundry.lock" in files and "src/story_1_1.py" in files

    async def test_the_diff_the_human_sees_is_the_failing_attempt(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("skip")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        await run_story(stage, epic, recorder, ctx)
        assert "+ID = '1.1'  # BROKEN" in human.documents[0]

    async def test_still_red_after_continuing_asks_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("continue", writing(good("1.1"), "continue", workspace))
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        assert await run_story(stage, epic, recorder, ctx)

        assert len(human.documents) == 2
        assert "still fail after your continue" in human.documents[1]
        assert lock(workspace)["interventions"]["epic-1"]["story-1"]["actions"] == ["continue", "continue"]


class TestSkipAction:
    async def test_the_story_is_deferred_and_its_work_is_kept_in_a_stash(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("skip")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)
        before = git(workspace, "rev-parse", "HEAD")

        assert not await run_story(stage, epic, recorder, ctx)

        assert git(workspace, "status", "--porcelain") == ""  # the tree is clean for the next story
        assert "foundry: story 1.1 deferred" in git(workspace, "stash", "list")
        assert "BROKEN" in git(workspace, "show", "stash@{0}:src/story_1_1.py")
        data = lock(workspace)
        assert "implementation" not in data  # not recorded as implemented
        assert data["deferred"]["epic-1"]["story-1"]["story_id"] == "1.1"
        assert data["interventions"]["epic-1"]["story-1"]["final_action"] == "skip"
        assert data["failures"]["epic-1"]["story-1"]["retry_outcome"] == "failed"
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.1 deferred — Todo Model"
        assert git(workspace, "rev-list", "--count", f"{before}..HEAD") == "1"  # lock only

    async def test_the_epic_carries_on_and_the_pr_lists_what_was_deferred(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("skip")
        ctx, recorder, _, stage, epic, branch, _ = await stuck_story(workspace, tmp_path, human)
        committed = [await run_story(stage, epic, recorder, ctx, index) for index in (0, 1)]
        assert committed == [False, True]

        github.ci = [[passing("test")]]
        await stage.finish_epic(epic, branch, ctx, recorder, deferred=[epic.stories[0]])

        assert github.pulls[1]["merged"] is True
        assert "Deferred by a human" in github.pulls[1]["body"]
        assert "Story 1.1: Todo Model" in github.pulls[1]["body"].split("Deferred by a human")[1]

    async def test_an_epic_with_every_story_deferred_still_merges_its_lock_entries(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        from foundry.implementation import ImplementationStage
        from tests.planned import checking_command, make_scaffolded, quiet

        runner = SequenceRunner({"1.1": [bad("1.1")], "1.2": [bad("1.2")]})
        failures = RetryingFailureHandler(
            ScriptedCheckpoint(
                {"escalation-story-1.1": Answers("skip"), "escalation-story-1.2": Answers("skip")}
            )
        )
        ctx, recorder, gitops = await make_scaffolded(workspace, checking_command(tmp_path))
        stage = ImplementationStage(
            quiet(gitops), runner, failures=failures,
            on_test_output=lambda t: None, say=lambda t: None,
        )

        await stage.run(ctx, recorder)

        assert [pull["head"] for pull in github.pulls.values()] == [
            "epic-1-core-storage",
            "epic-2-command-line",
        ]
        data = lock(workspace)
        assert "merged" in data["epics"]["epic-1"] and "merged" in data["epics"]["epic-2"]
        assert set(data["deferred"]["epic-1"]) == {"story-1", "story-2"}  # kept, not lost
        assert git(workspace, "branch", "--show-current") == "main"
        assert "Deferred by a human" in github.pulls[1]["body"]


class TestCancelAction:
    async def test_cancel_records_the_intervention_and_leaves_the_tree_alone(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("cancel")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(workspace, tmp_path, human)

        with pytest.raises(EscalationCancelled) as stop:
            await run_story(stage, epic, recorder, ctx)

        assert "story 1.1" in stop.value.before
        data = lock(workspace)
        assert data["interventions"]["epic-1"]["story-1"]["final_action"] == "cancel"
        assert data["failures"]["epic-1"]["story-1"]["retry_outcome"] == "failed"
        assert "BROKEN" in (workspace / "src" / "story_1_1.py").read_text(encoding="utf-8")
        assert git(workspace, "status", "--porcelain") != ""  # the human's evidence is untouched

    async def test_through_the_orchestrator_the_run_ends_as_cancelled(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        from tests.test_implementation_loop import whole_run

        runner = SequenceRunner({"1.1": [bad("1.1")]})
        failures = RetryingFailureHandler(
            ScriptedCheckpoint({"escalation-story-1.1": Answers("cancel")})
        )
        # the planned command must fail on BROKEN, so build the run with the checking command
        from tests.planned import checking_command

        ctx, result = await whole_run(
            workspace, tmp_path, runner, failures=failures, command=checking_command(tmp_path)
        )

        assert result.status is RunStatus.CANCELLED
        assert LockFile.for_workspace(workspace).status() == "cancelled"
        assert "story 1.1" in lock(workspace)["cancelled"]["before"]
        assert lock(workspace)["interventions"]["epic-1"]["story-1"]["final_action"] == "cancel"


class TestInfrastructureEscalation:
    async def test_story_level_exhaustion_offers_all_four_actions(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [InfraFailure("down"), InfraFailure("down"), good("1.1")]})
        human = Answers("retry")
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(
            workspace, tmp_path, human, runner, backoff=instant_backoff(max_retries=1)
        )
        assert await run_story(stage, epic, recorder, ctx)

        assert all(f"- `{name}`:" in human.documents[0] for name in STORY_ACTIONS)
        failure = lock(workspace)["failures"]["epic-1"]["story-1"]
        assert failure["category"] == "infrastructure" and failure["retry_outcome"] == "exhausted"

    async def test_a_session_that_never_ran_has_no_failing_output_to_pass_on(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [InfraFailure("down"), InfraFailure("down"), good("1.1")]})
        ctx, recorder, _, stage, epic, _, _ = await stuck_story(
            workspace, tmp_path, Answers("retry"), runner, backoff=instant_backoff(max_retries=1)
        )
        await run_story(stage, epic, recorder, ctx)
        assert "The previous attempt failed" not in runner.prompts_for("1.1")[-1]


class TestRedCi:
    async def finished(self, workspace: Path, tmp_path: Path, human: Answers):
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-epic-1-ci": human}))
        ctx, recorder, gitops, stage, epic, branch = await started_epic(
            workspace, tmp_path, runner=SequenceRunner(), failures=failures
        )
        for story in epic.stories:
            await stage.implement_story(story, "arch", recorder.lock.test_runner(), ctx, recorder)
        return ctx, recorder, gitops, stage, epic, branch

    async def test_the_human_sees_the_failing_checks_and_the_pr(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("cancel")
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, human)
        github.ci = [[failing("unit-tests")]]

        with pytest.raises(EscalationCancelled):
            await stage.finish_epic(epic, branch, ctx, recorder)

        assert "unit-tests: failure" in human.documents[0]
        assert "github.com/" in human.documents[0] and "/pull/1" in human.documents[0]
        assert github.pulls[1]["merged"] is False  # left open for the human

    async def test_retry_checks_ci_again_and_merges_when_it_is_green(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("retry")
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, human)
        github.ci = [[failing("unit-tests")], [passing("unit-tests")]]

        await stage.finish_epic(epic, branch, ctx, recorder)

        assert len(human.documents) == 1
        assert github.pulls[1]["merged"] is True
        assert lock(workspace)["epics"]["epic-1"]["merged"]["pull_request"] == 1

    async def test_retry_follows_a_fix_pushed_to_the_branch(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("retry")
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, human)
        github.ci = [[failing("unit-tests")], [passing("unit-tests")]]
        original = github.pulls  # the head moves when the human pushes
        new_head = "f" * 40

        def human_pushes(document: str) -> str:
            original[1]["sha"] = new_head
            return set_action(document, "retry")

        human.answers = [human_pushes]
        checked: list[str] = []
        real = stage.git.poll_ci

        async def spy(sha: str):
            checked.append(sha)
            return await real(sha)

        stage.git.poll_ci = spy  # type: ignore[method-assign]
        await stage.finish_epic(epic, branch, ctx, recorder)
        assert checked[-1] == new_head and checked[0] != new_head
        assert lock(workspace)["epics"]["epic-1"]["merged"]["head_sha"] == new_head

    async def test_still_red_after_retry_asks_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("retry", "cancel")
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, human)
        github.ci = [[failing("unit-tests")]]

        with pytest.raises(EscalationCancelled):
            await stage.finish_epic(epic, branch, ctx, recorder)
        assert len(human.documents) == 2


class TestRoundLimit:
    async def test_exhausting_the_rounds_stops_the_run_with_a_code_failure(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        failures = RetryingFailureHandler(
            ScriptedCheckpoint({"escalation-story-1.1": Answers("retry")}), max_rounds=2
        )
        runner = SequenceRunner({"1.1": [bad("1.1")]})
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures
        )
        with pytest.raises(CodeFailure, match="still fails after 2 escalations"):
            await run_story(stage, epic, recorder, ctx)

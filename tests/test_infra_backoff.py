"""Story 4.3: infrastructure failures are retried with exponential backoff and jitter."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

import pytest

from foundry.context import CodeFailure, EnvFailure, InfraFailure
from foundry.failures import (
    DEFAULT_MAX_RETRIES,
    BackoffPolicy,
    EscalationCancelled,
    RetriesExhausted,
    RetryingFailureHandler,
    with_backoff,
)
from foundry.lock import LockFile
from tests.fake_github import FakeGitHub, passing
from tests.helpers import ScriptedCheckpoint
from tests.planned import Answers, SequenceRunner, good, instant_backoff, started_epic


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


class TestPolicy:
    def test_the_default_is_five_retries(self) -> None:
        assert BackoffPolicy().max_retries == DEFAULT_MAX_RETRIES == 5

    def test_waits_double_each_time(self) -> None:
        policy = BackoffPolicy(base_delay=2.0, factor=2.0, max_delay=1000, rng=lambda: 1.0)
        assert [policy.delay(n) for n in range(1, 6)] == [2, 4, 8, 16, 32]

    def test_waits_are_capped(self) -> None:
        policy = BackoffPolicy(base_delay=2.0, max_delay=10, rng=lambda: 1.0)
        assert policy.delay(10) == 10

    def test_jitter_scales_each_wait_between_half_and_all_of_it(self) -> None:
        policy = BackoffPolicy(base_delay=8.0, rng=random.Random(7).random)
        samples = [policy.delay(1) for _ in range(500)]
        assert all(4.0 <= value <= 8.0 for value in samples)
        assert len({round(value, 3) for value in samples}) > 100  # actually random
        assert BackoffPolicy(base_delay=8.0, rng=lambda: 0.0).delay(1) == 4.0

    def test_max_retries_comes_from_the_environment(self) -> None:
        assert BackoffPolicy.from_env({}).max_retries == 5
        assert BackoffPolicy.from_env({"FOUNDRY_MAX_RETRIES": "2"}).max_retries == 2
        assert BackoffPolicy.from_env({"FOUNDRY_MAX_RETRIES": "0"}).max_retries == 0

    @pytest.mark.parametrize("raw", ["many", "-1", "2.5"])
    def test_a_bad_setting_is_an_environment_failure_that_says_what_to_do(self, raw: str) -> None:
        with pytest.raises(EnvFailure) as raised:
            BackoffPolicy.from_env({"FOUNDRY_MAX_RETRIES": raw})
        assert "FOUNDRY_MAX_RETRIES" in raised.value.message and raised.value.fix


class TestWithBackoff:
    async def test_retries_infrastructure_failures_until_one_succeeds(self) -> None:
        policy = instant_backoff()
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 4:
                raise InfraFailure("rate limited")
            return "ok"

        assert await with_backoff("call", flaky, policy) == "ok"
        assert calls == 4
        assert policy.waits == [2.0, 4.0, 8.0]  # type: ignore[attr-defined]

    async def test_raw_timeouts_count_as_infrastructure(self) -> None:
        policy = instant_backoff()
        calls = 0

        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("slow")
            return "ok"

        assert await with_backoff("call", flaky, policy) == "ok"

    @pytest.mark.parametrize("error", [CodeFailure("wrong"), EnvFailure("broken"), ValueError("?")])
    async def test_other_failures_are_never_retried(self, error: Exception) -> None:
        policy = instant_backoff()
        calls = 0

        async def failing() -> None:
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(Exception) as raised:
            await with_backoff("call", failing, policy)
        assert calls == 1 and policy.waits == []  # type: ignore[attr-defined]
        assert not isinstance(raised.value, RetriesExhausted)

    async def test_gives_up_after_max_retries(self) -> None:
        policy = instant_backoff(max_retries=3)
        calls = 0

        async def always() -> None:
            nonlocal calls
            calls += 1
            raise InfraFailure("down")

        with pytest.raises(RetriesExhausted) as raised:
            await with_backoff("fetch", always, policy)
        assert calls == 4  # the first try and three retries
        assert raised.value.attempts == 3
        assert "fetch still failing after 3 retries" in raised.value.message
        assert isinstance(raised.value, InfraFailure)

    async def test_no_retries_means_the_original_error(self) -> None:
        async def always() -> None:
            raise InfraFailure("down")

        with pytest.raises(InfraFailure, match="^down$"):
            await with_backoff("fetch", always, BackoffPolicy.none())

    async def test_each_wait_is_announced(self) -> None:
        said: list[str] = []
        calls = 0

        async def flaky() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InfraFailure("HTTP 502")

        await with_backoff("push", flaky, instant_backoff(), said.append)
        assert said == ["  push failed (HTTP 502); retry 1/5 in 2.0s"]


class TestSessionBackoff:
    async def test_a_session_that_hits_a_rate_limit_is_run_again_unchanged(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner(
            {"1.1": [InfraFailure("agent call failed (HTTP 429): slow down"), InfraFailure("overloaded"), good("1.1")]}
        )
        backoff = instant_backoff()
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, backoff=backoff
        )
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        prompts = runner.prompts_for("1.1")
        assert len(prompts) == 3 and len(set(prompts)) == 1  # the very same prompt each time
        assert "429" not in prompts[0] and "overloaded" not in prompts[0]  # never shown to the LLM
        assert backoff.waits == [2.0, 4.0]  # type: ignore[attr-defined]
        assert git(workspace, "log", "-1", "--format=%s") == "foundry: story 1.1 implemented — Todo Model"

    async def test_nothing_is_written_while_waiting(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [InfraFailure("down"), InfraFailure("down"), good("1.1")]})
        snapshots: list[tuple[bytes, str, str]] = []

        def state() -> tuple[bytes, str, str]:
            return (
                (workspace / "foundry.lock").read_bytes(),
                git(workspace, "rev-parse", "HEAD"),
                git(workspace, "status", "--porcelain"),
            )

        backoff = instant_backoff()
        real_sleep = backoff.sleep

        async def watching(seconds: float) -> None:
            snapshots.append(state())
            await real_sleep(seconds)

        backoff.sleep = watching
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, backoff=backoff
        )
        before = state()
        await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)

        assert len(snapshots) == 2
        assert all(snapshot == before for snapshot in snapshots)  # lock, HEAD and tree untouched

    async def test_exhausted_retries_with_the_default_handler_stop_the_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [InfraFailure("down")]})
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, backoff=instant_backoff(max_retries=2)
        )
        with pytest.raises(RetriesExhausted, match="after 2 retries"):
            await stage.implement_story(epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder)
        assert len(runner.sessions) == 3

    async def test_exhausted_retries_are_escalated_to_the_human(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        runner = SequenceRunner({"1.1": [InfraFailure("API down")]})
        human = Answers("skip")
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-story-1.1": human}))
        ctx, recorder, _, stage, epic, _ = await started_epic(
            workspace, tmp_path, runner=runner, failures=failures, backoff=instant_backoff(max_retries=2)
        )
        assert not await stage.implement_story(
            epic.stories[0], "arch", recorder.lock.test_runner(), ctx, recorder
        )

        assert len(human.documents) == 1
        assert "retried 2 times" in human.documents[0] and "API down" in human.documents[0]
        section = LockFile.for_workspace(workspace).read()["failures"]["epic-1"]["story-1"]
        assert section["category"] == "infrastructure"
        assert section["retry_outcome"] == "exhausted"
        assert section["retries"] == 2


class TestGitHubCalls:
    async def finished(self, workspace: Path, tmp_path: Path, **kwargs):
        ctx, recorder, gitops, stage, epic, branch = await started_epic(
            workspace, tmp_path, runner=SequenceRunner(), **kwargs
        )
        command = recorder.lock.test_runner()
        for story in epic.stories:
            await stage.implement_story(story, "arch", command, ctx, recorder)
        return ctx, recorder, gitops, stage, epic, branch

    async def test_a_transient_github_error_opening_the_pr_is_retried(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        backoff = instant_backoff()
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, backoff=backoff)
        github.fail("/pulls", 503, "unavailable")
        github.ci = [[passing("test")]]

        await stage.finish_epic(epic, branch, ctx, recorder)

        assert backoff.waits == [2.0]  # type: ignore[attr-defined]
        assert len(github.pulls) == 1 and github.pulls[1]["merged"] is True

    async def test_a_rate_limit_while_checking_ci_is_retried(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        backoff = instant_backoff()
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, backoff=backoff)
        github.fail("/check-runs", 403, "API rate limit exceeded")
        github.ci = [[passing("test")]]

        await stage.finish_epic(epic, branch, ctx, recorder)
        assert backoff.waits == [2.0] and github.pulls[1]["merged"] is True  # type: ignore[attr-defined]

    async def test_a_transient_error_merging_is_retried(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        backoff = instant_backoff()
        ctx, recorder, _, stage, epic, branch = await self.finished(workspace, tmp_path, backoff=backoff)
        github.fail("/merge", 502, "bad gateway")
        github.ci = [[passing("test")]]

        await stage.finish_epic(epic, branch, ctx, recorder)
        assert backoff.waits == [2.0] and github.pulls[1]["merged"] is True  # type: ignore[attr-defined]

    async def test_a_retried_pr_is_not_opened_twice(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        _, _, gitops, _, epic, branch = await self.finished(workspace, tmp_path)
        first = await gitops.create_pr(branch, "main", "Epic 1", "body")
        again = await gitops.create_pr(branch, "main", "Epic 1", "body")  # e.g. the reply was lost
        assert again.number == first.number and len(github.pulls) == 1

    async def test_exhausted_retries_ask_the_human_who_can_try_again(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        human = Answers("retry")
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-epic-1": human}))
        ctx, recorder, _, stage, epic, branch = await self.finished(
            workspace, tmp_path, failures=failures, backoff=instant_backoff(max_retries=2)
        )
        for _ in range(3):  # the first try and both retries fail
            github.fail("/pulls", 503, "unavailable")
        github.ci = [[passing("test")]]

        await stage.finish_epic(epic, branch, ctx, recorder)

        assert len(human.documents) == 1
        assert "retried 2 times" in human.documents[0]
        assert "`skip`" not in human.documents[0]  # skipping is meaningless for an epic-level call
        assert github.pulls[1]["merged"] is True

    async def test_cancelling_at_the_escalation_stops_the_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        failures = RetryingFailureHandler(ScriptedCheckpoint({"escalation-epic-1": Answers("cancel")}))
        ctx, recorder, _, stage, epic, branch = await self.finished(
            workspace, tmp_path, failures=failures, backoff=instant_backoff(max_retries=1)
        )
        for _ in range(2):
            github.fail("/pulls", 503, "unavailable")

        with pytest.raises(EscalationCancelled):
            await stage.finish_epic(epic, branch, ctx, recorder)
        assert github.pulls == {}

"""The implementation loop: one feature branch per epic, one Claude Code session per story.

Order inside an epic: branch from `main` and push it (FR-16), then for each story write the
prompt, run a fresh `claude -p` subprocess (AD-7), run the recorded test command (FR-18),
commit on green (FR-19); when the epic is done open a PR, wait for CI and merge (FR-20).
Every git/GitHub mutation is a GitOps call (AD-8) and every action is recorded in the lock
and in git together (AD-2).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from foundry.agent import AgentRequest, AgentResult, AgentRunner, ClaudeCodeRunner, print_to_terminal
from foundry.artifacts import read_artifact
from foundry.cancel import is_cancel_requested
from foundry.checkpoint import CheckpointHandler, EditorCheckpoint
from foundry.context import CodeFailure, EnvFailure, RunContext
from foundry.epics import Epic, Story, validate_plan
from foundry.gitops import MAIN_BRANCH, CIResult, GitOps, PullRequestInfo
from foundry.provenance import ProvenanceRecorder
from foundry.review import EpicReviewer, ProjectState
from foundry.scaffold import slugify
from foundry.stages import CancelRequested
from foundry.testgate import DEFAULT_TIMEOUT_SECONDS, GateResult, run_test_command

_BRANCH_SLUG_LENGTH = 40

# Implementation sessions edit the repository, so unlike the planning agents they get the
# file-editing and shell tools. Nothing else: no web access, no sub-agents.
IMPLEMENTATION_TOOLS: Sequence[str] = (
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Glob",
    "Grep",
    "Bash",
    "Skill",
)


def epic_branch_name(epic: Epic) -> str:
    """`epic-{N}-{slug}`, e.g. `epic-2-planning-pipeline`."""
    return f"epic-{epic.number}-{slugify(epic.title, _BRANCH_SLUG_LENGTH)}"


def build_story_prompt(story: Story, architecture: str, test_command: str) -> str:
    """The whole task in one file (AD-7): the story as written, then the architecture inlined.

    The agent should never need to locate a document to understand what it is building.
    """
    return (
        f"# Task: implement story {story.id}: {story.title}\n\n"
        "You are running non-interactively inside Foundry: no one can answer questions. Where "
        "you would ask, choose the most reasonable answer and note it in your final message.\n\n"
        "Implement the story below in the repository you are running in (the current directory). "
        "Earlier stories are already committed; read the code on disk to see what exists.\n\n"
        "Rules:\n"
        "- Write tests that cover every acceptance criterion, and make them pass.\n"
        f"- The project's test command is `{test_command}`; it must exit 0 when you finish.\n"
        "- Do not run `git commit`, `git push` or change branches: Foundry commits for you.\n"
        "- Do not edit `foundry.lock` or anything under `.foundry/`.\n"
        "- Do only this story; do not start on later ones.\n"
        "- Your final message is a short summary of what you changed.\n\n"
        "## Story\n\n"
        f"{story.raw.strip()}\n\n"
        "## Architecture\n\n"
        f"{architecture.strip()}\n"
    )


class CIFailure(CodeFailure):
    """CI on an epic's pull request finished red."""


@dataclass(frozen=True)
class Recovery:
    """A failure handler's answer: a passing gate result, and how many retries it took."""

    result: GateResult
    retries: int = 0


@runtime_checkable
class FailureHandler(Protocol):
    """Where Epic 4 plugs in (auto-retry, backoff, escalation). Raise to stop the run."""

    async def on_test_failure(
        self, story: Story, result: GateResult, ctx: RunContext
    ) -> Recovery: ...

    async def on_ci_failure(
        self, epic: Epic, ci: CIResult, pr: PullRequestInfo, ctx: RunContext
    ) -> CIResult: ...


class HaltOnFailure:
    """The Epic 3 default: a red gate or red CI stops the run, carrying the evidence."""

    async def on_test_failure(
        self, story: Story, result: GateResult, ctx: RunContext
    ) -> Recovery:
        raise CodeFailure(
            f"story {story.id} ({story.title}) failed its tests (exit {result.exit_code}). "
            "Last output:\n" + result.tail(),
            {"story": story.id, "exit_code": result.exit_code, "output": result.output},
        )

    async def on_ci_failure(
        self, epic: Epic, ci: CIResult, pr: PullRequestInfo, ctx: RunContext
    ) -> CIResult:
        raise CIFailure(
            f"CI failed on epic {epic.number}'s pull request {pr.url}: "
            + "; ".join(ci.failures)
            + ". The pull request has not been merged.",
            {"epic": epic.number, "pull_request": pr.number, "failures": list(ci.failures)},
        )


def _say(text: str) -> None:
    print_to_terminal(text + "\n")


class ImplementationStage:
    name = "implementation"

    def __init__(
        self,
        gitops: GitOps,
        runner: AgentRunner | None = None,
        failures: FailureHandler | None = None,
        checkpoint: CheckpointHandler | None = None,
        test_timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        on_test_output: Callable[[str], None] = print_to_terminal,
        say: Callable[[str], None] = _say,
    ) -> None:
        self.git = gitops
        self.runner = runner or ClaudeCodeRunner(allowed_tools=IMPLEMENTATION_TOOLS)
        self.failures = failures or HaltOnFailure()
        self.checkpoint = checkpoint
        self.test_timeout = test_timeout
        self.on_test_output = on_test_output
        self.say = say

    # ------------------------------------------------------------------- the loop

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        command = recorder.lock.test_runner()
        if command is None:
            raise EnvFailure("foundry.lock has no [run.test_runner]; there is nothing to gate on.")
        workspace = ctx.workspace_path
        architecture = await asyncio.to_thread(read_artifact, workspace, "architecture")
        epics = validate_plan(await asyncio.to_thread(read_artifact, workspace, "epics"))
        reviewer = EpicReviewer.for_run(ctx, self.checkpoint or EditorCheckpoint(), recorder)

        completed: list[str] = []
        merged: list[int] = []
        for index, epic in enumerate(epics):
            self._check_cancel(ctx, f"epic {epic.number}")
            state = ProjectState(
                tuple(completed), tuple(merged), tuple(e.number for e in epics[index + 1 :])
            )
            review = await reviewer.before_epic(epic, state)
            if review.reviewed:
                # The review's checkpoint commit is on main; publish it so main stays in step
                # with the remote and the post-merge fast-forward stays a fast-forward.
                await self.git.push(MAIN_BRANCH, set_upstream=False)
            epic = review.epic
            if not epic.stories:
                self.say(f"Epic {epic.number}: every story was skipped in review; nothing to do.")
                continue

            self.say(f"Epic {epic.number}: {epic.title}")
            branch = await self.start_epic(epic, ctx, recorder)
            for story in epic.stories:
                self._check_cancel(ctx, f"story {story.id}")
                await self.implement_story(story, architecture, command, ctx, recorder)
                completed.append(story.id)
            await self.finish_epic(epic, branch, ctx, recorder)
            merged.append(epic.number)

    @staticmethod
    def _check_cancel(ctx: RunContext, before: str) -> None:
        if is_cancel_requested(ctx.workspace_path):
            raise CancelRequested(before)

    async def implement_story(
        self,
        story: Story,
        architecture: str,
        command: str,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
    ) -> None:
        """Session, gate, commit. The clock covers all three, retries included."""
        if not await self.git.is_clean():
            dirty = ", ".join((await self.git.dirty_files())[:5])
            raise EnvFailure(
                f"cannot start story {story.id}: the working tree has uncommitted changes "
                f"({dirty}). Commit or discard them first."
            )
        started = time.monotonic()
        self.say(f"  Story {story.id}: {story.title}")
        await self.run_session(story, architecture, command, ctx)
        self.say("")
        recovery = await self.run_gate(story, command, ctx)
        await self.commit_story(story, recovery, time.monotonic() - started, recorder)
        counted = recovery.result.tests_passed
        self.say(f"  Story {story.id} committed ({counted if counted is not None else '?'} tests passed)")

    # --------------------------------------------------------------- one story

    async def run_session(
        self, story: Story, architecture: str, test_command: str, ctx: RunContext
    ) -> AgentResult:
        """One story, one brand-new `claude -p` subprocess with the repo root as its cwd.

        The runner writes `.foundry/current-story.md`, streams the session's output to the
        terminal as it arrives and deletes the file when the session ends, win or lose, so the
        prompt never exists while tests run (AD-7). Nothing carries over between stories.
        """
        prompt = build_story_prompt(story, architecture, test_command)
        return await self.runner.run(AgentRequest(name=f"story {story.id}", prompt=prompt), ctx)

    async def run_gate(self, story: Story, command: str, ctx: RunContext) -> Recovery:
        """Run the recorded test command; a pass proceeds to commit, a fail goes to Epic 4's
        failure handler with the captured output (FR-18)."""
        result = await run_test_command(
            command, ctx.workspace_path, timeout=self.test_timeout, on_output=self.on_test_output
        )
        if result.passed:
            return Recovery(result)
        recovery = await self.failures.on_test_failure(story, result, ctx)
        if not recovery.result.passed:
            raise CodeFailure(
                f"story {story.id} still fails its tests after recovery",
                {"story": story.id, "output": recovery.result.output},
            )
        return recovery

    async def commit_story(
        self,
        story: Story,
        recovery: Recovery,
        duration_seconds: float,
        recorder: ProvenanceRecorder,
    ) -> None:
        """Commit a story whose tests passed: its changes plus its lock entry, in one commit.

        A story that changed nothing is a failure, not a pass: the tests were green before the
        session started, so "green" proves nothing about the story.
        """
        await self.git.stage_all()
        digest = await self.git.staged_diff_hash()
        if digest is None:
            raise CodeFailure(
                f"story {story.id} ({story.title}) produced no changes; the session ended "
                "without implementing anything.",
                {"story": story.id},
            )
        await recorder.record_story(
            story,
            artifact_hash=digest,
            tests_passed=recovery.result.tests_passed,
            duration_seconds=duration_seconds,
            retries=recovery.retries,
        )

    # ---------------------------------------------------------------- one epic

    async def start_epic(
        self, epic: Epic, ctx: RunContext, recorder: ProvenanceRecorder
    ) -> str:
        """Create `epic-{N}-{slug}` from main, record it, push it. Returns the branch name.

        Every story commit of the epic lands on this branch. The push happens before any story
        starts, so the branch exists on the remote even if the first session never finishes.
        """
        branch = epic_branch_name(epic)
        await self._require_clean_main(epic)
        await self.git.create_branch(branch, base=MAIN_BRANCH)
        await recorder.record_epic_branch(
            epic.number, epic.title, branch, MAIN_BRANCH, epic.story_ids
        )
        await self.git.push(branch)
        return branch

    async def finish_epic(
        self, epic: Epic, branch: str, ctx: RunContext, recorder: ProvenanceRecorder
    ) -> PullRequestInfo:
        """PR from the epic branch to main, wait for CI, merge when green (FR-20).

        Red CI goes to the failure handler (Epic 4); until it exists the default stops the run
        with the failing checks and the PR link, and the PR is left open. After the merge, main
        is brought up to date and the merge is recorded on main in its own commit.
        """
        await self.git.push(branch, set_upstream=False)  # every story commit is on the remote
        pr = await self.git.create_pr(
            head=branch,
            base=MAIN_BRANCH,
            title=f"Epic {epic.number}: {epic.title}",
            body=_pr_body(epic),
        )
        self.say(f"Epic {epic.number}: opened {pr.url}; waiting for CI")

        ci = await self.git.poll_ci(pr.head_sha)
        if not ci.passed:
            ci = await self.failures.on_ci_failure(epic, ci, pr, ctx)
            if not ci.passed:
                raise CIFailure(
                    f"CI still fails on {pr.url} after recovery: " + "; ".join(ci.failures),
                    {"epic": epic.number, "pull_request": pr.number},
                )

        merge = await self.git.merge_pr(pr.number, f"Merge epic {epic.number}: {epic.title} (#{pr.number})")
        await self.git.sync_branch(MAIN_BRANCH)
        await recorder.record_epic_merged(epic.number, pr, ci, merge.sha)
        await self.git.push(MAIN_BRANCH, set_upstream=False)
        self.say(f"Epic {epic.number}: merged")
        return pr

    async def _require_clean_main(self, epic: Epic) -> None:
        current = await self.git.current_branch()
        if current != MAIN_BRANCH:
            raise EnvFailure(
                f"epic {epic.number} must start from {MAIN_BRANCH}, but the working tree is on "
                f"{current!r}. Finish or abandon that branch first."
            )
        if not await self.git.is_clean():
            dirty = ", ".join((await self.git.dirty_files())[:5])
            raise EnvFailure(
                f"cannot start epic {epic.number}: the working tree has uncommitted changes "
                f"({dirty}). Commit or discard them first."
            )


def _pr_body(epic: Epic) -> str:
    stories = "\n".join(f"- Story {story.id}: {story.title}" for story in epic.stories)
    return (
        f"Implements epic {epic.number}: {epic.title}.\n\n{stories}\n\n"
        "Each story is one commit on this branch, written by its own Claude Code session and "
        "gated on the project's test command. Opened by Foundry; it merges when CI is green."
    )

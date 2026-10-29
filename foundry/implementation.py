"""The implementation loop: one feature branch per epic, one Claude Code session per story.

Order inside an epic: branch from `main` and push it (FR-16), then for each story write the
prompt, run a fresh `claude -p` subprocess (AD-7), run the recorded test command (FR-18),
commit on green (FR-19); when the epic is done open a PR, wait for CI and merge (FR-20).
Every git/GitHub mutation is a GitOps call (AD-8) and every action is recorded in the lock
and in git together (AD-2).

Failures go through a FailureHandler (Epic 4): the default here stops at the first one, the
CLI wires in the retry / backoff / escalate policy from `foundry.failures`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from foundry.agent import AgentRequest, AgentResult, AgentRunner, ClaudeCodeRunner, print_to_terminal
from foundry.artifacts import read_artifact, write_artifact
from foundry.cancel import is_cancel_requested
from foundry.checkpoint import CheckpointHandler, EditorCheckpoint
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    FoundryError,
    RunContext,
)
from foundry.epics import Epic, Story, parse_stories, validate_plan
from foundry.failures import (
    BackoffPolicy,
    EscalationCancelled,
    FailureHandler,
    Recovery,
    RetriesExhausted,
    StoryAttempt,
    with_backoff,
)
from foundry.gitops import MAIN_BRANCH, CIResult, GitOps, PullRequestInfo
from foundry.progress import RunProgress, progress_from_lock
from foundry.provenance import ProvenanceRecorder
from foundry.review import EpicReviewer, ProjectState
from foundry.scaffold import slugify
from foundry.stages import CancelRequested
from foundry.testgate import DEFAULT_TIMEOUT_SECONDS, GateResult, run_test_command

__all__ = [
    "CIFailure",
    "FailureHandler",
    "HaltOnFailure",
    "ImplementationStage",
    "Recovery",
    "build_story_prompt",
    "epic_branch_name",
]

T = TypeVar("T")

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


class HaltOnFailure:
    """The default handler: any failure stops the run, carrying the evidence."""

    async def on_test_failure(
        self, story: Story, result: GateResult, ctx: RunContext, attempt: StoryAttempt | None = None
    ) -> Recovery:
        raise CodeFailure(
            f"story {story.id} ({story.title}) failed its tests (exit {result.exit_code}). "
            "Last output:\n" + result.tail(),
            {"story": story.id, "exit_code": result.exit_code, "output": result.output},
        )

    async def on_ci_failure(
        self,
        epic: Epic,
        ci: CIResult,
        pr: PullRequestInfo,
        ctx: RunContext,
        recheck: Callable[[], Awaitable[CIResult]] | None = None,
    ) -> CIResult:
        raise CIFailure(
            f"CI failed on epic {epic.number}'s pull request {pr.url}: "
            + "; ".join(ci.failures)
            + ". The pull request has not been merged.",
            {"epic": epic.number, "pull_request": pr.number, "failures": list(ci.failures)},
        )

    async def on_infra_exhausted(
        self,
        scope: str,
        error: RetriesExhausted,
        ctx: RunContext,
        attempt: StoryAttempt | None = None,
    ) -> Recovery | None:
        raise error


def _say(text: str) -> None:
    print_to_terminal(text + "\n")


@dataclass
class _Attempt:
    """What a failure handler may do to the story in front of it (see StoryAttempt)."""

    stage: ImplementationStage
    story: Story
    command: str
    ctx: RunContext
    architecture: str | None = None

    async def retry(self, notes: str | None = None, story: Story | None = None) -> GateResult:
        story = story or self.story
        architecture = self.architecture
        if architecture is None:
            architecture = await asyncio.to_thread(read_artifact, self.ctx.workspace_path, "architecture")
        await self.stage.run_session(story, architecture, self.command, self.ctx, notes=notes)
        self.stage.say("")
        return await self.gate()

    async def gate(self) -> GateResult:
        return await self.stage.run_test_gate(self.command, self.ctx)

    async def diff(self) -> str:
        return await self.stage.git.working_diff()


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
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self.git = gitops
        self.runner = runner or ClaudeCodeRunner(allowed_tools=IMPLEMENTATION_TOOLS)
        self.failures: FailureHandler = failures or HaltOnFailure()
        self.checkpoint = checkpoint
        self.test_timeout = test_timeout
        self.on_test_output = on_test_output
        self.say = say
        self.backoff = backoff or BackoffPolicy.none()

    # ------------------------------------------------------------------- the loop

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        """Every epic in order. The lock says what is already done, so the same loop starts a run
        and finishes one that `foundry resume` picked up: merged epics are skipped, a started epic
        is rejoined on its branch, and only stories with no record are implemented (NFR-4)."""
        command = recorder.lock.test_runner()
        if command is None:
            raise EnvFailure("foundry.lock has no [run.test_runner]; there is nothing to gate on.")
        workspace = ctx.workspace_path
        architecture = await asyncio.to_thread(read_artifact, workspace, "architecture")
        epics = validate_plan(await asyncio.to_thread(read_artifact, workspace, "epics"))
        reviewer = EpicReviewer.for_run(ctx, self.checkpoint or EditorCheckpoint(), recorder)

        progress = self._progress(recorder)
        if progress.merged_epics:
            # A crash after the merge record but before the push leaves main ahead of the remote.
            await self._guard(
                "the run", "push main", lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx
            )

        for index, epic in enumerate(epics):
            self._check_cancel(ctx, f"epic {epic.number}")
            progress = self._progress(recorder)
            if epic.number in progress.merged_epics:
                continue
            resuming = epic.number in progress.started_epics
            if not resuming:
                await self.consistency_pass(ctx, recorder, command, architecture)

            if resuming:
                epic = self._recorded_epic(epic, recorder, workspace)
            else:
                epic = await self._review(epic, epics[index + 1 :], progress, ctx, recorder, reviewer)
                if not epic.stories:
                    self.say(f"Epic {epic.number}: every story was skipped in review; nothing to do.")
                    continue

            self.say(f"Epic {epic.number}: {epic.title}")
            with self._located(epic=epic.number):
                if resuming:
                    branch = await self.rejoin_epic(epic, command, ctx, recorder)
                else:
                    branch = await self.start_epic(epic, ctx, recorder)
                for story in epic.stories:
                    if story.id in self._settled(recorder):
                        continue
                    self._check_cancel(ctx, f"story {story.id}")
                    with self._located(story=story.id):
                        await self.implement_story(story, architecture, command, ctx, recorder)
                deferred = [s for s in epic.stories if s.id in self._progress(recorder).deferred]
                # Even if every story was deferred the branch carries the epic's lock entries,
                # so it still goes through a pull request and lands on main.
                await self.finish_epic(epic, branch, ctx, recorder, deferred=deferred)

        await self.consistency_pass(ctx, recorder, command, architecture)

    @staticmethod
    def _progress(recorder: ProvenanceRecorder) -> RunProgress:
        progress = progress_from_lock(recorder.lock.read())
        if progress is None:
            raise EnvFailure("foundry.lock records no run.")
        return progress

    def _settled(self, recorder: ProvenanceRecorder) -> set[str]:
        progress = self._progress(recorder)
        return set(progress.implemented) | set(progress.deferred) | set(progress.skipped_in_review)

    async def _review(
        self,
        epic: Epic,
        later: Sequence[Epic],
        progress: RunProgress,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
        reviewer: EpicReviewer,
    ) -> Epic:
        """The epic as it will be built: reviewed if `--review-epics`, and only reviewed once."""
        data = recorder.lock.read()
        review_key = f"epic-{epic.number}-review"
        if review_key in (data.get("checkpoints") or {}):
            # Reviewed before a crash and not started: do not ask again (the record is append-only).
            return self._reviewed_epic(epic, data["checkpoints"][review_key], ctx.workspace_path)
        state = ProjectState(
            progress.implemented, progress.merged_epics, tuple(e.number for e in later)
        )
        review = await reviewer.before_epic(epic, state)
        if review.reviewed:
            text = "\n\n".join(story.raw for story in review.epic.stories)
            await asyncio.to_thread(
                write_artifact,
                ctx.workspace_path,
                f"epic-{epic.number}-reviewed",
                Artifact(text + "\n", ArtifactType.EPIC_REVIEW),
            )
            # The review's checkpoint commit is on main; publish it so main stays in step
            # with the remote and the post-merge fast-forward stays a fast-forward.
            await self._guard(
                f"epic {epic.number}", "push main", lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx
            )
        return review.epic

    @staticmethod
    def _reviewed_epic(epic: Epic, checkpoint: dict[str, Any], workspace: Any) -> Epic:
        """The epic as the human left it, from the saved review (or, failing that, the lock)."""
        by_id = {story.id: story for story in epic.stories}
        try:
            by_id.update({s.id: s for s in parse_stories(read_artifact(workspace, f"epic-{epic.number}-reviewed"))})
        except EnvFailure:
            pass  # the review was recorded but its text was not saved: the original texts stand
        order = [str(sid) for sid in checkpoint.get("story_order", epic.story_ids)]
        return Epic(epic.number, epic.title, tuple(by_id[sid] for sid in order if sid in by_id))

    def _recorded_epic(self, epic: Epic, recorder: ProvenanceRecorder, workspace: Any) -> Epic:
        """A started epic: the stories it was started with, in the order it was started with."""
        record = recorder.lock.read()["epics"][f"epic-{epic.number}"]
        by_id = {story.id: story for story in epic.stories}
        try:
            by_id.update({s.id: s for s in parse_stories(read_artifact(workspace, f"epic-{epic.number}-reviewed"))})
        except EnvFailure:
            pass
        ids = [str(sid) for sid in record.get("story_ids", epic.story_ids)]
        return Epic(epic.number, epic.title, tuple(by_id[sid] for sid in ids if sid in by_id))

    @staticmethod
    def _check_cancel(ctx: RunContext, before: str) -> None:
        if is_cancel_requested(ctx.workspace_path):
            raise CancelRequested(before)

    @staticmethod
    @contextmanager
    def _located(**where: Any) -> Iterator[None]:
        """Tell any error that leaves here which epic / story it happened in."""
        try:
            yield
        except FoundryError as err:
            for key, value in where.items():
                err.context.setdefault(key, value)
            raise

    # ------------------------------------------------------------ external calls

    async def _backoff(self, what: str, call: Callable[[], Awaitable[T]]) -> T:
        return await with_backoff(what, call, self.backoff, self.say)

    async def _guard(
        self, scope: str, what: str, call: Callable[[], Awaitable[T]], ctx: RunContext
    ) -> T:
        """An epic-level external call: backoff, and when that gives up, ask the human."""
        while True:
            try:
                return await self._backoff(what, call)
            except RetriesExhausted as exhausted:
                await self.failures.on_infra_exhausted(scope, exhausted, ctx, None)

    # ---------------------------------------------------------------- one story

    async def implement_story(
        self,
        story: Story,
        architecture: str,
        command: str,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
    ) -> bool:
        """Session, gate, commit. The clock covers all three, retries included.

        True when the story was committed, False when the human deferred it.
        """
        if not await self.git.is_clean():
            dirty = ", ".join((await self.git.dirty_files())[:5])
            raise EnvFailure(
                f"cannot start story {story.id}: the working tree has uncommitted changes "
                f"({dirty}). Commit or discard them first."
            )
        started = time.monotonic()
        self.say(f"  Story {story.id}: {story.title}")
        attempt = _Attempt(self, story, command, ctx, architecture)
        try:
            try:
                await self.run_session(story, architecture, command, ctx)
                self.say("")
                recovery = await self.run_gate(story, command, ctx, attempt)
            except RetriesExhausted as exhausted:
                outcome = await self.failures.on_infra_exhausted(
                    f"story {story.id}", exhausted, ctx, attempt
                )
                if outcome is None:  # a handler that cannot settle a story must raise
                    raise
                recovery = outcome
        except EscalationCancelled as stop:
            await recorder.record_story_outcome(
                story, "cancelled", failure=stop.failure, intervention=stop.intervention
            )
            raise

        if recovery.skipped:
            await recorder.record_story_outcome(
                story, "deferred", failure=recovery.failure, intervention=recovery.intervention
            )
            await self.git.stash(f"foundry: story {story.id} deferred")
            self.say(f"  Story {story.id} deferred; its changes are in `git stash`")
            return False
        await self.commit_story(story, recovery, time.monotonic() - started, recorder)
        counted = recovery.result.tests_passed
        self.say(f"  Story {story.id} committed ({counted if counted is not None else '?'} tests passed)")
        return True

    async def run_session(
        self,
        story: Story,
        architecture: str,
        test_command: str,
        ctx: RunContext,
        notes: str | None = None,
    ) -> AgentResult:
        """One story, one brand-new `claude -p` subprocess with the repo root as its cwd.

        The runner writes `.foundry/current-story.md`, streams the session's output to the
        terminal as it arrives and deletes the file when the session ends, win or lose, so the
        prompt never exists while tests run (AD-7). Nothing carries over between stories.
        `notes` (the failing test output of an earlier attempt) is appended to the prompt;
        infrastructure errors are retried with the very same prompt and never shown to the LLM.
        """
        prompt = build_story_prompt(story, architecture, test_command)
        if notes:
            prompt += "\n" + notes
        request = AgentRequest(name=f"story {story.id}", prompt=prompt)
        return await self._backoff(
            f"story {story.id} session", lambda: self.runner.run(request, ctx)
        )

    async def run_test_gate(self, command: str, ctx: RunContext) -> GateResult:
        return await run_test_command(
            command, ctx.workspace_path, timeout=self.test_timeout, on_output=self.on_test_output
        )

    async def run_gate(
        self, story: Story, command: str, ctx: RunContext, attempt: StoryAttempt | None = None
    ) -> Recovery:
        """Run the recorded test command; a pass proceeds to commit, a fail goes to the failure
        handler with the captured output (FR-18)."""
        result = await self.run_test_gate(command, ctx)
        if result.passed:
            return Recovery(result)
        attempt = attempt or _Attempt(self, story, command, ctx)
        recovery = await self.failures.on_test_failure(story, result, ctx, attempt)
        if not recovery.skipped and not recovery.result.passed:
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
        recovered: bool = False,
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
            kind=recovery.kind,
            failure=recovery.failure,
            intervention=recovery.intervention,
            recovered=recovered,
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
        if await self.git.branch_exists(branch):
            # A crash after the branch was created and before it was recorded. Reuse it, but only
            # if it holds nothing of anyone's work (a `foundry: resumed` record is not work).
            if [s for s in await self.git.subjects_ahead(branch) if not s.startswith("foundry: resumed")]:
                raise EnvFailure(
                    f"branch {branch} already exists with commits that foundry.lock does not "
                    f"record. Rename or delete it, then resume."
                )
            if await self.git.current_branch() != branch:
                await self._require_clean_main(epic)
                await self.git.checkout(branch)
        else:
            await self._require_clean_main(epic)
            await self.git.create_branch(branch, base=MAIN_BRANCH)
        await recorder.record_epic_branch(
            epic.number, epic.title, branch, MAIN_BRANCH, epic.story_ids
        )
        await self._guard(f"epic {epic.number}", "push epic branch", lambda: self.git.push(branch), ctx)
        return branch

    async def finish_epic(
        self,
        epic: Epic,
        branch: str,
        ctx: RunContext,
        recorder: ProvenanceRecorder,
        deferred: Sequence[Story] = (),
    ) -> PullRequestInfo:
        """PR from the epic branch to main, wait for CI, merge when green (FR-20).

        Red CI goes to the failure handler, which by default stops the run with the failing
        checks and the PR link (the PR stays open) and in the CLI asks the human. After the
        merge, main is brought up to date and the merge is recorded on main in its own commit.
        """
        scope = f"epic {epic.number}"
        await self._guard(scope, "push epic branch", lambda: self.git.push(branch, set_upstream=False), ctx)
        pr = await self._guard(
            scope,
            "open pull request",
            lambda: self.git.create_pr(
                head=branch,
                base=MAIN_BRANCH,
                title=f"Epic {epic.number}: {epic.title}",
                body=_pr_body(epic, deferred),
            ),
            ctx,
        )
        self.say(f"Epic {epic.number}: opened {pr.url}; waiting for CI")

        if pr.merged:
            # Found again after a crash between the merge and its record: nothing left to wait for.
            ci = CIResult(True, ("merged before the run resumed",))
            merge_sha = pr.merge_sha
            self.say(f"Epic {epic.number}: {pr.url} was already merged")
        else:
            ci = await self._guard(scope, "wait for CI", lambda: self.git.poll_ci(pr.head_sha), ctx)
            if not ci.passed:
                current = [pr]

                async def recheck() -> CIResult:
                    fresh = await self._guard(
                        scope, "read pull request", lambda: self.git.pull_request(pr.number), ctx
                    )
                    current[0] = fresh
                    return await self._guard(
                        scope, "wait for CI", lambda: self.git.poll_ci(fresh.head_sha), ctx
                    )

                ci = await self.failures.on_ci_failure(epic, ci, pr, ctx, recheck)
                pr = current[0]
                if not ci.passed:
                    raise CIFailure(
                        f"CI still fails on {pr.url} after recovery: " + "; ".join(ci.failures),
                        {"epic": epic.number, "pull_request": pr.number},
                    )
            title = f"Merge epic {epic.number}: {epic.title} (#{pr.number})"
            merge = await self._guard(
                scope, "merge pull request", lambda: self.git.merge_pr(pr.number, title), ctx
            )
            merge_sha = merge.sha
        await self._guard(scope, "update main", lambda: self.git.sync_branch(MAIN_BRANCH), ctx)
        await recorder.record_epic_merged(epic.number, pr, ci, merge_sha)
        await self._guard(scope, "push main", lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx)
        self.say(f"Epic {epic.number}: merged")
        return pr

    async def rejoin_epic(
        self, epic: Epic, command: str, ctx: RunContext, recorder: ProvenanceRecorder
    ) -> str:
        """Back onto a started epic's branch after an interruption (AD-6).

        Changes left in the working tree by the interrupted story are judged by the tests: green
        means the session finished and only the commit was lost, so it is committed as that story;
        red means it did not, so the changes are set aside (stashed, not lost) and the story runs
        again. A clean tree means the next story simply starts. At worst one story is re-run.
        """
        branch = epic_branch_name(epic)
        recorded = recorder.lock.read()["epics"][f"epic-{epic.number}"].get("branch", branch)
        branch = str(recorded)
        dirty = not await self.git.is_clean()
        if await self.git.current_branch() != branch:
            if dirty:
                files = ", ".join((await self.git.dirty_files())[:5])
                raise EnvFailure(
                    f"cannot continue epic {epic.number} on {branch}: the working tree has "
                    f"uncommitted changes ({files}) on another branch. Commit or discard them first."
                )
            await self.git.switch_to_branch(branch)
        await self._guard(f"epic {epic.number}", "push epic branch", lambda: self.git.push(branch), ctx)

        if dirty:
            todo = [s for s in epic.stories if s.id not in self._settled(recorder)]
            if not todo:
                files = ", ".join((await self.git.dirty_files())[:5])
                raise EnvFailure(
                    f"every story of epic {epic.number} is recorded, but the working tree has "
                    f"uncommitted changes ({files}). Commit or discard them first."
                )
            story = todo[0]
            self.say(f"  Story {story.id} was interrupted; running the tests on what it left behind")
            result = await self.run_test_gate(command, ctx)
            if result.passed:
                await self.commit_story(story, Recovery(result), 0.0, recorder, recovered=True)
                self.say(f"  Story {story.id} committed ({result.tests_passed or '?'} tests passed)")
            else:
                await self.git.stash(f"foundry: story {story.id} discarded on resume")
                self.say(f"  Story {story.id} left failing tests; its changes are in `git stash` and it runs again")
        return branch

    async def consistency_pass(
        self, ctx: RunContext, recorder: ProvenanceRecorder, command: str, architecture: str
    ) -> None:
        """After a model change, one dedicated session normalises style across the codebase (FR-34).

        It runs between epics, on `main`, once for every change since the last pass, and is
        skipped when the model never changed. It must not change behaviour: if the tests are no
        longer green afterwards its changes are set aside (stashed) and the pass is recorded as
        reverted, because a cosmetic pass is never worth a broken build.
        """
        data = recorder.lock.read()
        pending = progress_from_lock(data)
        change = pending.pending_model_change if pending else None
        if change is None:
            return
        if await self.git.current_branch() != MAIN_BRANCH or not await self.git.is_clean():
            raise EnvFailure(
                "the consistency pass needs a clean working tree on main, between epics."
            )
        total, done = change.stories_total, change.stories_completed
        self.say(f"Consistency pass: {change.original_model} -> {change.new_model} (model boundary at story {done}/{total})")
        prompt = build_consistency_prompt(change.original_model, change.new_model, command, architecture)
        request = AgentRequest(name="consistency pass", prompt=prompt)
        await self._guard(
            "the consistency pass", "consistency pass session",
            lambda: self.runner.run(request, ctx), ctx,
        )
        self.say("")
        common = dict(
            through_change=change.through_change,
            original_model=change.original_model,
            new_model=change.new_model,
            boundary_story=change.boundary_story,
            completed=done,
            total=total,
        )
        if await self.git.is_clean():
            await recorder.record_consistency_pass(**common, outcome="no changes needed", files_changed=0)
            return
        result = await self.run_test_gate(command, ctx)
        if not result.passed:
            await self.git.stash("foundry: consistency pass reverted (tests failed)")
            self.say("  The consistency pass broke the tests, so its changes were set aside in `git stash`.")
            await recorder.record_consistency_pass(**common, outcome="reverted: tests failed", files_changed=0)
            return
        await self.git.stage_all()
        files = len(await self.git.staged_files())
        await recorder.record_consistency_pass(**common, outcome="applied", files_changed=files, paths=None)
        await self._guard(
            "the consistency pass", "push main",
            lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx,
        )

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


def build_consistency_prompt(
    original_model: str, new_model: str, test_command: str, architecture: str
) -> str:
    return (
        "# Task: consistency pass\n\n"
        "You are running non-interactively inside Foundry: no one can answer questions.\n\n"
        f"This project was built by `{original_model}` and the rest of it is now being built by "
        f"`{new_model}`. Review the whole codebase in the current directory and make it read as if "
        "one person wrote it.\n\n"
        "Focus on:\n"
        "- naming conventions (files, functions, classes, variables, tests);\n"
        "- formatting and import order;\n"
        "- comment and docstring style.\n\n"
        "Rules:\n"
        "- Do NOT change behaviour or logic, add features or remove tests. Renames must update "
        "every use.\n"
        f"- The project's test command is `{test_command}`; it must still exit 0 when you finish.\n"
        "- Prefer the conventions already used most in the codebase and the architecture below.\n"
        "- Do not run `git commit`, `git push` or change branches: Foundry commits for you.\n"
        "- Do not edit `foundry.lock` or anything under `.foundry/`.\n"
        "- If the code is already consistent, change nothing.\n"
        "- Your final message is a short summary of what you changed.\n\n"
        "## Architecture\n\n"
        f"{architecture.strip()}\n"
    )


def _pr_body(epic: Epic, deferred: Sequence[Story] = ()) -> str:
    stories = "\n".join(f"- Story {story.id}: {story.title}" for story in epic.stories)
    body = (
        f"Implements epic {epic.number}: {epic.title}.\n\n{stories}\n\n"
        "Each story is one commit on this branch, written by its own Claude Code session and "
        "gated on the project's test command. Opened by Foundry; it merges when CI is green."
    )
    if deferred:
        listed = "\n".join(f"- Story {story.id}: {story.title}" for story in deferred)
        body += (
            "\n\n**Deferred by a human during the run, so not included in this pull request:**\n\n"
            + listed
        )
    return body

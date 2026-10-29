"""Failure handling: classify, retry, back off, escalate, stop (Epic 4, FR-21..24).

Every external call (Claude Code, GitHub, git, the test command, $EDITOR) already raises a
FoundryError whose category says how to recover. This module is the policy on top of that:

  * code            one automatic retry in a fresh session, then the human (4.2, 4.4);
  * infrastructure  exponential backoff with jitter, then the human (4.3, 4.4);
  * environment     no retry: the run stops with what to fix (4.5).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, TypeVar, runtime_checkable

from foundry.checkpoint import CheckpointHandler, EditorCheckpoint
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    FailureCategory,
    FoundryError,
    InfraFailure,
    RunContext,
    classify_exception,
    sha256_of,
)
from foundry.epics import Epic, Story
from foundry.gitops import CIResult, PullRequestInfo
from foundry.stages import CancelRequested
from foundry.testgate import GateResult

T = TypeVar("T")


# ----------------------------------------------------------------------- 4.1 classifier


class FailureClassifier:
    """Wraps an external call so only classified errors leave it (AD-3)."""

    def classify(self, exc: BaseException, source: str | None = None) -> FoundryError:
        return classify_exception(exc, source)

    async def call(
        self, source: str, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self.classify(exc, source) from exc

    def sync_call(self, source: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise self.classify(exc, source) from exc


CLASSIFIER = FailureClassifier()


# ------------------------------------------------------------------------- 4.5 fix hints

# (pattern in the message, what to fix). First match wins; most specific first.
_FIX_HINTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), hint)
    for pattern, hint in (
        (
            r"GITHUB_TOKEN is not set",
            "Set the GITHUB_TOKEN environment variable to a GitHub token with `repo` and "
            "`workflow` scope.",
        ),
        (
            r"rejected GITHUB_TOKEN|bad credentials",
            "Create a new token and set GITHUB_TOKEN; the current one is invalid or expired.",
        ),
        (
            r"token needs `repo`|refused to",
            "Give GITHUB_TOKEN `repo` and `workflow` scope, and write access to the repository.",
        ),
        (
            r"could not find 'claude'|could not launch 'claude'",
            "Install Claude Code and make sure `claude` is on PATH.",
        ),
        (r"git was not found", "Install git and make sure it is on PATH."),
        (r"disk is full|no space left", "Free up disk space."),
        (r"permission denied", "Make sure the current user can write to the project directory."),
        (
            r"\$EDITOR|could not launch editor|editor .* exited",
            "Set EDITOR to an installed editor that blocks until the file is closed "
            "(for example `code --wait`).",
        ),
        (
            r"test command could not run",
            "Fix `[run.test_runner]` in the architecture, or install what the test command needs.",
        ),
        (
            r"no CI checks appeared",
            "Check that .github/workflows/ci.yml is on main and that GitHub Actions is enabled "
            "for the repository.",
        ),
        (r"uncommitted changes", "Commit or stash the uncommitted changes."),
        (
            r"already has a git remote",
            "Run Foundry in a directory that is not attached to a git remote.",
        ),
        (r"FOUNDRY_MODEL", "Set FOUNDRY_MODEL to a pinned model ID."),
    )
)
GENERIC_FIX = "Fix the problem described above."


def fix_hint(error: FoundryError) -> str:
    """What the human should do about an environment failure."""
    if error.fix:
        return error.fix
    for pattern, hint in _FIX_HINTS:
        if pattern.search(error.message):
            return hint
    return GENERIC_FIX


def describe_halt(error: FoundryError) -> str:
    """The message the human reads when an environment failure stops the run."""
    return f"{error.message}\nTo fix: {fix_hint(error)}\nThen run `foundry resume` to continue."


# ----------------------------------------------------------------------- 4.3 backoff

DEFAULT_MAX_RETRIES = 5
MAX_RETRIES_ENV_VAR = "FOUNDRY_MAX_RETRIES"


class RetriesExhausted(InfraFailure):
    """An infrastructure failure that outlasted the backoff policy."""

    def __init__(self, what: str, attempts: int, last: FoundryError) -> None:
        super().__init__(
            f"{what} still failing after {attempts} {'retry' if attempts == 1 else 'retries'}: "
            f"{last.message}",
            {"attempts": attempts, "what": what},
        )
        self.attempts = attempts
        self.last_error = last


@dataclass
class BackoffPolicy:
    """Exponential backoff with jitter: waits ~base, base*factor, base*factor^2 ... up to max_delay.

    Each wait is its nominal value scaled by a random factor in [0.5, 1.0] so a crowd of
    clients that failed together does not retry together. `max_retries=0` disables retrying.
    """

    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = 2.0
    factor: float = 2.0
    max_delay: float = 120.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    rng: Callable[[], float] = field(default=random.random, repr=False)

    @classmethod
    def none(cls) -> BackoffPolicy:
        return cls(max_retries=0)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BackoffPolicy:
        env = os.environ if env is None else env
        raw = (env.get(MAX_RETRIES_ENV_VAR) or "").strip()
        if not raw:
            return cls()
        try:
            retries = int(raw)
        except ValueError:
            retries = -1
        if retries < 0:
            raise EnvFailure(
                f"{MAX_RETRIES_ENV_VAR} must be a whole number of retries (0 or more), got {raw!r}.",
                fix=f"Set {MAX_RETRIES_ENV_VAR} to a number such as {DEFAULT_MAX_RETRIES}, or unset it.",
            )
        return cls(max_retries=retries)

    def delay(self, retry: int) -> float:
        """Seconds to wait before retry number `retry` (1-based)."""
        nominal = min(self.max_delay, self.base_delay * self.factor ** (retry - 1))
        return nominal * (0.5 + 0.5 * self.rng())


async def with_backoff(
    what: str,
    call: Callable[[], Awaitable[T]],
    policy: BackoffPolicy,
    say: Callable[[str], None] = lambda text: None,
) -> T:
    """Run `call`; retry infrastructure failures per `policy`.

    Only infrastructure failures are retried. The retry repeats the same call with the same
    inputs: the error text never reaches the LLM (FR-22). Waiting touches no files, so the lock
    and the working tree are exactly as they were when the call failed.
    """
    retries = 0
    while True:
        try:
            return await call()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = CLASSIFIER.classify(exc, what)
            if error.category is not FailureCategory.INFRASTRUCTURE or policy.max_retries == 0:
                if error is exc:
                    raise
                raise error from exc
            if retries >= policy.max_retries:
                raise RetriesExhausted(what, retries, error) from exc
            retries += 1
            wait = policy.delay(retries)
            say(
                f"  {what} failed ({error.message}); retry {retries}/{policy.max_retries} "
                f"in {wait:.1f}s"
            )
            await policy.sleep(wait)


# ------------------------------------------------------------- records, outcomes, seams


@dataclass(frozen=True)
class FailureRecord:
    """What `[failures.epic-E.story-S]` says about a story that did not pass first time."""

    category: str
    retry_outcome: str  # passed | failed | exhausted
    retries: int = 0
    exit_code: int | None = None
    output_hash: str | None = None  # a hash, never the output itself (NFR-7)

    def as_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "category": self.category,
            "retries": self.retries,
            "retry_outcome": self.retry_outcome,
        }
        if self.exit_code is not None:
            fields["exit_code"] = self.exit_code
        if self.output_hash is not None:
            fields["output_hash"] = self.output_hash
        return fields


@dataclass(frozen=True)
class InterventionRecord:
    """What `[interventions.epic-E.story-S]` says about the human's part in a story."""

    actions: tuple[str, ...]
    story_edited: bool = False
    story_hash: str | None = None
    code_edited: bool = False
    seconds: int = 0

    def as_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "actions": list(self.actions),
            "final_action": self.actions[-1],
            "rounds": len(self.actions),
            "story_edited": self.story_edited,
            "code_edited": self.code_edited,
            "editor_seconds": self.seconds,
        }
        if self.story_hash is not None:
            fields["story_hash"] = self.story_hash
        return fields


@dataclass(frozen=True)
class Recovery:
    """A failure handler's answer for one story: a passing gate (or a skip), and how it got there."""

    result: GateResult
    retries: int = 0
    kind: str = "implemented"  # implemented | auto-fixed | human (picks the commit message)
    failure: FailureRecord | None = None
    intervention: InterventionRecord | None = None
    skipped: bool = False


class EscalationCancelled(CancelRequested):
    """The human chose to cancel at an escalation. Carries what to record before stopping."""

    def __init__(
        self,
        before: str,
        failure: FailureRecord | None = None,
        intervention: InterventionRecord | None = None,
    ) -> None:
        super().__init__(before)
        self.failure = failure
        self.intervention = intervention


@runtime_checkable
class StoryAttempt(Protocol):
    """What the implementation stage lets a failure handler do to the story in front of it."""

    story: Story

    async def retry(self, notes: str | None = None, story: Story | None = None) -> GateResult:
        """A brand-new session (original prompt plus `notes`), then the test gate."""
        ...

    async def gate(self) -> GateResult:
        """Run the test command on the working tree as it is now."""
        ...

    async def diff(self) -> str:
        """Every change in the working tree since the last commit."""
        ...


@runtime_checkable
class FailureHandler(Protocol):
    """Where the failure policy plugs into the implementation stage. Raise to stop the run."""

    async def on_test_failure(
        self, story: Story, result: GateResult, ctx: RunContext, attempt: StoryAttempt
    ) -> Recovery: ...

    async def on_ci_failure(
        self,
        epic: Epic,
        ci: CIResult,
        pr: PullRequestInfo,
        ctx: RunContext,
        recheck: Callable[[], Awaitable[CIResult]],
    ) -> CIResult: ...

    async def on_infra_exhausted(
        self,
        scope: str,
        error: RetriesExhausted,
        ctx: RunContext,
        attempt: StoryAttempt | None,
    ) -> Recovery | None:
        """Backoff gave up. A Recovery settles the story; None asks the caller to try again."""
        ...


# ------------------------------------------------------------------- 4.2 retry prompt

_RETRY_OUTPUT_LINES = 300
_RETRY_OUTPUT_CHARS = 30_000


def _fence(text: str) -> str:
    """Wrap `text` in a code fence longer than any run of backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text.rstrip()}\n{fence}"


def tail_lines(text: str, lines: int = _RETRY_OUTPUT_LINES, chars: int = _RETRY_OUTPUT_CHARS) -> str:
    kept = text.rstrip().splitlines()
    clipped = len(kept) > lines
    out = "\n".join(kept[-lines:])
    if len(out) > chars:
        out, clipped = out[-chars:], True
    return ("[... earlier output omitted ...]\n" if clipped else "") + out


def retry_section(result: GateResult) -> str:
    """The failing test output, for the prompt of the session that tries again (FR-21)."""
    how = "timed out" if result.timed_out else f"exited with status {result.exit_code}"
    return (
        "## The previous attempt failed\n\n"
        f"The project's test command {how}. The previous attempt's changes are still in the "
        "working tree: fix what is failing instead of starting over, and keep the tests "
        "that already pass.\n\nTest output (the end of it):\n\n"
        + _fence(tail_lines(result.output))
        + "\n"
    )


# ---------------------------------------------------------------- 4.4 human escalation

STORY_ACTIONS = ("retry", "continue", "skip", "cancel")
EPIC_ACTIONS = ("retry", "cancel")
MAX_INVALID_ANSWERS = 3
MAX_ROUNDS = 10
_DIFF_LINES = 1500

_STORY_HELP = {
    "retry": "run a brand-new session on the story as written in this file (edit it first to change the story)",
    "continue": "you fixed the code in the working tree yourself: run the tests and commit it",
    "skip": "set the story aside (its changes are stashed) and carry on with the next one",
    "cancel": "stop the run here",
}
_EPIC_HELP = {
    "retry": "try again (fix the cause first; for failing CI, push a fix to the branch or re-run the check)",
    "cancel": "stop the run here",
}
_STORY_BEGIN, _STORY_END = "<!-- foundry:story:begin -->", "<!-- foundry:story:end -->"
_ACTION_LINE = re.compile(r"^action:[ \t]*(\S*)", re.MULTILINE | re.IGNORECASE)
_STORY_BLOCK = re.compile(
    re.escape(_STORY_BEGIN) + r"\n?(.*?)\n?" + re.escape(_STORY_END), re.DOTALL
)


@dataclass(frozen=True)
class Decision:
    action: str
    story_text: str | None  # the story as the human left it (None for epic-level escalations)
    seconds: int = 0


def render_escalation(
    *,
    label: str,
    what_happened: str,
    output: str,
    diff: str,
    story_text: str | None,
    allowed: tuple[str, ...],
    problem: str | None = None,
) -> str:
    help_text = _STORY_HELP if story_text is not None else _EPIC_HELP
    diff_lines = diff.splitlines()
    if len(diff_lines) > _DIFF_LINES:
        extra = len(diff_lines) - _DIFF_LINES
        diff = "\n".join(diff_lines[:_DIFF_LINES]) + f"\n... {extra} more lines"
    lines = [f"# Foundry needs you: {label}", ""]
    if problem:
        lines += [f"> **{problem}**", ""]
    lines += [
        "Foundry could not get this through on its own. Read what happened, then choose what "
        "to do by writing it after `action:` at the end of this file, saving, and closing the editor.",
        "",
        "## What happened",
        "",
        what_happened.strip(),
        "",
        "## Failing test output",
        "",
        _fence(tail_lines(output)) if output.strip() else "_No test output._",
        "",
        "## Changes in the working tree",
        "",
        _fence(diff) if diff.strip() else "_No changes._",
        "",
    ]
    if story_text is not None:
        lines += [
            "## Story (edit it here, then choose `retry`)",
            "",
            _STORY_BEGIN,
            story_text.strip(),
            _STORY_END,
            "",
        ]
    lines += ["## Decision", ""]
    lines += [f"- `{name}`: {help_text[name]}" for name in allowed]
    lines += ["", "Leave `action:` empty to cancel the run.", "", "action: ", ""]
    return "\n".join(lines)


def parse_decision(
    text: str, allowed: tuple[str, ...]
) -> tuple[str | None, str | None, str | None]:
    """(action, story text, problem). A None action comes with the problem to show again."""
    marker = text.rfind("## Decision")
    match = _ACTION_LINE.search(text[marker:] if marker >= 0 else text)
    story_match = _STORY_BLOCK.search(text)
    story_text = story_match.group(1).strip() if story_match else None
    word = match.group(1).strip().lower() if match else ""
    if not word:
        return "cancel", story_text, None
    if word not in allowed:
        return None, story_text, f"`{word}` is not one of: {', '.join(allowed)}"
    return word, story_text, None


class HumanEscalation:
    """Puts the evidence in $EDITOR and reads the human's decision back."""

    def __init__(self, checkpoint: CheckpointHandler | None = None) -> None:
        self.checkpoint = checkpoint or EditorCheckpoint()

    async def ask(
        self,
        *,
        name: str,
        label: str,
        what_happened: str,
        output: str,
        diff: str,
        story_text: str | None,
        allowed: tuple[str, ...],
    ) -> Decision:
        problem: str | None = None
        seconds = 0
        for _ in range(MAX_INVALID_ANSWERS):
            document = Artifact(
                render_escalation(
                    label=label,
                    what_happened=what_happened,
                    output=output,
                    diff=diff,
                    story_text=story_text,
                    allowed=allowed,
                    problem=problem,
                ),
                ArtifactType.ESCALATION,
            )
            try:
                result = await self.checkpoint.present(
                    document, {"phase": name, "base_phase": name, "revision": 0}
                )
            except Exception as exc:  # classify before propagating (AD-3)
                raise CLASSIFIER.classify(exc, "open $EDITOR for escalation") from exc
            seconds += result.duration_seconds
            action, edited_story, problem = parse_decision(result.artifact.content, allowed)
            if action is not None:
                return Decision(action, edited_story if story_text is not None else None, seconds)
        return Decision("cancel", None, seconds)


class RetryingFailureHandler:
    """The Epic 4 policy: retry code failures once, wait out infrastructure ones, then ask."""

    def __init__(
        self, checkpoint: CheckpointHandler | None = None, max_rounds: int = MAX_ROUNDS
    ) -> None:
        self.escalation = HumanEscalation(checkpoint)
        self.max_rounds = max_rounds

    # ---------------------------------------------------------------- code failures

    async def on_test_failure(
        self, story: Story, result: GateResult, ctx: RunContext, attempt: StoryAttempt
    ) -> Recovery:
        category, digest = "code", sha256_of(result.output)
        try:
            retried = await attempt.retry(retry_section(result))  # exactly one automatic retry
        except RetriesExhausted as exc:
            failure = FailureRecord(category, "exhausted", 1, result.exit_code, digest)
            return await self._escalate_story(
                story, attempt, failure, f"The retry could not run: {exc.message}", result.output
            )
        if retried.passed:
            failure = FailureRecord(category, "passed", 1, result.exit_code, digest)
            return Recovery(retried, retries=1, kind="auto-fixed", failure=failure)
        failure = FailureRecord(category, "failed", 1, result.exit_code, digest)
        return await self._escalate_story(
            story,
            attempt,
            failure,
            "The story failed its tests, and failed them again after one automatic retry "
            f"(exit status {retried.exit_code}).",
            retried.output,
        )

    # ------------------------------------------------------- infrastructure failures

    async def on_infra_exhausted(
        self, scope: str, error: RetriesExhausted, ctx: RunContext, attempt: StoryAttempt | None
    ) -> Recovery | None:
        what = (
            f"Foundry retried {error.attempts} times with increasing waits and the failure did "
            f"not clear:\n\n{error.last_error.message}"
        )
        if attempt is not None:
            failure = FailureRecord("infrastructure", "exhausted", error.attempts)
            return await self._escalate_story(attempt.story, attempt, failure, what, "")
        decision = await self.escalation.ask(
            name=f"escalation-{_slug(scope)}",
            label=scope,
            what_happened=what,
            output="",
            diff="",
            story_text=None,
            allowed=EPIC_ACTIONS,
        )
        if decision.action == "retry":
            return None
        raise EscalationCancelled(f"{scope} (cancelled at escalation)")

    # ------------------------------------------------------------------ red CI

    async def on_ci_failure(
        self,
        epic: Epic,
        ci: CIResult,
        pr: PullRequestInfo,
        ctx: RunContext,
        recheck: Callable[[], Awaitable[CIResult]],
    ) -> CIResult:
        for _ in range(self.max_rounds):
            decision = await self.escalation.ask(
                name=f"escalation-epic-{epic.number}-ci",
                label=f"CI failed on epic {epic.number}'s pull request",
                what_happened=f"{pr.url}\n\n" + "\n".join(f"- {line}" for line in ci.failures),
                output="",
                diff="",
                story_text=None,
                allowed=EPIC_ACTIONS,
            )
            if decision.action != "retry":
                raise EscalationCancelled(f"merging epic {epic.number} (cancelled at escalation)")
            ci = await recheck()
            if ci.passed:
                return ci
        raise CodeFailure(f"CI on {pr.url} still fails after {self.max_rounds} escalations.")

    # ----------------------------------------------------------- the story escalation

    async def _escalate_story(
        self,
        story: Story,
        attempt: StoryAttempt,
        failure: FailureRecord,
        what_happened: str,
        output: str,
    ) -> Recovery:
        actions: list[str] = []
        seconds = 0
        current = story
        story_edited = code_edited = False
        retries = failure.retries
        exit_code = failure.exit_code if failure.exit_code is not None else -1
        last = GateResult(False, exit_code, output, 0.0, None)

        def record() -> InterventionRecord:
            return InterventionRecord(
                tuple(actions),
                story_edited=story_edited,
                story_hash=sha256_of(current.raw) if story_edited else None,
                code_edited=code_edited,
                seconds=seconds,
            )

        for _ in range(self.max_rounds):
            decision = await self.escalation.ask(
                name=f"escalation-story-{story.id}",
                label=f"story {story.id}: {story.title}",
                what_happened=what_happened,
                output=output,
                diff=await attempt.diff(),
                story_text=current.raw,
                allowed=STORY_ACTIONS,
            )
            actions.append(decision.action)
            seconds += decision.seconds

            if decision.action == "cancel":
                raise EscalationCancelled(
                    f"story {story.id} (cancelled at escalation)", failure, record()
                )
            if decision.action == "skip":
                return Recovery(last, retries, failure=failure, intervention=record(), skipped=True)

            if decision.action == "retry":
                edited = (decision.story_text or "").strip()
                if edited and edited != current.raw.strip():
                    current, story_edited = replace(current, raw=edited), True
                retries += 1
                try:
                    last = await attempt.retry(retry_section(last) if output else None, current)
                except RetriesExhausted as exc:
                    what_happened, output = f"The retry could not run: {exc.message}", ""
                    continue
            else:  # continue: the human changed the code themselves
                code_edited = True
                last = await attempt.gate()

            if last.passed:
                return Recovery(
                    last,
                    retries,
                    kind="human" if code_edited else "implemented",
                    failure=failure,
                    intervention=record(),
                )
            what_happened = (
                f"The tests still fail after your {decision.action} (exit status {last.exit_code})."
            )
            output = last.output
        raise CodeFailure(
            f"story {story.id} still fails after {self.max_rounds} escalations; stopping the run.",
            {"story": story.id},
        )


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "escalation"

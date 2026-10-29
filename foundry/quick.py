"""Quick mode: `foundry quick "<idea>"` skips planning and builds from a one-page spec (FR-2).

Story 7.1, the spec. One Claude Code session writes a one-page spec (feature list, what is not
being built, data model sketch, test strategy, assumptions) and the human reviews and edits it
in `$EDITOR` (FR-35). No BMad agent is invoked. The test command is read from the spec's
`## Test strategy` section and recorded as `[run.test_runner]`, exactly where Pro mode's
architecture puts it, so the repository stage, CI workflow and test gate need no special case.

Story 7.2, the build. A planning session cuts the approved spec into ordered increments (models,
logic, interface, config...), each small enough to leave the tests green. Then every increment
gets its own session, the same test gate, retry and escalation as a Pro mode story, and its own
commit on `main`: `foundry: story Q.N implemented — <title>` (FR-36). An increment is a `Story`
whose id is `Q.N` and whose lock entries live under `implementation.quick.story-N`, so the
failure handling and provenance code is shared rather than copied.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from foundry.agent import AgentRequest, AgentRunner, ClaudeCodeRunner
from foundry.artifacts import read_artifact
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    RunContext,
    sha256_of,
)
from foundry.epics import Story
from foundry.gitops import MAIN_BRANCH, GitOps
from foundry.implementation import ImplementationStage
from foundry.phases import Phase
from foundry.planning import (
    MARKDOWN_DELIVERABLE,
    AgentPhase,
    build_prompt,
    extract_test_runner,
    section_body,
)
from foundry.provenance import ProvenanceRecorder
from foundry.scaffold import PLANNING_DIR

QUICK = "quick"
INCREMENT_PREFIX = "Q"
SPEC_TEST_SECTION = "Test strategy"
SPEC_REQUIRED_SECTIONS = ("Feature list", "Data model", SPEC_TEST_SECTION)
PLAN_PATH = f"{PLANNING_DIR}/increments.md"
MAX_PLAN_ATTEMPTS = 2

# Neither session edits the repository: the spec and the plan come back as the final message.
PLANNING_TOOLS: Sequence[str] = ("Read",)

_SPEC_FORMAT = f"""\
Write ONE page (about 60 lines at most), in exactly this shape:

    # <Project name>

    ## Feature list
    The user-visible features, as a short bulleted list.

    ## Not building
    What is deliberately out of scope.

    ## Data model
    A sketch: the entities, their key fields and how they relate.

    ## {SPEC_TEST_SECTION}
    How it is tested. This section MUST contain a fenced code block holding the one shell
    command that installs the project's dependencies and runs its entire test suite from the
    repository root, exiting non-zero when any test fails. Example:

        ```bash
        pip install -e '.[dev]' && pytest -q
        ```

    ## Assumptions
    Every choice you made that the idea did not specify, including the language and framework.

The title is just the project's name (for example `# URL Shortener`), with no word "spec" in it.
Keep it small: this is a utility to be built in a handful of increments, not a product plan."""


def build_spec_prompt(idea: str) -> str:
    return build_prompt(
        "write a one-page spec for the idea",
        None,  # quick mode invokes no BMad skill (FR-2)
        deliverable=f"{MARKDOWN_DELIVERABLE}\n\n{_SPEC_FORMAT}",
        idea=idea,
    )


def check_spec(text: str) -> None:
    """The three sections the build cannot do without, and a usable test command (FR-35)."""
    missing = [
        title for title in SPEC_REQUIRED_SECTIONS if not (section_body(text, title) or "").strip()
    ]
    if missing:
        raise CodeFailure(
            "The spec is missing "
            + ", ".join(f"'## {title}'" for title in missing)
            + ". Add the section(s) with some content."
        )
    extract_test_runner(text, SPEC_TEST_SECTION, "spec")


class SpecPhase(AgentPhase):
    """FR-35: the idea becomes a one-page spec, which the human reviews and may edit."""

    name = "spec"
    artifact_type = ArtifactType.SPEC

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        return build_spec_prompt(ctx.idea)

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        artifact = await super().run(input, ctx)
        check_spec(artifact.content)  # fail fast, before the human spends time on it
        return artifact

    def derived_sections(self, approved: Artifact) -> dict[str, dict[str, Any]]:
        # Validated after the human's edits: a spec with no test command records nothing.
        return {
            "run.test_runner": {
                "command": extract_test_runner(approved.content, SPEC_TEST_SECTION, "spec"),
                "source_hash": approved.sha256_hash,
            }
        }


def build_quick_pipeline(runner: AgentRunner | None = None) -> list[Phase]:
    return [SpecPhase(runner or ClaudeCodeRunner(allowed_tools=PLANNING_TOOLS))]


# ------------------------------------------------------------------------------ increments


@dataclass(frozen=True)
class Increment(Story):
    """One logical unit of the build. A Story with id `Q.N`, so the Pro mode machinery applies."""

    @property
    def id(self) -> str:
        return f"{INCREMENT_PREFIX}.{self.number}"

    @property
    def lock_key(self) -> str:
        return f"quick.story-{self.number}"


_INCREMENT_RE = re.compile(
    r"^(#{2,4})[ \t]+(?:Increment|Story[ \t]+Q\.?)[ \t]*(\d+)[ \t]*[:.\-–—][ \t]*(.+?)[ \t]*$",
    re.MULTILINE,
)
_ANY_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)


def parse_increments(text: str) -> list[Increment]:
    """Increments in document order, numbered 1..N whatever numbers the planner wrote."""
    text = text.replace("\r\n", "\n")
    headings = [(m.start(), len(m.group(1))) for m in _ANY_HEADING_RE.finditer(text)]
    found: list[Increment] = []
    for match in _INCREMENT_RE.finditer(text):
        level = len(match.group(1))
        end = len(text)
        for position, other in headings:
            if position > match.start() and (other <= level or _INCREMENT_RE.match(text, position)):
                end = position
                break
        number = len(found) + 1
        title = match.group(3).strip()
        body = text[match.end() : end].strip().strip("-").strip()
        raw = f"### Increment {number}: {title}" + (f"\n\n{body}" if body else "")
        found.append(Increment(0, number, title, raw))
    return found


_PLAN_FORMAT = """\
Format: one `### Increment N: Title` heading per increment, in the order they are built, each
followed by two to four sentences saying what to build and which tests to write with it.

Rules for the increments:
- Between three and eight of them. Each is one logical unit: the data model, the core logic, the
  interface (routes, commands), configuration, and so on. Never everything in one.
- Each is committed on its own, so after each one the project's whole test suite must pass.
  Tests belong to the increment they test; do not leave them for a final "tests" increment.
- The first sets the project up: the dependency manifest, the test setup and the first module
  with its tests. The repository so far holds only a README, a LICENSE and a .gitignore.
- Only what the spec asks for. Nothing in its "Not building" section."""


def build_plan_prompt(spec: str, test_command: str, problem: str | None = None) -> str:
    deliverable = f"{MARKDOWN_DELIVERABLE}\n\n{_PLAN_FORMAT}"
    sections = {"approved_spec": spec, "test_command": f"`{test_command}`"}
    prompt = build_prompt("cut the spec into ordered increments", None, deliverable, **sections)
    if problem:
        prompt += f"\n## Problem with your previous answer\n\n{problem}\n"
    return prompt


def build_increment_prompt(
    story: Story, spec: str, plan: Sequence[Story], test_command: str
) -> str:
    """The whole task in one file (AD-7): the increment, where it sits in the plan, the spec."""
    outline = "\n".join(
        f"- {item.id} {item.title}"
        + (" (already committed)" if item.number < story.number else " (THIS ONE)" if item.id == story.id else " (later)")
        for item in plan
    )
    return (
        f"# Task: implement increment {story.id}: {story.title}\n\n"
        "You are running non-interactively inside Foundry: no one can answer questions. Where "
        "you would ask, choose the most reasonable answer and note it in your final message.\n\n"
        "Implement the increment below in the repository you are running in (the current "
        "directory). Earlier increments are already committed; read the code on disk to see what "
        "exists.\n\n"
        "Rules:\n"
        "- Write tests for everything this increment adds, and make them pass.\n"
        f"- The project's test command is `{test_command}`; it must exit 0 when you finish, with "
        "every test of the earlier increments still passing.\n"
        "- Do not run `git commit`, `git push` or change branches: Foundry commits for you.\n"
        "- Do not edit `foundry.lock` or anything under `.foundry/`.\n"
        "- Do only this increment; do not start on the later ones.\n"
        "- Your final message is a short summary of what you changed.\n\n"
        "## Increment\n\n"
        f"{story.raw.strip()}\n\n"
        "## All increments\n\n"
        f"{outline}\n\n"
        "## Spec\n\n"
        f"{spec.strip()}\n"
    )


# ---------------------------------------------------------------------------- the lock


@dataclass(frozen=True)
class QuickProgress:
    """How far a quick run has got, from the lock alone."""

    planned: tuple[str, ...]  # "Q.1", "Q.2"... once the increments are planned
    implemented: tuple[str, ...]
    deferred: tuple[str, ...]

    @property
    def planning_done(self) -> bool:
        return bool(self.planned)

    @property
    def remaining(self) -> tuple[str, ...]:
        settled = set(self.implemented) | set(self.deferred)
        return tuple(i for i in self.planned if i not in settled)


def _increment_ids(section: Any) -> tuple[str, ...]:
    quick = section.get("quick") if isinstance(section, dict) else None
    if not isinstance(quick, dict):
        return ()
    numbers = [
        int(key[6:]) for key, value in quick.items()
        if key.startswith("story-") and key[6:].isdigit() and isinstance(value, dict)
    ]
    return tuple(f"{INCREMENT_PREFIX}.{n}" for n in sorted(numbers))


def quick_progress(data: dict[str, Any]) -> QuickProgress:
    plan = data.get("increments") if isinstance(data.get("increments"), dict) else {}
    return QuickProgress(
        planned=tuple(str(i) for i in plan.get("ids", [])),
        implemented=_increment_ids(data.get("implementation")),
        deferred=_increment_ids(data.get("deferred")),
    )


# ---------------------------------------------------------------------------- the stage


class QuickImplementationStage(ImplementationStage):
    """Plan the increments, then build and commit them one by one on `main` (FR-36).

    Everything per increment is inherited from the Pro mode stage: a fresh session (AD-7), the
    test gate (FR-18), one automatic retry and human escalation (Epic 4), and the commit with its
    lock entry (FR-19). There are no epic branches or pull requests: a quick run is one linear
    history on `main`, pushed after every increment.
    """

    name = "implementation"

    def __init__(
        self,
        gitops: GitOps,
        runner: AgentRunner | None = None,
        *,
        planner: AgentRunner | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(gitops, runner, **kwargs)
        # The planning session only reads. Given one runner (tests), it serves both jobs.
        self.planner: AgentRunner = planner or (
            runner if runner is not None else ClaudeCodeRunner(allowed_tools=PLANNING_TOOLS)
        )
        self._plan: tuple[Increment, ...] = ()

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        command = recorder.lock.test_runner()
        if command is None:
            raise EnvFailure("foundry.lock has no [run.test_runner]; there is nothing to gate on.")
        spec = await asyncio.to_thread(read_artifact, ctx.workspace_path, "spec")
        plan = await self.plan_increments(spec, command, ctx, recorder)
        self._plan = tuple(plan)

        for increment in plan:
            progress = quick_progress(recorder.lock.read())
            if increment.id not in progress.remaining:
                continue
            self._check_cancel(ctx, f"increment {increment.id}")
            with self._located(story=increment.id):
                committed = await self.implement_story(increment, spec, command, ctx, recorder)
            if committed:
                await self._guard(
                    f"increment {increment.id}", "push main",
                    lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx,
                )

    async def plan_increments(
        self, spec: str, command: str, ctx: RunContext, recorder: ProvenanceRecorder
    ) -> list[Increment]:
        """The increments the spec is built in: recorded once, then read back from the repo."""
        if "increments" in recorder.lock.read():
            path = ctx.workspace_path / PLAN_PATH
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except OSError as exc:
                raise EnvFailure(f"cannot read the recorded increment plan {path}: {exc}") from exc
            return parse_increments(text)

        if not await self.git.is_clean():
            dirty = ", ".join((await self.git.dirty_files())[:5])
            raise EnvFailure(
                f"cannot plan the increments: the working tree has uncommitted changes ({dirty}). "
                "Commit or discard them first."
            )
        self.say("Planning the increments")
        problem: str | None = None
        for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
            request = AgentRequest(name="increment plan", prompt=build_plan_prompt(spec, command, problem))
            result = await self._backoff("increment plan session", lambda: self.planner.run(request, ctx))
            self.say("")
            increments = parse_increments(result.text)
            if increments:
                break
            problem = (
                "It contained no increments. Answer with `### Increment N: Title` headings, one "
                "per increment, each followed by a short description."
            )
        else:
            raise CodeFailure(
                f"the increment plan had no increments after {MAX_PLAN_ATTEMPTS} attempts. "
                "`foundry quick` starts a new run.",
                {"attempts": MAX_PLAN_ATTEMPTS},
            )

        document = "# Increments\n\n" + "\n\n".join(item.raw for item in increments) + "\n"
        written = self.git.write_files({PLAN_PATH: document})
        await recorder.record_increment_plan(
            {
                "count": len(increments),
                "ids": [item.id for item in increments],
                "titles": [item.title for item in increments],
                "spec_hash": sha256_of(spec),
                "plan_hash": sha256_of(document),
                "attempts": attempt,
            },
            written,
        )
        await self._guard(
            "the increment plan", "push main", lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx
        )
        self.say(f"  {len(increments)} increments: " + "; ".join(f"{i.id} {i.title}" for i in increments))
        return increments

    async def run_session(
        self,
        story: Story,
        architecture: str,  # the spec: quick mode has no architecture document
        test_command: str,
        ctx: RunContext,
        notes: str | None = None,
    ):
        prompt = build_increment_prompt(story, architecture, self._plan, test_command)
        if notes:
            prompt += "\n" + notes
        request = AgentRequest(name=f"story {story.id}", prompt=prompt)
        return await self._backoff(
            f"story {story.id} session", lambda: self.runner.run(request, ctx)
        )


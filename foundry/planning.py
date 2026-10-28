"""The BMad planning phases. Each one is a thin filter around an agent call (AD-1)."""

from __future__ import annotations

import re
from typing import Any

from foundry.agent import AgentRequest, AgentRunner
from foundry.context import Artifact, ArtifactType, CodeFailure, EnvFailure, RunContext
from foundry.epics import validate_plan
from foundry.phases import DefaultHooks

SKILL_BRAINSTORM = "bmad-toolbox:bmad-brainstorming"
SKILL_PRD = "bmad-method:bmad-prd"
SKILL_ARCHITECTURE = "bmad-method:bmad-architecture"

_NON_INTERACTIVE = (
    "You are running non-interactively inside Foundry: no one can answer questions. Where you "
    "would ask the user something, choose the most reasonable answer and list it under an "
    "'Assumptions' heading. Do not write or modify files."
)
MARKDOWN_DELIVERABLE = (
    "Your final message must be the complete deliverable in Markdown and nothing else "
    "(no preamble, no sign-off)."
)


def build_prompt(
    task: str,
    skill: str | None,
    deliverable: str = MARKDOWN_DELIVERABLE,
    **sections: str,
) -> str:
    """Assemble the prompt file: task, skill to use, then every input inlined (AD-7)."""
    parts = [f"# Task: {task}", ""]
    if skill:
        parts += [f"Use the `{skill}` skill.", ""]
    parts += [_NON_INTERACTIVE, deliverable]
    for title, body in sections.items():
        parts += ["", f"## {title.replace('_', ' ').title()}", "", body.strip()]
    return "\n".join(parts) + "\n"


class AgentPhase(DefaultHooks):
    """Shared shape of the agent-backed planning phases: build prompt, run agent, wrap output."""

    name: str
    artifact_type: ArtifactType
    checkpoint = True

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        raise NotImplementedError

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        result = await self.runner.run(
            AgentRequest(name=self.name, prompt=self.prompt_for(input, ctx)), ctx
        )
        text = result.text.strip()
        if not text:
            raise CodeFailure(f"{self.name} agent returned an empty document")
        return Artifact(content=text + "\n", type=self.artifact_type)


class BrainstormPhase(AgentPhase):
    """FR-7: explore the idea with the BMad brainstorm agent."""

    name = "brainstorm"
    artifact_type = ArtifactType.BRAINSTORM

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        return build_prompt(
            "brainstorm", SKILL_BRAINSTORM, idea=input.content
        )


class PrdPhase(AgentPhase):
    """FR-8: turn the approved brainstorm into a PRD."""

    name = "prd"
    artifact_type = ArtifactType.PRD

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        return build_prompt(
            "create a PRD from the brainstorm",
            SKILL_PRD,
            idea=ctx.idea,
            approved_brainstorm=input.content,
        )


# ------------------------------------------------------------------------ architecture

TEST_RUNNER_SECTION = "Test Runner"
_MAX_COMMAND_LENGTH = 500
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_TEST_RUNNER_INSTRUCTIONS = f"""\
The document MUST contain a section headed exactly `## {TEST_RUNNER_SECTION}` whose body is a
single fenced code block holding the one shell command that runs the project's entire test
suite from the repository root and exits non-zero when any test fails. CI runs it on a fresh
checkout, so it must also install the project's dependencies first. Example:

    ## {TEST_RUNNER_SECTION}

    ```bash
    pip install -e '.[dev]' && pytest -q
    ```"""


def extract_test_runner(architecture: str) -> str:
    """Pull the single test command out of the `## Test Runner` section (FR-9).

    Raises CodeFailure with a fix the human can act on; never guesses at multi-line input.
    """
    architecture = architecture.replace("\r\n", "\n")  # Windows editors save CRLF
    section = _section_body(architecture, TEST_RUNNER_SECTION)
    if section is None:
        raise CodeFailure(
            f"The architecture has no '## {TEST_RUNNER_SECTION}' section. Add one containing a "
            "fenced code block with the single command that runs the whole test suite."
        )
    fenced = _FENCE_RE.search(section)
    inline = _INLINE_CODE_RE.search(section)
    raw = fenced.group(2) if fenced else (inline.group(1) if inline else "")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise CodeFailure(
            f"The '## {TEST_RUNNER_SECTION}' section has no command; put it in a fenced code block."
        )
    if len(lines) > 1:
        raise CodeFailure(
            f"The '## {TEST_RUNNER_SECTION}' section has {len(lines)} lines; it must be exactly "
            "one command (chain steps with && if needed)."
        )
    command = lines[0].removeprefix("$ ").strip()
    if not command or len(command) > _MAX_COMMAND_LENGTH or any(ord(c) < 32 for c in command):
        raise CodeFailure(
            f"The test runner command is empty, too long, or has control characters: {command[:80]!r}"
        )
    return command


def _section_body(text: str, title: str) -> str | None:
    """Body of the first heading called `title`, up to the next heading of equal or higher rank."""
    headings = list(_HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        if heading.group(2).strip().lower() != title.lower():
            continue
        level = len(heading.group(1))
        end = len(text)
        for later in headings[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        return text[heading.end() : end]
    return None


SKILL_EPICS = "bmad-method:bmad-create-epics-and-stories"

_EPICS_FORMAT = """\
Format: one `### Epic N: Title` heading per epic, and under it one `### Story N.M: Title`
heading per story (N is the epic number, M the story number within it). Each story has the
user-story sentence ("As a ... I want ... So that ...") followed by acceptance criteria.
Every epic must have at least one story."""


class ArchitecturePhase(AgentPhase):
    """FR-9: design the architecture from the approved PRD and record the test runner."""

    name = "architecture"
    artifact_type = ArtifactType.ARCHITECTURE

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        prd = ctx.upstream.get("prd")
        if prd is None:
            raise EnvFailure("the architecture phase needs the approved PRD")
        return build_prompt(
            "design the architecture for the PRD",
            SKILL_ARCHITECTURE,
            deliverable=f"{MARKDOWN_DELIVERABLE}\n\n{_TEST_RUNNER_INSTRUCTIONS}",
            idea=ctx.idea,
            approved_prd=prd.content,
        )

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        artifact = await super().run(input, ctx)
        extract_test_runner(artifact.content)  # fail fast, before the human spends time on it
        return artifact

    def derived_sections(self, approved: Artifact) -> dict[str, dict[str, Any]]:
        # `[run]` is never rewritten (AD-5), so the command lands in a sub-table appended later.
        return {
            "run.test_runner": {
                "command": extract_test_runner(approved.content),
                "source_hash": approved.sha256_hash,
            }
        }


# ---------------------------------------------------------------------- epics and stories


class EpicsPhase(AgentPhase):
    """FR-10: break the approved architecture (and PRD) into epics and stories."""

    name = "epics"
    artifact_type = ArtifactType.EPICS

    def prompt_for(self, input: Artifact, ctx: RunContext) -> str:
        sections = {"idea": ctx.idea, "approved_architecture": input.content}
        prd = ctx.upstream.get("prd")
        if prd is not None:
            sections["approved_prd"] = prd.content  # so stories can cover every requirement
        return build_prompt(
            "create epics and stories from the architecture",
            SKILL_EPICS,
            deliverable=f"{MARKDOWN_DELIVERABLE}\n\n{_EPICS_FORMAT}",
            **sections,
        )

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        artifact = await super().run(input, ctx)
        validate_plan(artifact.content)  # fail fast on an unusable plan
        return artifact

    def derived_sections(self, approved: Artifact) -> dict[str, dict[str, Any]]:
        # The story manifest that implementation and `foundry resume` work from (AD-6).
        epics = validate_plan(approved.content)
        story_ids = [story_id for epic in epics for story_id in epic.story_ids]
        return {
            "plan": {
                "epic_count": len(epics),
                "story_count": len(story_ids),
                "story_ids": story_ids,
                "source_hash": approved.sha256_hash,
            }
        }

"""A workspace as it is when planning has just finished: lock, approved artifacts on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from foundry.artifacts import write_artifact
from foundry.context import Artifact, ArtifactType, RunContext
from foundry.provenance import ProvenanceRecorder

PRD = "# Todo Tracker PRD\n\nA small tracker for todos.\n"
BRAINSTORM = "# Brainstorm\n\nTodos, but kind.\n"
ARCHITECTURE = (
    "# Architecture\n\nPython 3.12, stdlib only.\n\n## Test Runner\n\n```bash\npytest -q\n```\n"
)
EPICS = """\
# Todo Tracker - Epic Breakdown

### Epic 1: Core Storage

Stores todos.

### Story 1.1: Todo Model

As a user, I want todos, So that I can track work.

**Acceptance Criteria:**

**Given** a title **When** I create a todo **Then** it has an id

### Story 1.2: Persistence

As a user, I want todos saved, So that they survive restarts.

**Acceptance Criteria:**

**Given** todos **When** the app restarts **Then** they are still there

---

### Epic 2: Command Line

### Story 2.1: Add Command

As a user, I want `todo add`, So that I can add from the shell.

**Acceptance Criteria:**

**Given** a title **When** I run `todo add` **Then** it is stored
"""
IDEA = "a todo tracker for the command line"


class ScriptedNamePrompt:
    """NamePrompt that answers from a script (None = accept the suggestion)."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def confirm_name(
        self, suggested: str, *, private: bool, problem: str | None = None
    ) -> str:
        self.calls.append({"suggested": suggested, "private": private, "problem": problem})
        answer = self.answers.pop(0) if self.answers else None
        return suggested if answer is None else answer


async def make_planned(
    workspace: Path, test_command: str = "pytest -q", epics: str = EPICS
) -> tuple[RunContext, ProvenanceRecorder]:
    ctx = RunContext(idea=IDEA, workspace_path=workspace)
    recorder = ProvenanceRecorder(workspace)
    await recorder.record_run_start(ctx)
    for phase, kind, text in (
        ("brainstorm", ArtifactType.BRAINSTORM, BRAINSTORM),
        ("prd", ArtifactType.PRD, PRD),
        ("architecture", ArtifactType.ARCHITECTURE, ARCHITECTURE),
        ("epics", ArtifactType.EPICS, epics),
    ):
        write_artifact(workspace, phase, Artifact(text, kind))
    await recorder.record_derived(
        "run.test_runner", {"command": test_command, "source_hash": "sha256:x"}
    )
    return ctx, recorder


async def make_scaffolded(workspace: Path, test_command: str = "pytest -q", epics: str = EPICS):
    """Planned, then repo-created and scaffolded against the fake GitHub (env must be set)."""
    from foundry.gitops import GitOps
    from foundry.repo import RepoStage

    ctx, recorder = await make_planned(workspace, test_command, epics)
    gitops = GitOps.from_env(workspace, require_token=True)
    recorder = ProvenanceRecorder(workspace, gitops)
    await RepoStage(gitops, ScriptedNamePrompt()).run(ctx, recorder)
    return ctx, recorder, gitops


# ------------------------------------------------------------- driving the implementation loop

import subprocess as _subprocess  # noqa: E402
import sys as _sys  # noqa: E402

from foundry.agent import AgentRequest, AgentResult  # noqa: E402

_COUNT_SCRIPT = (
    "import pathlib\n"
    "n = len(list(pathlib.Path('src').glob('*.py')))\n"
    "print(f'{n} passed')\n"
)


def count_command(directory: Path) -> str:
    """A test command that passes and reports one passed test per file under src/."""
    script = directory / "count_tests.py"
    script.write_text(_COUNT_SCRIPT, encoding="utf-8")
    return _subprocess.list2cmdline([_sys.executable, str(script)])


class ImplementingRunner:
    """AgentRunner that 'implements' a story by writing files, as a Claude Code session would."""

    def __init__(self, files: dict[str, dict[str, str]] | None = None) -> None:
        self.files = files or {}
        self.stories: list[str] = []
        self.prompts: list[str] = []
        self.branches: list[str] = []  # the branch checked out when each session ran
        self.errors: dict[str, BaseException] = {}

    async def run(self, request: AgentRequest, ctx: RunContext) -> AgentResult:
        story_id = request.name.removeprefix("story ")
        self.stories.append(story_id)
        self.prompts.append(request.prompt)
        head = (ctx.workspace_path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        self.branches.append(head.removeprefix("ref: refs/heads/"))
        if story_id in self.errors:
            raise self.errors[story_id]
        for relative, text in (
            self.files.get(story_id) or {f"src/story_{story_id.replace('.', '_')}.py": f"ID = '{story_id}'\n"}
        ).items():
            target = ctx.workspace_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return AgentResult(text=f"implemented {story_id}")


async def no_sleep(seconds: float) -> None:
    return None


def quiet(gitops):
    """Make CI polling instant for a test."""
    gitops.sleep = no_sleep
    gitops.ci_poll_interval = 0
    return gitops

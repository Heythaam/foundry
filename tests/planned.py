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


# ----------------------------------------------------------- failure handling (Epic 4)

import re as _re  # noqa: E402
from collections.abc import Callable  # noqa: E402

BROKEN = "BROKEN"  # a source file containing this word fails the checking command

_CHECK_SCRIPT = (
    "import pathlib, sys\n"
    "files = sorted(pathlib.Path('src').glob('*.py'))\n"
    "bad = [f for f in files if 'BROKEN' in f.read_text(encoding='utf-8')]\n"
    "if bad:\n"
    "    print('FAILED ' + bad[0].name + ': assertion error')\n"
    "    print('1 failed, ' + str(len(files) - len(bad)) + ' passed')\n"
    "    sys.exit(1)\n"
    "print(str(len(files)) + ' passed')\n"
)


def checking_command(directory: Path) -> str:
    """A test command that fails while any file under src/ contains the word BROKEN."""
    script = directory / "check_tests.py"
    script.write_text(_CHECK_SCRIPT, encoding="utf-8")
    return _subprocess.list2cmdline([_sys.executable, str(script)])


def good(story_id: str) -> dict[str, str]:
    return {f"src/story_{story_id.replace('.', '_')}.py": f"ID = '{story_id}'\n"}


def bad(story_id: str) -> dict[str, str]:
    return {f"src/story_{story_id.replace('.', '_')}.py": f"ID = '{story_id}'  # {BROKEN}\n"}


class SequenceRunner:
    """AgentRunner whose Nth session for a story does what the Nth step says.

    A step is a dict of files to write, or an exception to raise. The last step repeats; a
    story with no script writes one good file per session.
    """

    def __init__(self, steps: dict[str, list[dict[str, str] | BaseException]] | None = None) -> None:
        self.steps = steps or {}
        self.sessions: list[tuple[str, str]] = []  # (story id, prompt), in order

    def prompts_for(self, story_id: str) -> list[str]:
        return [prompt for sid, prompt in self.sessions if sid == story_id]

    async def run(self, request: AgentRequest, ctx: RunContext) -> AgentResult:
        story_id = request.name.removeprefix("story ")
        count = len([1 for sid, _ in self.sessions if sid == story_id])
        self.sessions.append((story_id, request.prompt))
        script = self.steps.get(story_id)
        step = good(story_id) if not script else script[min(count, len(script) - 1)]
        if isinstance(step, BaseException):
            raise step
        for relative, text in step.items():
            target = ctx.workspace_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return AgentResult(text=f"session {count + 1} for {story_id}")


def set_action(document: str, action: str) -> str:
    return _re.sub(r"^action:.*$", f"action: {action}", document, flags=_re.MULTILINE)


class Answers:
    """Scripted human for escalations: one answer per time the editor opens (the last repeats).

    An answer is an action word, or a function from the document to the edited document.
    """

    def __init__(self, *answers: str | Callable[[str], str]) -> None:
        self.answers = list(answers)
        self.documents: list[str] = []

    def __call__(self, document: str) -> str:
        self.documents.append(document)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return set_action(document, answer) if isinstance(answer, str) else answer(document)


def with_story_text(action: str, old: str, new: str) -> Callable[[str], str]:
    def edit(document: str) -> str:
        assert old in document, f"{old!r} not in the escalation document"
        return set_action(document.replace(old, new), action)

    return edit


def writing(files: dict[str, str], then: str, workspace: Path) -> Callable[[str], str]:
    """The human edits the code (writes `files` into the workspace), then chooses `then`."""

    def edit(document: str) -> str:
        for relative, text in files.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return set_action(document, then)

    return edit


def instant_backoff(max_retries: int = 5):
    """A backoff policy that records its waits instead of sleeping."""
    from foundry.failures import BackoffPolicy

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    policy = BackoffPolicy(max_retries=max_retries, sleep=sleep, rng=lambda: 1.0)
    policy.waits = waits  # type: ignore[attr-defined]
    return policy


async def started_epic(
    workspace: Path,
    tmp_path: Path,
    *,
    runner,
    failures=None,
    backoff=None,
    command: str | None = None,
    epics: str = EPICS,
):
    """A scaffolded repo with epic 1's branch created and pushed, ready for its stories."""
    from foundry.epics import parse_epics
    from foundry.implementation import ImplementationStage

    ctx, recorder, gitops = await make_scaffolded(
        workspace, command or checking_command(tmp_path), epics
    )
    quiet(gitops)
    stage = ImplementationStage(
        gitops,
        runner,
        failures=failures,
        backoff=backoff,
        on_test_output=lambda text: None,
        say=lambda text: None,
    )
    epic = parse_epics(epics)[0]
    branch = await stage.start_epic(epic, ctx, recorder)
    return ctx, recorder, gitops, stage, epic, branch


# ------------------------------------------------------------------- resume (Epic 5)


class Crash(BaseException):
    """A process dying. A BaseException so no `except Exception` in foundry can tidy up after it."""


class AlwaysAvailable:
    """ModelChecker for a model that is still there."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def check(self, model_id: str):
        from foundry.models import ModelAvailability

        self.asked.append(model_id)
        return ModelAvailability(True)


async def resume_run(
    workspace: Path,
    runner,
    *,
    checker=None,
    prompt=None,
    said: list[str] | None = None,
    finalize: bool = False,
    **stage_kwargs,
):
    """`foundry resume` without the CLI: context from the lock, the stages, the coordinator."""
    from foundry.gitops import GitOps
    from foundry.implementation import ImplementationStage
    from foundry.orchestrator import Orchestrator
    from foundry.repo import RepoStage
    from foundry.resume import ResumeCoordinator, context_from_lock
    from tests.helpers import ScriptedCheckpoint

    ctx = context_from_lock(workspace)
    gitops = quiet(GitOps.from_env(workspace, require_token=True))
    stages = [
        RepoStage(gitops, ScriptedNamePrompt()),
        ImplementationStage(
            gitops, runner, on_test_output=lambda t: None, say=lambda t: None, **stage_kwargs
        ),
    ]
    if finalize:
        from foundry.dockerfile import DockerfileStage

        stages.append(
            DockerfileStage(gitops, runner, on_test_output=lambda t: None, say=lambda t: None)
        )
    coordinator = ResumeCoordinator(
        ctx,
        gitops,
        checker or AlwaysAvailable(),
        prompt,
        say=(said.append if said is not None else (lambda t: None)),
    )
    orchestrator = Orchestrator([], checkpoint=ScriptedCheckpoint(), stages=stages, gitops=gitops)
    result = await orchestrator.resume(ctx, coordinator.prepare)
    return ctx, result, coordinator


class RecordingRunner(ImplementingRunner):
    """Remembers the model each session ran on; consistency passes change nothing by default."""

    def __init__(self, files=None) -> None:
        super().__init__(files)
        self.models: list[tuple[str, str]] = []  # (session name, model id)
        self.consistency_files: dict[str, str] = {}  # written by a consistency pass, if set
        self.consistency_prompts: list[str] = []

    async def run(self, request, ctx):
        self.models.append((request.name, ctx.model_id))
        if request.name == "consistency pass":
            self.consistency_prompts.append(request.prompt)
            for relative, text in self.consistency_files.items():
                target = ctx.workspace_path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return AgentResult(text="normalised")
        return await super().run(request, ctx)


class ScriptedModelPrompt:
    """ModelPrompt that picks a fixed model and records what it was shown."""

    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.shown: list[tuple[str, tuple[str, ...]]] = []

    async def choose(self, unavailable: str, alternatives) -> str:
        self.shown.append((unavailable, tuple(alternatives)))
        return self.choice


class ModelGone:
    """ModelChecker for a retired model, with some alternatives."""

    def __init__(self, alternatives=("claude-sonnet-5-0", "claude-opus-5-0")) -> None:
        self.alternatives = tuple(alternatives)
        self.asked: list[str] = []

    async def check(self, model_id: str):
        from foundry.models import ModelAvailability

        self.asked.append(model_id)
        return ModelAvailability(False, self.alternatives)


# ----------------------------------------------------------------- finalization (Epic 6)

SINGLE_STAGE = """FROM python:3.12-slim
WORKDIR /app
COPY . .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
USER nobody
CMD ["python", "-m", "todo"]
"""

MULTI_STAGE = """FROM node:20-alpine AS build
WORKDIR /src
COPY . .
RUN npm ci && npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=build /src/dist ./dist
EXPOSE 3000
HEALTHCHECK CMD wget -qO- http://localhost:3000/health || exit 1
USER node
CMD ["node", "dist/server.js"]
"""

NODE_PACKAGE = '{"name": "todo", "scripts": {"build": "tsc", "test": "jest"}}\n'


class FinalizingRunner(ImplementingRunner):
    """Writes a Dockerfile when the "dockerfile" session runs, as Claude Code would.

    `dockerfiles` has one step per session (the last repeats): a dict of files to write, where a
    value of None deletes the file, or an exception to raise.
    """

    def __init__(self, dockerfiles=None, files=None, cancel_after: str | None = None) -> None:
        super().__init__(files)
        self.dockerfiles = dockerfiles if dockerfiles is not None else [{"Dockerfile": SINGLE_STAGE}]
        self.dockerfile_prompts: list[str] = []
        self.cancel_after = cancel_after  # ask for a cancel once this story's session has run

    async def run(self, request, ctx):
        if request.name != "dockerfile":
            result = await super().run(request, ctx)
            if self.cancel_after and request.name == f"story {self.cancel_after}":
                from foundry.cancel import request_cancel

                request_cancel(ctx.workspace_path)
            return result
        self.dockerfile_prompts.append(request.prompt)
        step = self.dockerfiles[min(len(self.dockerfile_prompts), len(self.dockerfiles)) - 1]
        if isinstance(step, BaseException):
            raise step
        for relative, text in step.items():
            target = ctx.workspace_path / relative
            if text is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return AgentResult(text="dockerfile written")


# ------------------------------------------------------------------------ quick mode (Epic 7)


def make_spec(test_command: str, title: str = "Link Shortener") -> str:
    """A valid one-page spec whose test strategy runs `test_command`."""
    return (
        f"# {title}\n\n"
        "## Feature list\n- Shorten a URL\n- Resolve a short code\n\n"
        "## Not building\n- Accounts\n\n"
        "## Data model\n- Link(code, url, created_at)\n\n"
        f"## Test strategy\n\nUnit tests for every function.\n\n```bash\n{test_command}\n```\n\n"
        "## Assumptions\n- Python 3.12, standard library only\n"
    )


PLAN = """\
### Increment 1: Data model

Build the Link model and its tests.

### Increment 2: Shortening logic

Build code generation and lookup, with tests.

### Increment 3: Command line

Build the `shorten` command, with tests.
"""


class QuickRunner(FinalizingRunner):
    """One scripted agent for a whole quick run: the spec, the increment plan, the increments
    (as `ImplementingRunner` does) and the Dockerfile."""

    def __init__(self, spec: str | None = None, plans=None, story_steps=None, **kwargs) -> None:
        super().__init__(**kwargs)
        # "Q.2" -> one step per session of that increment (the last repeats): files or an exception
        self.story_steps = dict(story_steps or {})
        self.spec = spec  # filled in with the test command by `quick_run` when left as None
        self.plans = [PLAN] if plans is None else list(plans)
        self.spec_prompts: list[str] = []
        self.plan_prompts: list[str] = []
        self.sessions: list[str] = []  # every session's name, in order
        self.tools_branches: list[str] = []

    async def run(self, request, ctx):
        self.sessions.append(request.name)
        if request.name == "spec":
            self.spec_prompts.append(request.prompt)
            return AgentResult(text=self.spec or "")
        if request.name == "increment plan":
            self.plan_prompts.append(request.prompt)
            step = self.plans[min(len(self.plan_prompts), len(self.plans)) - 1]
            if isinstance(step, BaseException):
                raise step
            return AgentResult(text=step)
        story = request.name.removeprefix("story ")
        if request.name.startswith("story Q.") and story in self.story_steps:
            steps = self.story_steps[story]
            self.prompts.append(request.prompt)
            head = (ctx.workspace_path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
            self.branches.append(head.removeprefix("ref: refs/heads/"))
            step = steps[min(self.sessions.count(request.name), len(steps)) - 1]
            if isinstance(step, BaseException):
                raise step
            for relative, text in step.items():
                target = ctx.workspace_path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return AgentResult(text=f"implemented {story}")
        return await super().run(request, ctx)


async def quick_run(
    workspace: Path,
    tmp_path: Path,
    runner: QuickRunner,
    *,
    command: str | None = None,
    checkpoint=None,
    through: str = "dockerfile",
    idea: str = "a link shortener",
    **stage_kwargs,
):
    """`foundry quick` without the CLI: spec, checkpoint, repo, increments, Dockerfile.

    `through` stops early: "repo" (spec approved, repository scaffolded), "implementation"
    (plus the increments) or "dockerfile" (the whole run).
    """
    from foundry.dockerfile import DockerfileStage
    from foundry.gitops import GitOps
    from foundry.orchestrator import Orchestrator
    from foundry.quick import QuickImplementationStage, build_quick_pipeline
    from foundry.repo import RepoStage
    from tests.helpers import ScriptedCheckpoint

    test_command = command or count_command(tmp_path)
    if runner.spec is None:
        runner.spec = make_spec(test_command)
    gitops = quiet(GitOps.from_env(workspace, require_token=True))
    stages = [RepoStage(gitops, ScriptedNamePrompt())]
    if through in ("implementation", "dockerfile"):
        stages.append(
            QuickImplementationStage(
                gitops, runner, on_test_output=lambda t: None, say=lambda t: None, **stage_kwargs
            )
        )
    if through == "dockerfile":
        stages.append(
            DockerfileStage(gitops, runner, on_test_output=lambda t: None, say=lambda t: None)
        )
    ctx = RunContext(idea=idea, workspace_path=workspace, mode="quick")
    orchestrator = Orchestrator(
        build_quick_pipeline(runner),
        checkpoint=checkpoint or ScriptedCheckpoint(),
        stages=stages,
        gitops=gitops,
    )
    return ctx, await orchestrator.run(ctx)

"""Finalization: the Dockerfile that makes the finished project runnable anywhere (FR-30).

Once every epic is merged, one more Claude Code session writes a production-ready Dockerfile for
the stack the architecture document names. Foundry does not trust the session's word for it: the
Dockerfile is parsed and must have a `FROM`, an `EXPOSE`d port and a `HEALTHCHECK` in its final
stage, and it must be multi-stage exactly when the project has a build step. The project's tests
must still pass (the session may have had to add a health endpoint). Anything short of that goes
back to the session with the list of problems; only a Dockerfile that passes is committed to
`main`, with its entry in `foundry.lock`, and pushed (AD-2, AD-8). The run is marked complete by
the orchestrator once this stage returns.

Foundry has no opinion about where the container runs: no compose file, no fly.toml, no manifests.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from foundry.agent import AgentRequest, AgentRunner, ClaudeCodeRunner, print_to_terminal
from foundry.artifacts import read_artifact
from foundry.cancel import is_cancel_requested
from foundry.context import CodeFailure, EnvFailure, RunContext, sha256_of
from foundry.failures import BackoffPolicy, FailureHandler, RetriesExhausted, with_backoff
from foundry.gitops import MAIN_BRANCH, GitOps
from foundry.implementation import IMPLEMENTATION_TOOLS, HaltOnFailure
from foundry.progress import RunProgress, progress_from_lock
from foundry.provenance import ProvenanceRecorder
from foundry.stages import CancelRequested
from foundry.testgate import DEFAULT_TIMEOUT_SECONDS, GateResult, run_test_command

T = TypeVar("T")

DOCKERFILE = "Dockerfile"
SESSION_NAME = "dockerfile"
MAX_ATTEMPTS = 3  # the first try and two corrections

# Files that tie a project to one place it runs. FR-30: Foundry generates none of them.
DEPLOYMENT_FILES = frozenset(
    {
        "fly.toml",
        "render.yaml",
        "render.yml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "procfile",
        "app.yaml",
        "vercel.json",
        "netlify.toml",
        "railway.json",
        "railway.toml",
        "skaffold.yaml",
    }
)
DEPLOYMENT_DIRS = frozenset({"k8s", "kubernetes", "helm"})


def _say(text: str) -> None:
    print_to_terminal(text + "\n")


# ------------------------------------------------------------------- is there a build step?


@dataclass(frozen=True)
class BuildStep:
    """Whether the project is compiled or bundled before it runs, and what said so."""

    present: bool
    evidence: str


_SKIPPED_DIRS = frozenset(
    {".git", ".foundry", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build"}
)
_MAX_DEPTH = 2  # the root and one level down: enough for `frontend/package.json`

_ARCHITECTURE_BUILD = re.compile(
    r"\b(?:npm|yarn|pnpm|bun) run build\b|\bcargo build\b|\bgo build\b|\bmvn(?:w)? (?:package|install)\b|"
    r"\bgradle(?:w)? build\b|\bdotnet (?:build|publish)\b|\bnext build\b|\bvite build\b|\btsc\b|\bwebpack\b",
    re.IGNORECASE,
)

_COMPILED_MARKERS = (
    ("Cargo.toml", "Cargo.toml: Rust compiles to a binary"),
    ("go.mod", "go.mod: Go compiles to a binary"),
    ("pom.xml", "pom.xml: a Maven project compiles to a jar"),
    ("build.gradle", "build.gradle: a Gradle project compiles to a jar"),
    ("build.gradle.kts", "build.gradle.kts: a Gradle project compiles to a jar"),
    ("tsconfig.json", "tsconfig.json: TypeScript is compiled to JavaScript"),
)


def _project_files(workspace: Path) -> Iterator[tuple[str, Path]]:
    root = Path(workspace)
    for directory, dirs, files in os.walk(root):
        depth = len(Path(directory).relative_to(root).parts)
        dirs[:] = [] if depth >= _MAX_DEPTH else [d for d in dirs if d not in _SKIPPED_DIRS]
        for name in files:
            path = Path(directory) / name
            yield path.relative_to(root).as_posix(), path


def detect_build_step(workspace: Path, architecture: str = "") -> BuildStep:
    """Look for a compile or bundle step in the repository, then in the architecture document.

    A Python or Ruby project that runs from source has none. A `package.json` with a `build`
    script, a compiled language's manifest, a TypeScript config or a Makefile `build` target do.
    """
    for relative, path in _project_files(workspace):
        name = path.name
        if name == "package.json":
            try:
                scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts") or {}
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(scripts, dict) and "build" in scripts:
                return BuildStep(True, f"{relative} defines a build script")
        for marker, evidence in _COMPILED_MARKERS:
            if name == marker:
                return BuildStep(True, evidence.replace(marker, relative, 1))
        if name.endswith((".csproj", ".sln")):
            return BuildStep(True, f"{relative}: a .NET project compiles to assemblies")
        if name == "Makefile":
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if re.search(r"^build\s*:", text, re.MULTILINE):
                return BuildStep(True, f"{relative} has a build target")
    if match := _ARCHITECTURE_BUILD.search(architecture):
        return BuildStep(True, f"the architecture document names `{match.group(0)}`")
    return BuildStep(False, "nothing in the repository or the architecture is compiled or bundled")


# ------------------------------------------------------------------ reading a Dockerfile


@dataclass(frozen=True)
class DockerfileInfo:
    stages: int
    base_images: tuple[str, ...]  # one per stage, in order
    ports: tuple[int, ...]  # EXPOSEd in the final stage
    healthcheck: str | None  # the final stage's HEALTHCHECK, if it has a real one

    @property
    def base_image(self) -> str:
        return self.base_images[-1] if self.base_images else ""

    @property
    def multi_stage(self) -> bool:
        return self.stages > 1


def _logical_lines(text: str) -> Iterator[str]:
    """Instructions as Docker reads them: comments dropped, backslash continuations joined."""
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1].rstrip() + " "
            continue
        yield (current + line).strip()
        current = ""
    if current.strip():
        yield current.strip()


_VARIABLE = re.compile(r"\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)")
_ASSIGNMENT = re.compile(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S*)")


def _defaults(instructions: Sequence[tuple[str, str]]) -> dict[str, str]:
    """`ARG PORT=8000` and `ENV PORT=8000` (or `ENV PORT 8000`): what `$PORT` stands for."""
    values: dict[str, str] = {}
    for keyword, rest in instructions:
        if keyword not in ("ARG", "ENV"):
            continue
        pairs = _ASSIGNMENT.findall(rest)
        if pairs:
            values.update({key: value.strip("\"'") for key, value in pairs})
        elif keyword == "ENV" and len(rest.split(None, 1)) == 2:
            key, value = rest.split(None, 1)
            values[key] = value.strip("\"'")
    return values


def _resolve(token: str, values: dict[str, str]) -> str:
    def swap(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        return values.get(name, match.group(2) or "")

    return _VARIABLE.sub(swap, token)


def inspect_dockerfile(text: str) -> DockerfileInfo:
    instructions: list[tuple[str, str]] = []
    for line in _logical_lines(text):
        parts = line.split(None, 1)
        instructions.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    values = _defaults(instructions)

    images: list[str] = []
    final: list[tuple[str, str]] = []
    for keyword, rest in instructions:
        if keyword == "FROM":
            words = [word for word in rest.split() if not word.startswith("--")]
            images.append(_resolve(words[0], values) if words else "")
            final = []
        elif images:
            final.append((keyword, rest))

    ports: list[int] = []
    healthcheck: str | None = None
    for keyword, rest in final:
        if keyword == "EXPOSE":
            for token in rest.split():
                number = _resolve(token, values).split("/")[0]
                if re.fullmatch(r"\d+(?:-\d+)?", number):
                    ports.append(int(number.split("-")[0]))
        elif keyword == "HEALTHCHECK":
            words = rest.split()
            if words and words[0].upper() != "NONE" and any(w.upper() == "CMD" for w in words):
                healthcheck = rest
            elif words and words[0].upper() == "NONE":
                healthcheck = None
    return DockerfileInfo(len(images), tuple(images), tuple(ports), healthcheck)


def check_dockerfile(info: DockerfileInfo, build: BuildStep) -> list[str]:
    """What is wrong with a Dockerfile, in words the session can act on. Empty means fine."""
    if info.stages == 0:
        return ["the Dockerfile has no FROM instruction"]
    problems: list[str] = []
    if not info.ports:
        problems.append(
            "the final stage has no EXPOSE with a port number (EXPOSE in an earlier stage does "
            "not count); declare the port the application listens on"
        )
    if info.healthcheck is None:
        problems.append(
            "the final stage has no HEALTHCHECK with a CMD (a HEALTHCHECK in an earlier stage is "
            "not inherited, and HEALTHCHECK NONE does not count)"
        )
    if build.present and not info.multi_stage:
        problems.append(
            f"the project has a build step ({build.evidence}), so the Dockerfile must be "
            "multi-stage: build in one stage and copy only the output into a slim final stage"
        )
    if not build.present and info.multi_stage:
        problems.append(
            "the project has no build step, so the Dockerfile must be a single stage "
            f"(it has {info.stages})"
        )
    return problems


def deployment_files(paths: Sequence[str]) -> list[str]:
    """Paths that belong to one hosting platform rather than to the project."""
    found = []
    for path in paths:
        parts = path.replace("\\", "/").lower().split("/")
        if parts[-1] in DEPLOYMENT_FILES or DEPLOYMENT_DIRS & set(parts[:-1]):
            found.append(path)
    return found


# ------------------------------------------------------------------------------ the prompt


def build_dockerfile_prompt(
    architecture: str,
    test_command: str,
    build: BuildStep,
    problems: Sequence[str] = (),
    source: str = "architecture",
) -> str:
    """The whole task in one file (AD-7), plus what was wrong with the last try, if there was one."""
    if build.present:
        shape = (
            f"This project HAS a build step ({build.evidence}). Use a MULTI-STAGE build: compile or "
            "bundle in a builder stage, then copy only the output into a slim final stage."
        )
    else:
        shape = (
            f"This project has NO build step ({build.evidence}). Use a SINGLE-STAGE build: one "
            "`FROM`, no builder stage."
        )
    prompt = (
        "# Task: write the project's Dockerfile\n\n"
        "You are running non-interactively inside Foundry: no one can answer questions. Where you "
        "would ask, choose the most reasonable answer and note it in your final message.\n\n"
        "Everything this project was asked to do is implemented and merged. Read the code in the "
        "current directory and write a production-ready `Dockerfile` at its root, for the tech "
        f"stack the {source} below names.\n\n"
        "Requirements:\n"
        f"- {shape}\n"
        "- Use a standard, version-pinned base image for the language and framework (never "
        "`latest`); install dependencies before copying the source so layers cache; run as a "
        "non-root user; keep dev tooling and test files out of the final image.\n"
        "- `EXPOSE` the port the application listens on, in the FINAL stage.\n"
        "- Add a `HEALTHCHECK ... CMD ...` in the FINAL stage. It must call a real health endpoint "
        "(`/health` or equivalent) using only what the final image contains. If the application "
        "has no such endpoint, add one, with a test.\n"
        "- A `.dockerignore` is fine. Do NOT create anything that ties the project to a hosting "
        "platform: no docker-compose, fly.toml, render.yaml, Procfile or Kubernetes manifests.\n"
        f"- The project's test command is `{test_command}`; it must exit 0 when you finish.\n"
        "- Do not run `git commit`, `git push` or change branches: Foundry commits for you.\n"
        "- Do not edit `foundry.lock` or anything under `.foundry/`.\n"
        "- Your final message is a short summary of the image: base image, port, health endpoint.\n"
    )
    if problems:
        listed = "\n".join(f"- {problem}" for problem in problems)
        prompt += (
            "\n## Problems with your previous attempt\n\n"
            "Your files from that attempt are still in the working tree. Fix these and nothing "
            f"else:\n\n{listed}\n"
        )
    return prompt + f"\n## {source.capitalize()}\n\n{architecture.strip()}\n"


# ------------------------------------------------------------------------------- the stage


@dataclass
class _Verdict:
    problems: list[str]
    info: DockerfileInfo | None = None
    text: str = ""
    gate: GateResult | None = None
    files: tuple[str, ...] = ()


class DockerfileStage:
    name = "finalization"

    def __init__(
        self,
        gitops: GitOps,
        runner: AgentRunner | None = None,
        failures: FailureHandler | None = None,
        backoff: BackoffPolicy | None = None,
        test_timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        on_test_output: Callable[[str], None] = print_to_terminal,
        say: Callable[[str], None] = _say,
    ) -> None:
        self.git = gitops
        self.runner = runner or ClaudeCodeRunner(allowed_tools=IMPLEMENTATION_TOOLS)
        self.failures: FailureHandler = failures or HaltOnFailure()
        self.backoff = backoff or BackoffPolicy.none()
        self.test_timeout = test_timeout
        self.on_test_output = on_test_output
        self.say = say

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        progress = progress_from_lock(recorder.lock.read())
        if progress is None:
            raise EnvFailure("foundry.lock records no run.")
        if progress.dockerfile is not None:
            # Generated before a restart. Nothing to redo; make sure main reached the remote.
            await self._publish(ctx)
            return
        quick = ctx.mode == "quick"
        if quick:
            self._require_finished_increments(recorder.lock.read())
        else:
            self._require_finished_epics(progress)
        command = recorder.lock.test_runner()
        if command is None:
            raise EnvFailure("foundry.lock has no [run.test_runner]; there is nothing to gate on.")
        workspace = ctx.workspace_path
        source = "spec" if quick else "architecture"  # the document that names the tech stack
        architecture = await asyncio.to_thread(read_artifact, workspace, source)
        await self._require_main()

        build = await asyncio.to_thread(detect_build_step, workspace, architecture)
        self.say(f"Finalization: writing the Dockerfile ({'multi-stage' if build.present else 'single-stage'}: {build.evidence})")

        problems: list[str] = []
        verdict = _Verdict(["no attempt was made"])
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if is_cancel_requested(workspace):
                raise CancelRequested("dockerfile generation")
            prompt = build_dockerfile_prompt(architecture, command, build, problems, source)
            request = AgentRequest(name=SESSION_NAME, prompt=prompt)
            await self._guard(
                "the Dockerfile", "dockerfile session", lambda: self.runner.run(request, ctx), ctx
            )
            self.say("")
            verdict = await self._verify(command, build, ctx)
            if not verdict.problems:
                break
            problems = verdict.problems
            for problem in problems:
                self.say(f"  Dockerfile problem: {problem.splitlines()[0]}")
            if attempt < MAX_ATTEMPTS:
                self.say(f"  Asking for a correction (attempt {attempt + 1} of {MAX_ATTEMPTS})")
        else:
            await self.git.stash("foundry: dockerfile attempt discarded")
            raise CodeFailure(
                f"the Dockerfile still has problems after {MAX_ATTEMPTS} attempts:\n"
                + "\n".join(f"- {problem}" for problem in verdict.problems)
                + "\nIts files are in `git stash`. `foundry resume` tries again.",
                {"attempts": MAX_ATTEMPTS, "problems": list(verdict.problems)},
            )

        info = verdict.info
        assert info is not None and verdict.gate is not None
        fields = {
            "base_image": info.base_image,
            "base_images": list(info.base_images),
            "stages": info.stages,
            "multi_stage": info.multi_stage,
            "build_step": build.present,
            "build_step_evidence": build.evidence,
            "port": info.ports[0],
            "ports": list(info.ports),
            "healthcheck": info.healthcheck,
            "artifact_hash": sha256_of(verdict.text),
            "attempts": attempt,
            "files": list(verdict.files),
            "model": ctx.model_id,
        }
        if verdict.gate.tests_passed is not None:
            fields["tests_passed"] = verdict.gate.tests_passed
        await recorder.record_dockerfile(fields)
        await self._publish(ctx)
        self.say(f"Dockerfile committed: {info.base_image}, port {info.ports[0]}")

    # -------------------------------------------------------------------- checks

    @staticmethod
    def _require_finished_epics(progress: RunProgress) -> None:
        open_epics = [e for e in progress.started_epics if e not in progress.merged_epics]
        if progress.remaining or open_epics:
            what = (
                f"story {progress.remaining[0]} is not done"
                if progress.remaining
                else f"epic {open_epics[0]} is not merged"
            )
            raise EnvFailure(
                f"the Dockerfile is generated after every epic is merged, but {what}.",
                fix="Run `foundry resume` to finish the implementation first.",
            )

    @staticmethod
    def _require_finished_increments(data: dict) -> None:
        from foundry.quick import quick_progress  # quick imports the implementation stage

        progress = quick_progress(data)
        if not progress.planning_done or progress.remaining:
            what = (
                f"increment {progress.remaining[0]} is not done"
                if progress.remaining
                else "the increments have not been planned"
            )
            raise EnvFailure(
                f"the Dockerfile is generated after every increment is committed, but {what}.",
                fix="Quick runs cannot be resumed; run `foundry quick` again in a new directory.",
            )

    async def _require_main(self) -> None:
        branch = await self.git.current_branch()
        if branch != MAIN_BRANCH:
            raise EnvFailure(
                f"the Dockerfile is committed to {MAIN_BRANCH}, but the working tree is on {branch!r}.",
                fix=f"Check out {MAIN_BRANCH} (with every epic merged), then run `foundry resume`.",
            )
        if not await self.git.is_clean():
            # A session that was interrupted: its half-written files are set aside, not lost.
            await self.git.stash("foundry: dockerfile attempt discarded on resume")
            self.say("  Set aside an unfinished Dockerfile attempt in `git stash`; starting it again")

    async def _verify(self, command: str, build: BuildStep, ctx: RunContext) -> _Verdict:
        """Parse what the session left, check the rules, then run the tests (it may have added code)."""
        workspace = ctx.workspace_path
        path = workspace / DOCKERFILE
        if not path.is_file():
            return _Verdict([f"there is no file named {DOCKERFILE} at the repository root"])
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        info = inspect_dockerfile(text)
        problems = check_dockerfile(info, build)

        await self.git.stage_all()
        files = tuple(await self.git.staged_files())
        if elsewhere := deployment_files(files):
            problems.append(
                "remove the platform-specific files " + ", ".join(elsewhere)
                + ": Foundry produces a Dockerfile only"
            )
        if "foundry.lock" in files:
            problems.append("foundry.lock was edited; restore it, Foundry owns that file")
        if problems:
            return _Verdict(problems, info, text, None, files)

        gate = await run_test_command(
            command, workspace, timeout=self.test_timeout, on_output=self.on_test_output
        )
        if not gate.passed:
            return _Verdict(
                [f"the project's tests fail after your changes (exit {gate.exit_code}):\n{gate.tail()}"],
                info, text, gate, files,
            )
        return _Verdict([], info, text, gate, files)

    # ------------------------------------------------------------ external calls

    async def _publish(self, ctx: RunContext) -> None:
        await self._guard(
            "the Dockerfile", "push main", lambda: self.git.push(MAIN_BRANCH, set_upstream=False), ctx
        )

    async def _guard(
        self, scope: str, what: str, call: Callable[[], Awaitable[T]], ctx: RunContext
    ) -> T:
        """Backoff on infrastructure errors; when that gives up, ask the failure handler."""
        while True:
            try:
                return await with_backoff(what, call, self.backoff, self.say)
            except RetriesExhausted as exhausted:
                await self.failures.on_infra_exhausted(scope, exhausted, ctx, None)

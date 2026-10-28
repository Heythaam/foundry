"""The repo transition (AD-9): planning is done, so create the GitHub repo and scaffold it.

Runs once, after the epics are approved. The name is derived from the project, confirmed with
the human, and only then does anything touch GitHub (FR-14). Every git and GitHub mutation is
a GitOps call (AD-8); this module decides what and in which order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from foundry.artifacts import read_artifact
from foundry.context import EnvFailure, RunContext, sha256_of
from foundry.gitops import MAIN_BRANCH, GitOps, RepoExists, RepoInfo
from foundry.provenance import ProvenanceRecorder
from foundry.scaffold import PLANNING_PHASES, project_name, scaffold_files, slugify

MAX_NAME_ATTEMPTS = 3


@runtime_checkable
class NamePrompt(Protocol):
    async def confirm_name(self, suggested: str, *, private: bool, problem: str | None = None) -> str:
        """Return the repository name to use: `suggested`, or what the human typed instead."""
        ...


class TerminalNamePrompt:
    async def confirm_name(self, suggested: str, *, private: bool, problem: str | None = None) -> str:
        visibility = "private" if private else "public"
        question = (
            f"Create {visibility} GitHub repository '{suggested}'? "
            "Press Enter to accept, or type another name: "
        )

        def ask() -> str:
            if problem:
                print(problem)
            return input(question)

        try:
            answer = await asyncio.to_thread(ask)
        except EOFError as exc:
            raise EnvFailure(
                "cannot ask you to confirm the repository name: standard input is closed. "
                "Run foundry from an interactive terminal."
            ) from exc
        return slugify(answer) if answer.strip() else suggested


class RepoStage:
    """Derive and confirm a name, create the repo, commit the scaffold, push `main`."""

    name = "repo"

    def __init__(
        self, gitops: GitOps, prompt: NamePrompt | None = None, private: bool = False
    ) -> None:
        self.git = gitops
        self.prompt = prompt or TerminalNamePrompt()
        self.private = private

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None:
        lock = recorder.lock
        if lock.has_section("scaffold"):
            return  # already transitioned; nothing to redo
        if lock.has_section("repo"):
            raise EnvFailure(
                "a repository was created for this run but its scaffold was not committed; "
                "finishing that is the job of `foundry resume`."
            )
        command = lock.test_runner()
        if command is None:
            raise EnvFailure(
                "foundry.lock has no [run.test_runner]; the architecture must be approved "
                "with a '## Test Runner' section before a repository is created."
            )

        documents = {
            phase: await asyncio.to_thread(read_artifact, ctx.workspace_path, phase)
            for phase in PLANNING_PHASES
        }
        title = project_name(documents, ctx.idea)
        holder = await self.git.user_name() or f"{title} authors"
        files = scaffold_files(
            name=title,
            idea=ctx.idea,
            test_command=command,
            documents=documents,
            license_holder=holder,
            year=ctx.start_time.year,
        )
        self._refuse_to_overwrite(ctx.workspace_path, files)

        await self.git.prepare_for_scaffold()
        info = await self._create_repo(slugify(title), ctx.idea)
        await recorder.record_repo(info)

        written = self.git.write_files(files)
        hashes = {path: sha256_of(files[path]) for path in written}
        await recorder.record_scaffold(MAIN_BRANCH, hashes, written)

        await self.git.add_remote(info.clone_url)
        await self.git.push(MAIN_BRANCH)

    async def _create_repo(self, suggested: str, idea: str) -> RepoInfo:
        problem: str | None = None
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            name = slugify(await self.prompt.confirm_name(suggested, private=self.private, problem=problem))
            try:
                return await self.git.create_repo(name, idea, private=self.private)
            except RepoExists:
                if attempt == MAX_NAME_ATTEMPTS:
                    raise
                problem = f"'{name}' is already taken on your GitHub account. Pick another name."
                suggested = name
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _refuse_to_overwrite(workspace: Path, files: dict[str, str]) -> None:
        clashes = [path for path in files if (workspace / path).exists()]
        if clashes:
            raise EnvFailure(
                "the scaffold would overwrite existing files: " + ", ".join(sorted(clashes)) +
                ". Run foundry in an empty directory."
            )

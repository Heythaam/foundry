"""Typer entry point. Thin dispatch only: no pipeline logic lives here.

Heavy modules (the orchestrator, agent runner, lock/sqlite stack) are imported inside the
commands that need them so `foundry --help` stays well under the 2 second budget (NFR-1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from foundry.context import FoundryError, RunContext

if TYPE_CHECKING:
    from foundry.gitops import GitOps
    from foundry.phases import Phase
    from foundry.stages import Stage

app = typer.Typer(
    name="foundry",
    help="Take an idea through a human-checkpointed planning and build pipeline.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,  # plain help: skips importing Rich, ~0.8s of startup
)


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=1)


def _fail_with(err: FoundryError) -> typer.Exit:
    """Report an error. An environment failure says what to fix; one that stopped a run and
    was saved to the lock also says how to continue (FR-24)."""
    from foundry.context import FailureCategory
    from foundry.failures import GENERIC_FIX, describe_halt, fix_hint

    saved = err.context.get("halted")
    if saved is True:
        return _fail(describe_halt(err))
    if err.category is FailureCategory.ENVIRONMENT:
        hint = fix_hint(err)
        message = err.message if hint == GENERIC_FIX else f"{err.message}\nTo fix: {hint}"
        if saved is False:
            message += "\n(The failure could not be saved to foundry.lock.)"
        return _fail(message)
    return _fail(err.message)


def _default_phases() -> list[Phase]:
    from foundry.pipeline import build_pipeline

    return build_pipeline()


def _build_stages(workspace: Path, private: bool) -> tuple[list[Stage], GitOps | None]:
    """The post-planning stages and the one GitOps they share. Fails before any planning work
    is spent if GITHUB_TOKEN is missing (NFR-6)."""
    from foundry.failures import BackoffPolicy, RetryingFailureHandler
    from foundry.gitops import GitOps
    from foundry.implementation import ImplementationStage
    from foundry.repo import RepoStage

    gitops = GitOps.from_env(workspace, require_token=True)
    implementation = ImplementationStage(
        gitops, failures=RetryingFailureHandler(), backoff=BackoffPolicy.from_env()
    )
    return [RepoStage(gitops, private=private), implementation], gitops


@app.command()
def pro(
    idea: str = typer.Argument(..., help="The idea to take through the full pipeline."),
    review_epics: bool = typer.Option(
        False, "--review-epics", help="Pause for review before each epic starts implementation."
    ),
    private: bool = typer.Option(
        False, "--private", help="Create the GitHub repository as private (default: public)."
    ),
) -> None:
    """Start a full planning-pipeline run with mandatory human checkpoints."""
    from foundry.orchestrator import Orchestrator, RunStatus

    ctx = RunContext(idea=idea, review_epics=review_epics)
    try:
        stages, gitops = _build_stages(ctx.workspace_path, private)
        orchestrator = Orchestrator(_default_phases(), stages=stages, gitops=gitops)
        result = asyncio.run(orchestrator.run(ctx))
    except FoundryError as err:
        raise _fail_with(err) from err
    if result.status is RunStatus.CANCELLED:
        raise _fail(f"Run {ctx.run_id} cancelled.")
    typer.echo(f"Run {ctx.run_id} finished.")


@app.command()
def quick(idea: str = typer.Argument(..., help="The idea to build in Quick mode.")) -> None:
    """Start a Quick mode run that skips planning (not implemented yet)."""
    raise _fail("`foundry quick` is not implemented yet.")


@app.command()
def resume() -> None:
    """Continue a previously interrupted run (not implemented yet)."""
    raise _fail("`foundry resume` is not implemented yet.")


@app.command()
def status() -> None:
    """Show the state of the current run (not implemented yet)."""
    raise _fail("`foundry status` is not implemented yet.")


@app.command()
def cancel() -> None:
    """Cleanly stop an in-progress run after its current phase."""
    from foundry.cancel import request_cancel

    try:
        requested = request_cancel(Path.cwd())
    except FoundryError as err:
        raise _fail(err.message) from err
    if not requested:
        raise _fail("No run in progress.")
    typer.echo("Cancel requested. The run will stop after the current phase.")

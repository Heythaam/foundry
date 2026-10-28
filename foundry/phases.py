"""The Phase protocol (AD-1) and the placeholder phase the skeleton pipeline runs."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from foundry.context import Artifact, ArtifactType, RunContext


@runtime_checkable
class Phase(Protocol):
    """A filter in the pipes-and-filters pipeline.

    Phases receive an artifact and return an artifact. They never call other phases;
    the orchestrator owns sequencing, checkpoints and provenance.
    """

    name: str
    checkpoint: bool  # whether the orchestrator opens a checkpoint after this phase

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact: ...


@runtime_checkable
class PhaseHooks(Protocol):
    """Optional capabilities the orchestrator uses when a phase provides them.

    A phase never talks to provenance or other phases itself; it only describes, from the
    artifact the human approved, what should be recorded and whether upstream needs rework.
    """

    # Name of the phase whose output is this phase's `input`, if the human can send the
    # pipeline back to edit it (pre-flight -> "prd").
    revise_target: str | None

    def checkpoint_fields(self, approved: Artifact) -> dict[str, Any]:
        """Extra fields merged into this phase's `[checkpoints.<name>]` section."""
        ...

    def derived_sections(self, approved: Artifact) -> dict[str, dict[str, Any]]:
        """Extra lock sections (dotted name -> fields) to append once approved."""
        ...

    def requests_revision(self, approved: Artifact) -> bool:
        """True when the approved artifact says upstream must be reworked and re-run."""
        ...


class DefaultHooks:
    """No-op hooks; subclass and override what the phase needs."""

    revise_target: str | None = None

    def checkpoint_fields(self, approved: Artifact) -> dict[str, Any]:
        return {}

    def derived_sections(self, approved: Artifact) -> dict[str, dict[str, Any]]:
        return {}

    def requests_revision(self, approved: Artifact) -> bool:
        return False


class StubPhase:
    """Placeholder phase so the pipeline skeleton can be exercised end to end."""

    checkpoint = False

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        return Artifact(
            content=f"# {self.name}\n\nIdea: {ctx.idea}\n\nInput hash: {input.sha256_hash}\n",
            type=ArtifactType.STUB,
        )

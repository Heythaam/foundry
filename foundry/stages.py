"""Stages: the post-planning steps the orchestrator runs after the phase pipeline.

Phases are pure artifact filters (AD-1). Repo creation and implementation mutate git and
GitHub, so they are stages: they get the recorder and the run context, and the orchestrator
still owns their sequencing, cancel polling and error classification.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from foundry.context import RunContext
from foundry.provenance import ProvenanceRecorder


class CancelRequested(Exception):
    """Raised by a stage that saw the cancel sentinel at a safe point between units of work."""

    def __init__(self, before: str) -> None:
        super().__init__(before)
        self.before = before


@runtime_checkable
class Stage(Protocol):
    name: str

    async def run(self, ctx: RunContext, recorder: ProvenanceRecorder) -> None: ...

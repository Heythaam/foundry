"""Shared fakes for the planning-phase tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from foundry.agent import AgentRequest, AgentResult
from foundry.checkpoint import CheckpointResult
from foundry.context import Artifact, RunContext, utc_now


class FakeRunner:
    """AgentRunner that replays scripted responses (the last one repeats)."""

    def __init__(self, *responses: str | BaseException) -> None:
        self.responses = list(responses)
        self.requests: list[AgentRequest] = []
        self.contexts: list[RunContext] = []

    async def run(self, request: AgentRequest, ctx: RunContext) -> AgentResult:
        self.requests.append(request)
        self.contexts.append(ctx)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, BaseException):
            raise response
        return AgentResult(text=response)

    @property
    def prompts(self) -> list[str]:
        return [request.prompt for request in self.requests]


class ScriptedCheckpoint:
    """CheckpointHandler that approves, optionally rewriting named phases' content.

    `edits` maps a phase name (as passed in metadata, e.g. "prd" or "prd.revision-1") to a
    function from presented text to approved text. `presented` records what the human saw.
    """

    def __init__(self, edits: Mapping[str, Callable[[str], str]] | None = None) -> None:
        self.edits = dict(edits or {})
        self.presented: list[tuple[str, str]] = []
        self.metadata: list[Mapping[str, Any]] = []

    async def present(
        self, artifact: Artifact, metadata: Mapping[str, Any] | None = None
    ) -> CheckpointResult:
        phase = str((metadata or {}).get("phase", ""))
        self.presented.append((phase, artifact.content))
        self.metadata.append(dict(metadata or {}))
        edit = self.edits.get(phase)
        approved = Artifact(edit(artifact.content), artifact.type) if edit else artifact
        now = utc_now()
        return CheckpointResult(approved, artifact.sha256_hash, now, now)

    @property
    def phases(self) -> list[str]:
        return [phase for phase, _ in self.presented]

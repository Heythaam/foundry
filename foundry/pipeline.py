"""The planning pipeline in order: brainstorm -> PRD -> pre-flight -> ..."""

from __future__ import annotations

from foundry.agent import AgentRunner, ClaudeCodeRunner
from foundry.phases import Phase
from foundry.planning import ArchitecturePhase, BrainstormPhase, EpicsPhase, PrdPhase
from foundry.preflight import PreflightPhase


def build_pipeline(runner: AgentRunner | None = None) -> list[Phase]:
    runner = runner or ClaudeCodeRunner()
    return [
        BrainstormPhase(runner),
        PrdPhase(runner),
        PreflightPhase(runner),
        ArchitecturePhase(runner),
        EpicsPhase(runner),
    ]

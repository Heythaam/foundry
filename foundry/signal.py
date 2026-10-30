"""Phase boundary signal — Story 8.3.

Prints what just completed and what comes next at every phase boundary, then waits
for a single keypress unless `--yes` was passed or stdin is not a TTY.
"""

from __future__ import annotations

import sys

from foundry.context import RunContext


class PhaseSignal:
    """Print a completed/upcoming summary at a phase boundary.

    Parameters
    ----------
    completed:
        Name of the phase that just finished.
    outcome:
        Short description of what it produced (e.g. "5 stories approved").
    upcoming:
        Name of the next phase.
    description:
        What the next phase will produce (e.g. "architecture document").
    ctx:
        The active RunContext; `ctx.yes` and stdin TTY detection control the wait.
    """

    def present(
        self,
        completed: str,
        outcome: str,
        upcoming: str,
        description: str,
        ctx: RunContext,
    ) -> None:
        print(f"  ✓ {completed} — {outcome}", flush=True)
        print(f"  → {upcoming}: {description}", flush=True)

        if ctx.yes or not sys.stdin.isatty():
            return

        try:
            input("")
        except EOFError:
            pass  # piped stdin — proceed without blocking
        except KeyboardInterrupt:
            raise

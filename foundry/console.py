"""Rich console and visual output for Foundry."""

from __future__ import annotations

import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import print as rprint

console = Console()


def print_phase_start(phase_name: str, detail: str = "") -> None:
    """Prints a bold blue panel header when a new phase starts.

    Example: ╭─ Phase: Brainstorming ──────────────────────╮
             │  Applying first principles...               │
    """
    content = detail if detail else ""
    panel = Panel(
        content,
        title=f"[bold blue]Phase: {phase_name}[/bold blue]",
        title_align="left",
        border_style="blue",
    )
    console.print(panel)


_last_agent: str | None = None
_at_start_of_line: bool = True


def print_agent_chunk(agent_name: str, text: str, color: str = "yellow") -> None:
    """Prints streaming text from an agent with a colored label prefix.

    [brainstorm-agent] text here...  (yellow)
    [user-agent] text here...        (cyan)
    [implementation] text here...    (green)
    """
    global _last_agent, _at_start_of_line

    if color == "yellow":
        if "user" in agent_name.lower():
            color = "cyan"
        elif "impl" in agent_name.lower() or "story" in agent_name.lower():
            color = "green"

    if not text:
        return

    parts = text.splitlines(keepends=True)
    for part in parts:
        if _at_start_of_line or _last_agent != agent_name:
            prefix = Text()
            prefix.append(f"[{agent_name}] ", style=color)
            console.print(prefix, end="")
            _last_agent = agent_name
            _at_start_of_line = False
        console.print(part, end="", markup=False)
        if part.endswith(("\n", "\r")):
            _at_start_of_line = True
            _last_agent = None

    if hasattr(console.file, "flush"):
        console.file.flush()


def print_technique_separator(technique_name: str) -> None:
    """Prints a dim separator line between techniques.

    ── Technique: Kill the Crown Jewel ──
    """
    console.rule(f"[dim]Technique: {technique_name}[/dim]", style="dim")


def print_phase_complete(phase_name: str) -> None:
    """Prints a green ✓ confirmation when a phase completes.

    ✓ Brainstorming complete
    """
    console.print(f"[bold green]✓[/bold green] [green]{phase_name} complete[/green]")


def print_story_start(story_id: str, title: str, epic_num: int, story_num: int, total: int) -> None:
    """Prints story start indicator.

    ● Epic 2 · Story 2.3 of 8: "Add user authentication"
    """
    console.print(
        f"[bold cyan]●[/bold cyan] [bold]Epic {epic_num}[/bold] · Story {story_id} of {total}: \"{title}\""
    )


def print_test_running() -> None:
    """Shows a subtle indicator that tests are running.

    ⟳ Running tests...
    """
    console.print("[dim]⟳ Running tests...[/dim]")


def print_test_result(passed: bool, output: str = "") -> None:
    """Prints test result.

    ✓ Tests passed   (green)
    ✗ Tests failed   (red) + first few lines of output
    """
    if passed:
        console.print("[bold green]✓ Tests passed[/bold green]")
    else:
        console.print("[bold red]✗ Tests failed[/bold red]")
        if output:
            sample = output[:10000]
            lines = sample.strip().splitlines()[:5]
            for line in lines:
                truncated = line[:300] + ("..." if len(line) > 300 else "")
                console.print(f"  {truncated}", style="dim red", markup=False)


def print_error(category: str, message: str, fix: str = "") -> None:
    """Prints a red error panel with category label and optional fix instruction.

    ╭─ Environment Failure ───────────────────────────────╮
    │ GITHUB_TOKEN is not set                             │
    │ Fix: export GITHUB_TOKEN=your_token                 │
    """
    content = message
    if fix:
        content += f"\nFix: {fix}"
    panel = Panel(
        content,
        title=f"[bold red]{category}[/bold red]",
        title_align="left",
        border_style="red",
    )
    console.print(panel)


def print_status(progress_data: dict) -> None:
    """Rich version of the current render_status() output.

    Uses a clean table layout instead of plain text.
    """
    run_id = progress_data.get("run_id", "")
    console.print(f"[bold]Run {run_id}[/bold]")

    table = Table(box=None, show_header=False, padding=(0, 0))
    table.add_column("Property", style="bold cyan")
    table.add_column("Value")

    if "idea" in progress_data:
        table.add_row("  Idea:        ", str(progress_data["idea"]))
    if progress_data.get("mode") == "quick":
        table.add_row("  Mode:        ", "quick")
    status_text = progress_data.get("status_text") or progress_data.get("status")
    if status_text:
        table.add_row("  Status:      ", str(status_text))
    phase_text = progress_data.get("phase_text") or progress_data.get("phase")
    if phase_text:
        table.add_row("  Phase:       ", str(phase_text))
    if "stories_text" in progress_data:
        table.add_row("  Stories:     ", str(progress_data["stories_text"]))
    if "increments_text" in progress_data:
        table.add_row("  Increments:  ", str(progress_data["increments_text"]))
    if "epics_text" in progress_data:
        table.add_row("  Epics:       ", str(progress_data["epics_text"]))
    if "dockerfile_text" in progress_data:
        table.add_row("  Dockerfile:  ", str(progress_data["dockerfile_text"]))
    if "model" in progress_data:
        table.add_row("  Model:       ", str(progress_data["model"]))
    if "started_at" in progress_data:
        table.add_row("  Started:     ", str(progress_data["started_at"]))
    elapsed_text = progress_data.get("elapsed_text") or progress_data.get("elapsed")
    if elapsed_text:
        table.add_row("  Elapsed:     ", str(elapsed_text))
    if "last_action_text" in progress_data:
        table.add_row("  Last action: ", str(progress_data["last_action_text"]))
    if "history_text" in progress_data:
        table.add_row("  History:     ", str(progress_data["history_text"]))
    if "stopped_text" in progress_data:
        table.add_row("  Stopped:     ", str(progress_data["stopped_text"]))
    if "to_fix_text" in progress_data:
        table.add_row("  To fix:      ", str(progress_data["to_fix_text"]))

    console.print(table)

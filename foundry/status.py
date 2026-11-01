"""`foundry status`: a plain summary of a run, read from foundry.lock (FR-4, NFR-9).

It reads only the recorded state, never the in-flight state, so it is accurate to within one
story and safe to run while `foundry pro` or `foundry resume` is working in another terminal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from foundry.progress import (
    PHASE_DONE,
    PHASE_FINALIZATION,
    PHASE_IMPLEMENTATION,
    PHASE_PLANNING,
    PHASE_REPO,
    RunProgress,
    parse_timestamp,
)

NO_RUN_MESSAGE = "No Foundry run found in this directory."


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _status_line(progress: RunProgress) -> str:
    if progress.status == "completed":
        return "completed"
    if progress.status == "cancelled":
        return "cancelled (resume with `foundry resume`)"
    if progress.last_halt:
        return "stopped by an environment failure (fix it, then `foundry resume`)"
    return "in progress (or interrupted: `foundry resume` continues it)"


def _phase_line(progress: RunProgress) -> str:
    if progress.phase == PHASE_DONE:
        return "finished"
    if progress.phase == PHASE_PLANNING:
        after = f" (last approved: {progress.last_artifact})" if progress.last_artifact else ""
        return f"planning{after}"
    if progress.phase == PHASE_REPO:
        return "creating the repository"
    if progress.phase == PHASE_FINALIZATION:
        if progress.dockerfile:
            return "finalization: recording completion"
        return "finalization: generating the Dockerfile"
    assert progress.phase == PHASE_IMPLEMENTATION
    if progress.next_story:
        return f"implementation, epic {progress.next_epic}, next story {progress.next_story}"
    open_epics = [e for e in progress.started_epics if e not in progress.merged_epics]
    if open_epics:
        return f"implementation, epic {open_epics[0]}: opening or merging its pull request"
    return "implementation: wrapping up"


def render_status(progress: RunProgress, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [
        f"Run {progress.run_id}",
        f"  Idea:        {progress.idea}",
        f"  Status:      {_status_line(progress)}",
        f"  Phase:       {_phase_line(progress)}",
    ]
    if progress.plan_complete:
        extras = []
        if progress.deferred:
            extras.append(f"{len(progress.deferred)} deferred")
        if progress.skipped_in_review:
            extras.append(f"{len(progress.skipped_in_review)} skipped in review")
        suffix = f" ({', '.join(extras)})" if extras else ""
        lines.append(
            f"  Stories:     {progress.completed_count} of {progress.stories_total} completed{suffix}"
        )
        lines.append(f"  Epics:       {len(progress.merged_epics)} of {progress.epics_total} merged")
    if progress.dockerfile:
        image, port = progress.dockerfile.get("base_image"), progress.dockerfile.get("port")
        lines.append(f"  Dockerfile:  generated ({image}, port {port})")
    model = f"  Model:       {progress.model}"
    if progress.pending_model_change:
        change = progress.pending_model_change
        model += f" (changed from {change.original_model}; a consistency pass follows the current epic)"
    lines.append(model)
    lines.append(f"  Started:     {progress.started_at}")
    lines.append(f"  Elapsed:     {format_duration(progress.elapsed_seconds)}")
    if progress.last_action_at:
        age = (now - parse_timestamp(progress.last_action_at)).total_seconds()
        lines.append(f"  Last action: {progress.last_action_at} ({format_duration(age)} ago)")
    if progress.resumes or progress.cancellations:
        lines.append(
            f"  History:     resumed {progress.resumes} time(s), cancelled {progress.cancellations} time(s)"
        )
    if progress.last_halt:
        halt = progress.last_halt
        lines.append(f"  Stopped:     {halt.get('message', '')}")
        if halt.get("fix"):
            lines.append(f"  To fix:      {halt['fix']}")
    return "\n".join(lines)


def _quick_phase_line(data: dict, progress: RunProgress) -> str:
    from foundry.quick import quick_progress

    if progress.status == "completed":
        return "finished"
    if "spec" not in (data.get("artifacts") or {}):
        return "writing the spec (it opens in your editor for review)"
    if "scaffold" not in data:
        return "creating the repository"
    quick = quick_progress(data)
    if not quick.planning_done:
        return "planning the increments"
    if quick.remaining:
        return f"implementation, next increment {quick.remaining[0]}"
    if progress.dockerfile:
        return "finalization: recording completion"
    return "finalization: generating the Dockerfile"


def render_quick_status(data: dict, progress: RunProgress, now: datetime | None = None) -> str:
    """The same summary for a `foundry quick` run, whose steps are increments, not epics."""
    from foundry.quick import quick_progress

    now = now or datetime.now(timezone.utc)
    quick = quick_progress(data)
    status = _status_line(progress)
    if progress.status == "cancelled":
        status = "cancelled (a quick run cannot be resumed)"
    lines = [
        f"Run {progress.run_id}",
        f"  Idea:        {progress.idea}",
        "  Mode:        quick",
        f"  Status:      {status}",
        f"  Phase:       {_quick_phase_line(data, progress)}",
    ]
    if quick.planning_done:
        extra = f" ({len(quick.deferred)} deferred)" if quick.deferred else ""
        lines.append(
            f"  Increments:  {len(quick.implemented)} of {len(quick.planned)} committed{extra}"
        )
    if progress.dockerfile:
        image, port = progress.dockerfile.get("base_image"), progress.dockerfile.get("port")
        lines.append(f"  Dockerfile:  generated ({image}, port {port})")
    lines.append(f"  Model:       {progress.model}")
    lines.append(f"  Started:     {progress.started_at}")
    lines.append(f"  Elapsed:     {format_duration(progress.elapsed_seconds)}")
    if progress.last_action_at:
        age = (now - parse_timestamp(progress.last_action_at)).total_seconds()
        lines.append(f"  Last action: {progress.last_action_at} ({format_duration(age)} ago)")
    if progress.last_halt:
        halt = progress.last_halt
        lines.append(f"  Stopped:     {halt.get('message', '')}")
        if halt.get("fix"):
            lines.append(f"  To fix:      {halt['fix']}")
    return "\n".join(lines)


def rich_render_status(
    progress: RunProgress, data: dict | None = None, now: datetime | None = None
) -> None:
    """Rich version of render_status using console.print_status."""
    from foundry.console import print_status

    now = now or datetime.now(timezone.utc)
    if progress.mode == "quick":
        from foundry.quick import quick_progress

        quick = quick_progress(data or {}) if data else None
        status = _status_line(progress)
        if progress.status == "cancelled":
            status = "cancelled (a quick run cannot be resumed)"
        pdata: dict[str, Any] = {
            "run_id": progress.run_id,
            "idea": progress.idea,
            "mode": "quick",
            "status_text": status,
            "phase_text": _quick_phase_line(data or {}, progress) if data else _phase_line(progress),
        }
        if quick and quick.planning_done:
            extra = f" ({len(quick.deferred)} deferred)" if quick.deferred else ""
            pdata["increments_text"] = (
                f"{len(quick.implemented)} of {len(quick.planned)} committed{extra}"
            )
        if progress.dockerfile:
            image, port = progress.dockerfile.get("base_image"), progress.dockerfile.get("port")
            pdata["dockerfile_text"] = f"generated ({image}, port {port})"
        pdata["model"] = progress.model
        pdata["started_at"] = progress.started_at
        pdata["elapsed_text"] = format_duration(progress.elapsed_seconds)
        if progress.last_action_at:
            age = (now - parse_timestamp(progress.last_action_at)).total_seconds()
            pdata["last_action_text"] = f"{progress.last_action_at} ({format_duration(age)} ago)"
        if progress.last_halt:
            halt = progress.last_halt
            pdata["stopped_text"] = halt.get("message", "")
            if halt.get("fix"):
                pdata["to_fix_text"] = halt["fix"]
        print_status(pdata)
        return

    pdata = {
        "run_id": progress.run_id,
        "idea": progress.idea,
        "status_text": _status_line(progress),
        "phase_text": _phase_line(progress),
    }
    if progress.plan_complete:
        extras = []
        if progress.deferred:
            extras.append(f"{len(progress.deferred)} deferred")
        if progress.skipped_in_review:
            extras.append(f"{len(progress.skipped_in_review)} skipped in review")
        suffix = f" ({', '.join(extras)})" if extras else ""
        pdata["stories_text"] = (
            f"{progress.completed_count} of {progress.stories_total} completed{suffix}"
        )
        pdata["epics_text"] = f"{len(progress.merged_epics)} of {progress.epics_total} merged"
    if progress.dockerfile:
        image, port = progress.dockerfile.get("base_image"), progress.dockerfile.get("port")
        pdata["dockerfile_text"] = f"generated ({image}, port {port})"
    model = progress.model
    if progress.pending_model_change:
        change = progress.pending_model_change
        model += f" (changed from {change.original_model}; a consistency pass follows the current epic)"
    pdata["model"] = model
    pdata["started_at"] = progress.started_at
    pdata["elapsed_text"] = format_duration(progress.elapsed_seconds)
    if progress.last_action_at:
        age = (now - parse_timestamp(progress.last_action_at)).total_seconds()
        pdata["last_action_text"] = f"{progress.last_action_at} ({format_duration(age)} ago)"
    if progress.resumes or progress.cancellations:
        pdata["history_text"] = (
            f"resumed {progress.resumes} time(s), cancelled {progress.cancellations} time(s)"
        )
    if progress.last_halt:
        halt = progress.last_halt
        pdata["stopped_text"] = halt.get("message", "")
        if halt.get("fix"):
            pdata["to_fix_text"] = halt["fix"]
    print_status(pdata)

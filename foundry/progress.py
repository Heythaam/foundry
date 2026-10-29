"""Where a run stands, worked out from foundry.lock alone (FR-4, FR-32).

`foundry resume` and `foundry status` both need to answer "what is done and what is next?".
They answer it the same way, from the committed record: the story manifest in `[plan]`, the
`[implementation.*]` entries, the stories a human deferred or skipped in review, and the epics
that merged. Pure functions over the parsed lock: no git, no network, nothing to mock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from foundry.lock import _numbered, cancellation_count, status_of

PHASE_PLANNING = "planning"
PHASE_REPO = "repo"
PHASE_IMPLEMENTATION = "implementation"
PHASE_DONE = "done"

_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def story_key(story_id: str) -> tuple[int, int]:
    epic, _, number = story_id.partition(".")
    return int(epic), int(number)


@dataclass(frozen=True)
class PendingModelChange:
    """A model change whose consistency pass has not run yet (FR-34)."""

    through_change: int  # the newest change this pass will cover
    original_model: str  # the model before the first uncovered change
    new_model: str
    boundary_story: str
    stories_completed: int
    stories_total: int


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    idea: str
    status: str  # in_progress | cancelled | completed
    phase: str  # planning | repo | implementation | done
    model: str
    started_at: str
    review_epics: bool
    last_action_at: str | None
    elapsed_seconds: float
    plan_complete: bool
    stories_total: int
    implemented: tuple[str, ...]  # story ids, in manifest order
    deferred: tuple[str, ...]
    skipped_in_review: tuple[str, ...]
    remaining: tuple[str, ...]
    epics_total: int
    merged_epics: tuple[int, ...]
    started_epics: tuple[int, ...]
    last_halt: dict[str, Any] | None = None  # the newest halt, if the run has not progressed since
    pending_model_change: PendingModelChange | None = None
    resumes: int = 0
    cancellations: int = 0
    last_artifact: str | None = field(default=None)  # the latest approved planning phase

    @property
    def next_story(self) -> str | None:
        return self.remaining[0] if self.remaining else None

    @property
    def next_epic(self) -> int | None:
        return story_key(self.remaining[0])[0] if self.remaining else None

    @property
    def completed_count(self) -> int:
        return len(self.implemented)


def _timestamps(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [stamp for value in node.values() for stamp in _timestamps(value)]
    if isinstance(node, str) and _TIMESTAMP.match(node):
        return [node]
    return []


def parse_timestamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _story_ids(section: Any) -> list[str]:
    """`{"epic-1": {"story-2": {...}}}` -> ["1.2"]."""
    found: list[str] = []
    if isinstance(section, dict):
        for epic_key, stories in section.items():
            if not (epic_key.startswith("epic-") and isinstance(stories, dict)):
                continue
            for story_key_, value in stories.items():
                if story_key_.startswith("story-") and isinstance(value, dict):
                    found.append(f"{epic_key[5:]}.{story_key_[6:]}")
    return found


def pending_model_change(data: dict[str, Any]) -> PendingModelChange | None:
    changes = _numbered(data.get("model_changes"), "change-")
    if not changes:
        return None
    covered = max(
        (int(p.get("through_change", 0)) for _, p in _numbered(data.get("consistency"), "pass-")),
        default=0,
    )
    uncovered = [(n, c) for n, c in changes if n > covered]
    if not uncovered:
        return None
    first = uncovered[0][1]
    return PendingModelChange(
        through_change=uncovered[-1][0],
        original_model=str(first.get("from", "")),
        new_model=str(uncovered[-1][1].get("to", "")),
        boundary_story=str(first.get("boundary_story", "")),
        stories_completed=int(first.get("stories_completed", 0)),
        stories_total=int(first.get("stories_total", 0)),
    )


def _current_model(data: dict[str, Any]) -> str:
    changes = _numbered(data.get("model_changes"), "change-")
    if changes and changes[-1][1].get("to"):
        return str(changes[-1][1]["to"])
    return str(data.get("run", {}).get("model", ""))


def progress_from_lock(data: dict[str, Any], now: datetime | None = None) -> RunProgress | None:
    """None when the lock records no run at all."""
    status = status_of(data)
    if status is None:
        return None
    run = data["run"]
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    manifest = [str(s) for s in plan.get("story_ids", [])]
    manifest.sort(key=story_key)

    implemented = {s for s in _story_ids(data.get("implementation"))}
    deferred = {s for s in _story_ids(data.get("deferred"))}
    skipped: set[str] = set()
    for key, checkpoint in (data.get("checkpoints") or {}).items():
        if key.endswith("-review") and isinstance(checkpoint, dict):
            skipped.update(str(s) for s in checkpoint.get("skipped", []))

    epics = {k: v for k, v in (data.get("epics") or {}).items() if k.startswith("epic-") and isinstance(v, dict)}
    merged = sorted(int(k[5:]) for k, v in epics.items() if "merged" in v)
    started = sorted(int(k[5:]) for k, v in epics.items() if "branch" in v)

    plan_complete = bool(manifest) and isinstance(run.get("test_runner"), dict)
    settled = implemented | deferred | skipped
    remaining = tuple(s for s in manifest if s not in settled)

    if status == "completed":
        phase = PHASE_DONE
    elif not plan_complete:
        phase = PHASE_PLANNING
    elif "scaffold" not in data:
        phase = PHASE_REPO
    else:
        phase = PHASE_IMPLEMENTATION

    stamps = _timestamps(data)
    last = max(stamps) if stamps else None
    started_at = str(run.get("started_at", ""))
    if status == "completed":
        end = parse_timestamp(str(data["completed"].get("completed_at", last)))
    elif status == "cancelled" and last:
        end = parse_timestamp(last)
    else:
        end = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (end - parse_timestamp(started_at)).total_seconds()) if started_at else 0.0

    halts = _numbered(data.get("halts"), "halt-")
    last_halt = None
    if halts and status == "in_progress":
        newest = halts[-1][1]
        halted_at = str(newest.get("halted_at", ""))
        since = [
            stamp
            for section in ("implementation", "deferred", "epics", "resumes")
            for stamp in _timestamps(data.get(section))
            if stamp > halted_at
        ]
        if not since:  # nothing has happened since the run stopped
            last_halt = dict(newest)

    approved = [p for p in ("brainstorm", "prd", "preflight", "architecture", "epics") if p in (data.get("artifacts") or {})]
    return RunProgress(
        run_id=str(run.get("run_id", "")),
        idea=str(run.get("idea", "")),
        status=status,
        phase=phase,
        model=_current_model(data),
        started_at=started_at,
        review_epics=bool(run.get("review_epics", False)),
        last_action_at=last,
        elapsed_seconds=elapsed,
        plan_complete=plan_complete,
        stories_total=int(plan.get("story_count", len(manifest)) or 0),
        implemented=tuple(sorted(implemented, key=story_key)),
        deferred=tuple(sorted(deferred, key=story_key)),
        skipped_in_review=tuple(sorted(skipped, key=story_key)),
        remaining=remaining,
        epics_total=int(plan.get("epic_count", 0) or 0),
        merged_epics=tuple(merged),
        started_epics=tuple(started),
        last_halt=last_halt,
        pending_model_change=pending_model_change(data),
        resumes=len(_numbered(data.get("resumes"), "resume-")),
        cancellations=cancellation_count(data),
        last_artifact=approved[-1] if approved else None,
    )

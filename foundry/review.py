"""`--review-epics`: a checkpoint before each epic starts implementation (FR-6).

The implementation loop (Epic 3) calls `EpicReviewer.before_epic()` as it is about to begin an
epic. Without `--review-epics` that is a no-op: no checkpoint, no lock entry, no commit.

With it, the human sees the epic's stories plus the current project state in `$EDITOR` and can
  * reorder: move a whole story section up or down;
  * skip:    delete a whole story section (recorded as skipped, never silently lost);
  * edit:    change a story's text in place.
Stories cannot be added or renumbered: the manifest in `[plan]` is the source of truth.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from foundry.checkpoint import CheckpointHandler
from foundry.context import Artifact, ArtifactType, EnvFailure, RunContext, classify_exception
from foundry.epics import Epic, parse_stories
from foundry.provenance import ProvenanceRecorder

_MARKER_RE = re.compile(r"<!--\s*foundry:epic-review\b.*?-->", re.DOTALL)


class ReviewError(EnvFailure):
    """The reviewed document cannot be applied to the epic."""


@dataclass(frozen=True)
class ProjectState:
    """Where the project stands when an epic is about to start."""

    completed_story_ids: Sequence[str] = field(default_factory=tuple)
    merged_epics: Sequence[int] = field(default_factory=tuple)
    remaining_epics: Sequence[int] = field(default_factory=tuple)


@dataclass(frozen=True)
class EpicReview:
    epic: Epic  # what the loop should now implement: reordered, edited, skipped ones removed
    reviewed: bool
    skipped: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    reordered: bool = False


def render_review(epic: Epic, state: ProjectState) -> str:
    done = ", ".join(state.completed_story_ids) or "none yet"
    merged = ", ".join(map(str, state.merged_epics)) or "none yet"
    remaining = ", ".join(map(str, state.remaining_epics)) or "none"
    marker = json.dumps({"epic": epic.number, "stories": epic.story_ids})
    lines = [
        f"# Review before Epic {epic.number}: {epic.title}",
        "",
        "## Project state",
        "",
        f"- Completed stories ({len(state.completed_story_ids)}): {done}",
        f"- Merged epics: {merged}",
        f"- Epics after this one: {remaining}",
        f"- This epic: {len(epic.stories)} stories",
        "",
        "## How to review",
        "",
        "- **Reorder**: move a whole `### Story` section up or down.",
        "- **Skip**: delete a whole `### Story` section. It is recorded as skipped.",
        "- **Edit**: change a story's text in place.",
        "- Do not add stories or change story IDs. Everything above the first `### Story` is ignored.",
        "",
        f"<!-- foundry:epic-review {marker} -->",
        "",
    ]
    return "\n".join(lines) + "\n" + "\n\n".join(story.raw for story in epic.stories) + "\n"


def apply_review(text: str, original: Epic) -> EpicReview:
    """Turn the edited review document back into an Epic, validating that nothing was invented."""
    text = _MARKER_RE.sub("", text)
    stories = parse_stories(text)  # duplicate IDs raise CodeFailure here

    originals = {story.id: story for story in original.stories}
    for story in stories:
        if story.id not in originals:
            raise ReviewError(
                f"Story {story.id} is not part of epic {original.number}. A review can reorder, "
                "skip or edit the stories shown, but not add or renumber them."
            )

    kept_ids = [story.id for story in stories]
    skipped = tuple(sid for sid in original.story_ids if sid not in kept_ids)
    edited = tuple(story.id for story in stories if story.raw != originals[story.id].raw)
    expected_order = [sid for sid in original.story_ids if sid in kept_ids]
    return EpicReview(
        epic=Epic(original.number, original.title, tuple(stories)),
        reviewed=True,
        skipped=skipped,
        edited=edited,
        reordered=kept_ids != expected_order,
    )


class EpicReviewer:
    def __init__(
        self, checkpoint: CheckpointHandler, recorder: ProvenanceRecorder, enabled: bool
    ) -> None:
        self.checkpoint = checkpoint
        self.recorder = recorder
        self.enabled = enabled

    @classmethod
    def for_run(
        cls, ctx: RunContext, checkpoint: CheckpointHandler, recorder: ProvenanceRecorder
    ) -> EpicReviewer:
        return cls(checkpoint, recorder, enabled=ctx.review_epics)

    async def before_epic(self, epic: Epic, state: ProjectState) -> EpicReview:
        if not self.enabled:
            return EpicReview(epic=epic, reviewed=False)

        document = Artifact(render_review(epic, state), ArtifactType.EPIC_REVIEW)
        name = f"epic-{epic.number}-review"
        try:
            result = await self.checkpoint.present(
                document, {"phase": name, "base_phase": name, "revision": 0}
            )
        except Exception as exc:  # classify before propagating (AD-3)
            raise classify_exception(exc) from exc

        review = apply_review(result.artifact.content, epic)  # validate before recording
        await self.recorder.record_checkpoint(
            name,
            result,
            {
                "epic": epic.number,
                "story_order": [story.id for story in review.epic.stories],
                "skipped": list(review.skipped),
                "edited": list(review.edited),
                "reordered": review.reordered,
                "completed_stories": len(state.completed_story_ids),
            },
        )
        return review

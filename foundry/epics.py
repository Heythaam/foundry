"""Parse an epics-and-stories Markdown document into Epic and Story objects.

Recognised headings (any level from `##` to `####`):
    ### Epic 2: Planning Pipeline
    ### Story 2.3: Pre-flight Analysis Phase
A story belongs to the epic named by the first number of its ID. Everything after a story
heading, up to the next heading of the same or higher rank (or the next Epic/Story heading),
is that story's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from foundry.context import CodeFailure

_SEP = r"\s*[:.\-–—]\s*"
_EPIC_RE = re.compile(rf"^(#{{2,4}})[ \t]+Epic[ \t]+(\d+){_SEP}(.+?)[ \t]*$", re.MULTILINE)
_STORY_RE = re.compile(rf"^(#{{2,4}})[ \t]+Story[ \t]+(\d+)\.(\d+){_SEP}(.+?)[ \t]*$", re.MULTILINE)
_ANY_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)
_TRAILING_RULE_RE = re.compile(r"(?:\n[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*)+\s*$")


@dataclass(frozen=True)
class Story:
    epic: int
    number: int
    title: str
    raw: str  # the heading line and body, exactly as written (minus trailing rules/blank lines)

    @property
    def id(self) -> str:
        return f"{self.epic}.{self.number}"

    @property
    def lock_key(self) -> str:
        """Where this story's records live in the lock: `epic-1.story-2`."""
        return f"epic-{self.epic}.story-{self.number}"


@dataclass(frozen=True)
class Epic:
    number: int
    title: str
    stories: tuple[Story, ...]

    @property
    def story_ids(self) -> list[str]:
        return [story.id for story in self.stories]


def parse_stories(text: str) -> list[Story]:
    """Every story in document order. Duplicate IDs are an error, not a silent merge."""
    text = text.replace("\r\n", "\n")
    headings = [(m.start(), len(m.group(1))) for m in _ANY_HEADING_RE.finditer(text)]
    stories: list[Story] = []
    seen: set[str] = set()
    for match in _STORY_RE.finditer(text):
        level = len(match.group(1))
        end = len(text)
        for position, other_level in headings:
            if position <= match.start():
                continue
            starts_new_item = bool(
                _EPIC_RE.match(text, position) or _STORY_RE.match(text, position)
            )
            if other_level <= level or starts_new_item:
                end = position
                break
        raw = _TRAILING_RULE_RE.sub("", text[match.start() : end]).strip()
        story = Story(int(match.group(2)), int(match.group(3)), match.group(4).strip(), raw)
        if story.id in seen:
            raise CodeFailure(f"Story {story.id} appears more than once in the epics document.")
        seen.add(story.id)
        stories.append(story)
    return stories


def parse_epics(text: str) -> list[Epic]:
    """Epics in order of first appearance, each with its stories in document order."""
    text = text.replace("\r\n", "\n")
    titles: dict[int, str] = {}
    for match in _EPIC_RE.finditer(text):
        titles.setdefault(int(match.group(2)), match.group(3).strip())

    by_epic: dict[int, list[Story]] = {}
    for story in parse_stories(text):
        by_epic.setdefault(story.epic, []).append(story)

    order = sorted(set(titles) | set(by_epic))
    return [
        Epic(number, titles.get(number, f"Epic {number}"), tuple(by_epic.get(number, ())))
        for number in order
    ]


def validate_plan(text: str) -> list[Epic]:
    """Parse and require a usable plan: at least one epic, every epic with a story."""
    epics = parse_epics(text)
    if not epics:
        raise CodeFailure(
            "The document contains no epics or stories. Use headings like "
            "'### Epic 1: Title' and '### Story 1.1: Title'."
        )
    empty = [epic.number for epic in epics if not epic.stories]
    if empty:
        raise CodeFailure(
            "Epic(s) " + ", ".join(map(str, empty)) + " have no stories. Every epic needs at "
            "least one '### Story N.M: Title' section."
        )
    return epics

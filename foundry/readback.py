"""Spec interpretation readback — Story 8.1.

Prints a structured interpretation of the run's idea before any phase executes so the
developer can see and approve what Foundry inferred before it writes anything.

Inferred fields are marked with `~`; directly-stated facts are unmarked.
Story count is always TBD at this point because spec/planning has not run yet.
"""

from __future__ import annotations

import re
import sys

from foundry.context import EnvFailure, RunContext

# ------------------------------------------------------------------ keyword tables

_LANG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpython\b", re.IGNORECASE), "Python"),
    (re.compile(r"\bflask\b", re.IGNORECASE), "Python / Flask"),
    (re.compile(r"\bfastapi\b", re.IGNORECASE), "Python / FastAPI"),
    (re.compile(r"\bdjango\b", re.IGNORECASE), "Python / Django"),
    (re.compile(r"\bexpress\b", re.IGNORECASE), "Node.js / Express"),
    (re.compile(r"\breact\b", re.IGNORECASE), "JavaScript / React"),
    (re.compile(r"\bnext(?:\.?js)?\b", re.IGNORECASE), "JavaScript / Next.js"),
    (re.compile(r"\bvue\b", re.IGNORECASE), "JavaScript / Vue"),
    (re.compile(r"\brust\b", re.IGNORECASE), "Rust"),
    (re.compile(r"\bgo\b|\bgolang\b", re.IGNORECASE), "Go"),
    (re.compile(r"\bjava\b", re.IGNORECASE), "Java"),
    (re.compile(r"\bruby\b|\brails\b", re.IGNORECASE), "Ruby"),
    (re.compile(r"\bphp\b", re.IGNORECASE), "PHP"),
    (re.compile(r"\btypescript\b|\bts\b", re.IGNORECASE), "TypeScript"),
    (re.compile(r"\bnode(?:\.?js)?\b", re.IGNORECASE), "Node.js"),
]

_RUNNER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpytest\b", re.IGNORECASE), "pytest"),
    (re.compile(r"\bjest\b", re.IGNORECASE), "jest"),
    (re.compile(r"\bvitest\b", re.IGNORECASE), "vitest"),
    (re.compile(r"\bgo\s+test\b", re.IGNORECASE), "go test"),
    (re.compile(r"\bmocha\b", re.IGNORECASE), "mocha"),
    (re.compile(r"\brspec\b", re.IGNORECASE), "rspec"),
]

# ------------------------------------------------------------------ helpers

_STRIP_STOP_WORDS = re.compile(
    r"\b(a|an|the|build|create|make|write|develop|implement|design|"
    r"simple|basic|small|quick|new|my|our|using|with|for|in|on|of|to)\b",
    re.IGNORECASE,
)


def _extract_project_name(idea: str) -> tuple[str, bool]:
    """Return (name, inferred).  `inferred=True` when we had to guess."""
    # Strip leading imperative verbs and stop words, take first meaningful phrase.
    cleaned = _STRIP_STOP_WORDS.sub(" ", idea).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    words = cleaned.split()
    if words:
        # Capitalise the first word; keep up to 4 words for the name.
        name = " ".join(words[:4])
        return name.capitalize(), True
    return idea.strip()[:40] or "unnamed project", True


def _detect_language(idea: str) -> tuple[str | None, bool]:
    """Return (language_string, inferred).  None when nothing is recognised."""
    for pattern, label in _LANG_PATTERNS:
        if pattern.search(idea):
            # If the user literally stated the name in the idea it is a fact.
            return label, False
    return None, True


def _detect_runner(idea: str) -> tuple[str | None, bool]:
    """Return (runner_string, inferred)."""
    for pattern, label in _RUNNER_PATTERNS:
        if pattern.search(idea):
            return label, False
    return None, True


# ------------------------------------------------------------------ readback

class SpecReadback:
    """Print a structured interpretation of `ctx.idea` and optionally wait for approval.

    Fields inferred (not literally stated in the idea) are marked with `~`.
    Story count is always unknown at this stage.
    """

    def present(self, ctx: RunContext) -> None:
        """Print the readback and (unless `ctx.yes`) wait for the user to press Enter.

        Raises `EnvFailure` with message "run aborted at readback" on Ctrl-C.
        """
        name, name_inferred = _extract_project_name(ctx.idea)
        language, lang_inferred = _detect_language(ctx.idea)
        runner, runner_inferred = _detect_runner(ctx.idea)

        def _f(value: str | None, inferred: bool, fallback: str = "not detected") -> str:
            v = value if value is not None else fallback
            return f"~{v}" if inferred else v

        lines = [
            "",
            "Foundry understood your idea as:",
            f"  Project name   : {_f(name, name_inferred)}",
            f"  Language/stack : {_f(language, lang_inferred)}",
            f"  Test runner    : {_f(runner, runner_inferred)}",
            "  Story count    : ~TBD (determined after spec phase)",
            "",
            "Fields marked with ~ were inferred, not explicitly stated.",
            "",
        ]
        print("\n".join(lines), flush=True)

        if ctx.yes or not sys.stdin.isatty():
            return

        try:
            input("Press Enter to proceed (Ctrl-C to abort): ")
        except (KeyboardInterrupt, EOFError) as exc:
            print("", flush=True)  # newline after ^C or EOF
            if isinstance(exc, KeyboardInterrupt):
                raise EnvFailure("run aborted at readback") from exc
            # EOF: non-interactive stdin (CI, pipe) — proceed without waiting

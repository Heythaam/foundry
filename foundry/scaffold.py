"""What goes into the initial scaffold commit. Pure functions: no git, no network (AD-8).

FR-15: README.md, LICENSE (MIT), .gitignore, .github/workflows/ci.yml and the planning
artifacts, with foundry.lock already in place. FR-37: the CI workflow runs exactly one thing,
the test command recorded in foundry.lock, on pull requests to main.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
PLANNING_DIR = "docs/planning"
PLANNING_PHASES = ("brainstorm", "prd", "architecture", "epics")

DEFAULT_REPO_NAME = "foundry-project"
_MAX_SLUG_LENGTH = 60  # GitHub allows 100; shorter names are friendlier to type

_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_DOC_WORDS_RE = re.compile(
    r"\b(?:product requirements document|requirements document|prd|epic breakdown|"
    r"epics and stories|architecture (?:spine|document))\b",
    re.IGNORECASE,
)


def slugify(text: str, max_length: int = _MAX_SLUG_LENGTH) -> str:
    """`Foundry CLI: Ideas → Repos!` -> `foundry-cli-ideas-repos` (a valid GitHub repo name)."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        slug = (cut.rsplit("-", 1)[0] if "-" in cut else cut).strip("-")
    return slug or DEFAULT_REPO_NAME


def project_name(documents: Mapping[str, str], idea: str) -> str:
    """The project's name: the PRD's title, else the epics' title, else the idea's first words."""
    for phase in ("prd", "epics", "architecture"):
        title = _title_of(documents.get(phase, ""))
        if title:
            return title
    words = re.sub(r"\s+", " ", idea).strip().split(" ")
    return " ".join(words[:6]) or "Foundry Project"


def _title_of(document: str) -> str:
    match = _H1_RE.search(document.replace("\r\n", "\n"))
    if not match:
        return ""
    title = re.sub(r"[`*_]", "", match.group(1))
    title = _DOC_WORDS_RE.sub(" ", title)
    return re.sub(r"^[\s:–—\-|/]+|[\s:–—\-|/]+$", "", re.sub(r"\s+", " ", title))


# ---------------------------------------------------------------------------- files


def render_readme(name: str, idea: str, test_command: str) -> str:
    summary = re.sub(r"\s+", " ", idea).strip()
    return (
        f"# {name}\n\n"
        f"{summary}\n\n"
        "This project was planned and built with [Foundry](https://github.com/Heythaam/foundry). "
        f"The approved planning documents are in [`{PLANNING_DIR}/`]({PLANNING_DIR}/) and every "
        "step Foundry took is recorded in `foundry.lock` and in the commit history.\n\n"
        "## Tests\n\n"
        f"```bash\n{test_command}\n```\n"
    )


def render_license(holder: str, year: int) -> str:
    return f"""\
MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# Language detection reads the test command and the architecture, so the .gitignore and the CI
# toolchain setup match what the project is actually built with.
_LANGUAGES: dict[str, tuple[re.Pattern[str], str, str | None]] = {
    "python": (
        re.compile(r"\b(?:python3?|pytest|pip|uv|poetry|tox|unittest)\b", re.IGNORECASE),
        "__pycache__/\n*.py[cod]\n*.egg-info/\n.venv/\nvenv/\n.pytest_cache/\n.mypy_cache/\n"
        ".ruff_cache/\nbuild/\ndist/\n",
        "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n",
    ),
    "node": (
        re.compile(r"\b(?:node(?:js)?|npm|npx|yarn|pnpm|jest|vitest|mocha|typescript|bun)\b",
                   re.IGNORECASE),
        "node_modules/\ndist/\ncoverage/\n*.log\n",
        "      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'\n",
    ),
    "go": (
        re.compile(r"\bgo (?:test|build|vet)\b|\bgolang\b", re.IGNORECASE),
        "bin/\n*.test\n*.out\n",
        "      - uses: actions/setup-go@v5\n        with:\n          go-version: stable\n",
    ),
    "rust": (
        re.compile(r"\bcargo\b|\brustc?\b", re.IGNORECASE),
        "target/\n",
        None,  # the runner image ships a Rust toolchain
    ),
    "java": (
        re.compile(r"\b(?:mvn|maven|gradle|gradlew|java)\b", re.IGNORECASE),
        "target/\nbuild/\n.gradle/\n*.class\n",
        "      - uses: actions/setup-java@v4\n        with:\n          distribution: temurin\n"
        "          java-version: '21'\n",
    ),
}


def detect_languages(test_command: str, architecture: str = "") -> list[str]:
    """Languages named by the test command; if it names none, those the architecture names."""
    for text in (test_command, architecture):
        found = [name for name, (pattern, _, _) in _LANGUAGES.items() if pattern.search(text)]
        if found:
            return found
    return []


def render_gitignore(languages: list[str]) -> str:
    blocks = [
        "# Foundry runtime state (cancel sentinel, state.db, prompt files, approved artifacts)\n"
        ".foundry/\n",
        "# Secrets and local config\n.env\n.env.*\n!.env.example\n",
        "# Editors and OS\n.idea/\n.vscode/\n*.swp\n.DS_Store\nThumbs.db\n",
    ]
    for name in languages:
        blocks.append(f"# {name}\n{_LANGUAGES[name][1]}")
    return "\n".join(blocks)


def render_ci(test_command: str, languages: list[str]) -> str:
    """The workflow: check out, set up the toolchain, run the one recorded test command."""
    if "\n" in test_command or "\r" in test_command:
        raise ValueError("the test command must be a single line")
    setup = "".join(setup for name in languages if (setup := _LANGUAGES[name][2]))
    return (
        "name: CI\n\n"
        "on:\n"
        "  pull_request:\n"
        "    branches: [main]\n\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{setup}"
        "      - name: Run tests\n"
        "        run: |\n"
        f"          {test_command}\n"
    )


def scaffold_files(
    *,
    name: str,
    idea: str,
    test_command: str,
    documents: Mapping[str, str],
    license_holder: str,
    year: int,
) -> dict[str, str]:
    """Every file of the scaffold commit except foundry.lock, keyed by repo-relative path."""
    languages = detect_languages(test_command, documents.get("architecture", ""))
    files = {
        "README.md": render_readme(name, idea, test_command),
        "LICENSE": render_license(license_holder, year),
        ".gitignore": render_gitignore(languages),
        CI_WORKFLOW_PATH: render_ci(test_command, languages),
    }
    for phase in PLANNING_PHASES:
        if phase in documents:
            files[f"{PLANNING_DIR}/{phase}.md"] = documents[phase]
    return files

from __future__ import annotations

import re

import pytest

from foundry.scaffold import (
    CI_WORKFLOW_PATH,
    detect_languages,
    project_name,
    render_ci,
    render_gitignore,
    render_license,
    scaffold_files,
    slugify,
)


class TestSlugify:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Todo Tracker", "todo-tracker"),
            ("  Foundry CLI: Ideas → Repos!  ", "foundry-cli-ideas-repos"),
            ("Café Ünïcode", "cafe-unicode"),
            ("a---b___c", "a-b-c"),
            ("", "foundry-project"),
            ("!!!", "foundry-project"),
        ],
    )
    def test_produces_a_github_safe_name(self, text: str, expected: str) -> None:
        assert slugify(text) == expected

    def test_is_always_a_valid_repo_name(self) -> None:
        slug = slugify("An Extremely Long Project Name " * 10)
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        assert len(slug) <= 60
        assert not slug.endswith("-")


class TestProjectName:
    def test_prefers_the_prd_title_and_drops_document_words(self) -> None:
        assert project_name({"prd": "# Todo Tracker PRD\n"}, "idea") == "Todo Tracker"
        assert project_name({"prd": "# Product Requirements Document: Foundry\n"}, "i") == "Foundry"

    def test_falls_back_to_the_epics_title(self) -> None:
        assert project_name({"epics": "# Foundry CLI - Epic Breakdown\n"}, "i") == "Foundry CLI"

    def test_falls_back_to_the_first_words_of_the_idea(self) -> None:
        assert project_name({}, "a todo tracker for the command line of everyone") == (
            "a todo tracker for the command"
        )

    def test_a_title_that_is_only_document_words_falls_through(self) -> None:
        assert project_name({"prd": "# PRD\n", "epics": "# Epic Breakdown\n"}, "my idea") == "my idea"

    def test_ignores_markdown_emphasis(self) -> None:
        assert project_name({"prd": "# **Todo** `Tracker` PRD\n"}, "i") == "Todo Tracker"


class TestFiles:
    def test_scaffold_contains_the_required_files_and_planning_docs(self) -> None:
        files = scaffold_files(
            name="Todo Tracker",
            idea="a tracker",
            test_command="pytest -q",
            documents={"prd": "p", "architecture": "a", "epics": "e", "brainstorm": "b"},
            license_holder="Ada",
            year=2026,
        )
        assert set(files) == {
            "README.md",
            "LICENSE",
            ".gitignore",
            CI_WORKFLOW_PATH,
            "docs/planning/brainstorm.md",
            "docs/planning/prd.md",
            "docs/planning/architecture.md",
            "docs/planning/epics.md",
        }
        assert files["docs/planning/prd.md"] == "p"

    def test_license_is_mit_with_holder_and_year(self) -> None:
        text = render_license("Ada Lovelace", 2026)
        assert text.startswith("MIT License")
        assert "Copyright (c) 2026 Ada Lovelace" in text
        assert "Permission is hereby granted, free of charge" in text

    def test_readme_names_the_project_and_the_test_command(self) -> None:
        files = scaffold_files(
            name="Todo Tracker",
            idea="a   tracker\nfor todos",
            test_command="npm test",
            documents={},
            license_holder="x",
            year=2026,
        )
        assert files["README.md"].startswith("# Todo Tracker\n")
        assert "a tracker for todos" in files["README.md"]
        assert "npm test" in files["README.md"]

    def test_gitignore_always_ignores_foundry_state_and_secrets(self) -> None:
        lines = render_gitignore([]).splitlines()
        assert ".foundry/" in lines
        assert ".env" in lines

    def test_gitignore_follows_the_projects_language(self) -> None:
        assert "__pycache__/" in render_gitignore(["python"])
        assert "node_modules/" in render_gitignore(["node"])
        assert "node_modules/" not in render_gitignore(["python"])

    def test_language_comes_from_the_test_command_before_the_architecture(self) -> None:
        assert detect_languages("npm test", "uses python scripts") == ["node"]
        assert detect_languages("make check", "A Rust service built with cargo") == ["rust"]
        assert detect_languages("make check", "no hints") == []


class TestCiWorkflow:
    def test_runs_on_pull_requests_to_main_only(self) -> None:
        text = render_ci("pytest -q", ["python"])
        assert "on:\n  pull_request:\n    branches: [main]\n" in text
        assert "push:" not in text and "schedule:" not in text

    def test_has_exactly_one_run_step_and_it_is_the_recorded_command(self) -> None:
        text = render_ci("pip install -e . && pytest -q", ["python"])
        assert len(re.findall(r"^\s+run:", text, re.MULTILINE)) == 1
        assert "        run: |\n          pip install -e . && pytest -q\n" in text

    def test_sets_up_the_toolchain_without_running_anything_else(self) -> None:
        text = render_ci("pytest -q", ["python"])
        assert "actions/checkout@v4" in text
        assert "actions/setup-python@v5" in text

    def test_command_with_yaml_metacharacters_stays_inside_the_block_scalar(self) -> None:
        command = "pytest -k 'a: b' # still part of the script"
        text = render_ci(command, [])
        assert f"          {command}\n" in text

    def test_rejects_a_multiline_command(self) -> None:
        with pytest.raises(ValueError):
            render_ci("a\nb", [])

    def test_is_valid_yaml_when_a_parser_is_available(self) -> None:
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(render_ci("pytest -q", ["python"]))
        assert doc["jobs"]["test"]["steps"][-1]["run"].strip() == "pytest -q"

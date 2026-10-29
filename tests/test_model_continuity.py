"""Story 5.3: on resume, the pinned model is checked and the human chooses if it is gone."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from foundry.context import EnvFailure, InfraFailure
from foundry.lock import LockFile
from foundry.models import (
    AnthropicModelChecker,
    ModelAvailability,
    TerminalModelPrompt,
)
from foundry.orchestrator import RunStatus
from foundry.progress import progress_from_lock
from foundry.provenance import ProvenanceRecorder
from foundry.resume import ResumeCoordinator, context_from_lock
from tests.fake_github import FakeGitHub
from tests.planned import (
    AlwaysAvailable,
    ModelGone,
    RecordingRunner,
    ScriptedModelPrompt,
    resume_run,
)
from tests.test_implementation_loop import whole_run
from tests.test_resume import CancelsDuring

PINNED = "claude-sonnet-4-5"


class ModelsApi:
    """A local stand-in for GET /v1/models."""

    def __init__(self, pages: list[dict[str, Any]], status: int = 200) -> None:
        self.pages = pages
        self.status = status
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append((self.path, dict(self.headers)))
                index = len(outer.requests) - 1
                body = json.dumps(outer.pages[min(index, len(outer.pages) - 1)]).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> ModelsApi:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def page(*ids: str, more: bool = False) -> dict[str, Any]:
    return {"data": [{"id": i, "type": "model"} for i in ids], "has_more": more, "last_id": ids[-1]}


class TestTheModelsApiCheck:
    async def test_a_model_the_api_lists_is_available(self) -> None:
        with ModelsApi([page(PINNED, "claude-opus-4-5")]) as api:
            result = await AnthropicModelChecker("key", api.url).check(PINNED)
        assert result.available is True

    async def test_a_model_the_api_does_not_list_is_gone_and_alternatives_are_offered(self) -> None:
        with ModelsApi([page("claude-sonnet-5-0", "claude-opus-5-0")]) as api:
            result = await AnthropicModelChecker("key", api.url).check(PINNED)
        assert result.available is False
        assert result.alternatives == ("claude-sonnet-5-0", "claude-opus-5-0")

    async def test_the_key_and_version_are_sent_and_every_page_is_read(self) -> None:
        with ModelsApi([page("a-1", more=True), page(PINNED)]) as api:
            result = await AnthropicModelChecker("secret-key", api.url).check(PINNED)
        assert result.available is True  # found on the second page
        assert len(api.requests) == 2 and "after_id=a-1" in api.requests[1][0]
        headers = {k.lower(): v for k, v in api.requests[0][1].items()}
        assert headers["x-api-key"] == "secret-key" and headers["anthropic-version"]

    async def test_without_a_key_the_answer_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = await AnthropicModelChecker(base_url="http://127.0.0.1:1").check(PINNED)
        assert result.available is None and "ANTHROPIC_API_KEY" in result.reason

    @pytest.mark.parametrize("status", [401, 403, 500])
    async def test_an_api_error_is_unknown_not_gone(self, status: int) -> None:
        with ModelsApi([{"error": {"message": "no"}}], status=status) as api:
            result = await AnthropicModelChecker("key", api.url).check(PINNED)
        assert result.available is None and str(status) in result.reason

    async def test_an_unreachable_api_is_unknown_not_gone(self) -> None:
        result = await AnthropicModelChecker("key", "http://127.0.0.1:1", timeout=2).check(PINNED)
        assert result.available is None and "could not be reached" in result.reason

    async def test_nonsense_from_the_api_is_unknown_not_gone(self) -> None:
        with ModelsApi([{"unexpected": True}]) as api:
            result = await AnthropicModelChecker("key", api.url).check(PINNED)
        assert result.available is None

    def test_the_base_url_can_be_overridden_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.example/")
        assert AnthropicModelChecker("k").base_url == "http://proxy.example"


class TestResumeWhenTheModelIsAvailable:
    async def test_it_continues_silently_on_the_same_model(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        checker, prompt, said = AlwaysAvailable(), ScriptedModelPrompt("never"), []
        runner = RecordingRunner()

        ctx, result, _ = await resume_run(workspace, runner, checker=checker, prompt=prompt, said=said)

        assert result.status is RunStatus.COMPLETED
        assert checker.asked == [PINNED] and prompt.shown == []
        assert not any("model" in line.lower() and "Model:" not in line for line in said)
        assert {model for _, model in runner.models} == {PINNED}
        assert "model_changes" not in LockFile.for_workspace(workspace).read()
        assert ctx.model_id == PINNED

    @pytest.mark.parametrize(
        "checker",
        [
            type("Unknown", (), {"check": lambda self, m: _coro(ModelAvailability(None, reason="no key"))})(),
            type("Broken", (), {"check": lambda self, m: _raise(InfraFailure("api down"))})(),
        ],
        ids=["unknown", "check-raises"],
    )
    async def test_a_check_that_cannot_be_made_does_not_stop_the_run(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub, checker: Any
    ) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))
        said: list[str] = []
        prompt = ScriptedModelPrompt("never")

        _, result, _ = await resume_run(
            workspace, RecordingRunner(), checker=checker, prompt=prompt, said=said
        )

        assert result.status is RunStatus.COMPLETED and prompt.shown == []
        assert any("Could not check" in line and PINNED in line for line in said)


async def _coro(value: Any) -> Any:
    return value


async def _raise(error: Exception) -> Any:
    raise error


class TestResumeWhenTheModelIsGone:
    async def cancelled(self, workspace: Path, tmp_path: Path) -> None:
        await whole_run(workspace, tmp_path, CancelsDuring("1.1"))

    async def test_the_human_is_offered_the_alternatives(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        prompt = ScriptedModelPrompt("claude-sonnet-5-0")
        await resume_run(workspace, RecordingRunner(), checker=ModelGone(), prompt=prompt)
        assert prompt.shown == [(PINNED, ("claude-sonnet-5-0", "claude-opus-5-0"))]

    async def test_the_choice_is_recorded_as_a_new_section_and_run_model_is_untouched(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        before = (workspace / "foundry.lock").read_text(encoding="utf-8")

        await resume_run(
            workspace, RecordingRunner(), checker=ModelGone(), prompt=ScriptedModelPrompt("claude-sonnet-5-0")
        )

        after = (workspace / "foundry.lock").read_text(encoding="utf-8")
        assert after.startswith(before)  # append-only: nothing earlier was edited
        data = LockFile.for_workspace(workspace).read()
        assert data["run"]["model"] == PINNED
        change = data["model_changes"]["change-1"]
        assert (change["from"], change["to"]) == (PINNED, "claude-sonnet-5-0")
        assert change["boundary_story"] == "1.1"
        assert (change["stories_completed"], change["stories_total"]) == (1, 3)
        assert LockFile.for_workspace(workspace).current_model() == "claude-sonnet-5-0"
        assert data["resumes"]["resume-1"]["model_changed"] is True
        assert data["resumes"]["resume-1"]["model"] == "claude-sonnet-5-0"

    async def test_the_remaining_stories_run_on_the_new_model(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        runner = RecordingRunner()
        ctx, result, _ = await resume_run(
            workspace, runner, checker=ModelGone(), prompt=ScriptedModelPrompt("claude-sonnet-5-0")
        )

        assert result.status is RunStatus.COMPLETED and ctx.model_id == "claude-sonnet-5-0"
        stories = [(name, model) for name, model in runner.models if name.startswith("story")]
        assert stories == [("story 1.2", "claude-sonnet-5-0"), ("story 2.1", "claude-sonnet-5-0")]

    async def test_the_change_flags_a_consistency_pass_for_the_current_epic(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        ctx = context_from_lock(workspace)
        coordinator = ResumeCoordinator(
            ctx, _git(workspace), ModelGone(), ScriptedModelPrompt("claude-sonnet-5-0"), say=lambda t: None
        )
        await coordinator.prepare(ProvenanceRecorder(workspace, coordinator.git))

        progress = progress_from_lock(LockFile.for_workspace(workspace).read())
        assert progress is not None and progress.pending_model_change is not None
        flag = progress.pending_model_change
        assert (flag.original_model, flag.new_model) == (PINNED, "claude-sonnet-5-0")
        assert (flag.boundary_story, flag.stories_completed, flag.stories_total) == ("1.1", 1, 3)

    async def test_choosing_the_same_model_changes_nothing(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        await resume_run(workspace, RecordingRunner(), checker=ModelGone(), prompt=ScriptedModelPrompt(PINNED))
        assert "model_changes" not in LockFile.for_workspace(workspace).read()

    async def test_a_later_resume_checks_the_model_the_run_is_on_now(
        self, workspace: Path, tmp_path: Path, github: FakeGitHub
    ) -> None:
        await self.cancelled(workspace, tmp_path)
        await resume_run(
            workspace, CancelsDuring("1.2"), checker=ModelGone(), prompt=ScriptedModelPrompt("claude-sonnet-5-0")
        )
        checker = AlwaysAvailable()
        ctx, result, _ = await resume_run(workspace, RecordingRunner(), checker=checker)

        assert checker.asked == ["claude-sonnet-5-0"]  # not the model the run started with
        assert ctx.model_id == "claude-sonnet-5-0" and result.status is RunStatus.COMPLETED


def _git(workspace: Path):
    from foundry.gitops import GitOps

    return GitOps.from_env(workspace, require_token=True)


class TestTheTerminalPrompt:
    @staticmethod
    def answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> list[str]:
        asked: list[str] = []
        queue = list(replies)

        def fake_input(question: str = "") -> str:
            asked.append(question)
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        return asked

    async def test_a_number_picks_from_the_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers(monkeypatch, "2")
        chosen = await TerminalModelPrompt().choose(PINNED, ["claude-sonnet-5-0", "claude-opus-5-0"])
        assert chosen == "claude-opus-5-0"

    async def test_a_model_id_can_be_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers(monkeypatch, " claude-haiku-5-0 ")
        assert await TerminalModelPrompt().choose(PINNED, ["claude-sonnet-5-0"]) == "claude-haiku-5-0"

    async def test_an_alias_without_a_version_is_refused_and_asked_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self.answers(monkeypatch, "claude-sonnet", "claude-sonnet-5-0")
        assert await TerminalModelPrompt().choose(PINNED, []) == "claude-sonnet-5-0"
        assert len(asked) == 2

    async def test_three_unusable_answers_stop_the_resume_with_a_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.answers(monkeypatch, "", "claude-sonnet", "latest")
        with pytest.raises(EnvFailure) as raised:
            await TerminalModelPrompt().choose(PINNED, [])
        assert raised.value.fix

    async def test_closed_input_is_an_environment_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answers(monkeypatch)
        with pytest.raises(EnvFailure, match="standard input is closed"):
            await TerminalModelPrompt().choose(PINNED, ["claude-sonnet-5-0"])

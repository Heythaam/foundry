from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's environment out of the tests; give git an identity."""
    for var in (
        "FOUNDRY_MODEL",
        "FOUNDRY_MAX_RETRIES",
        "EDITOR",
        "VISUAL",
        "GITHUB_TOKEN",
        "GITHUB_API_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Foundry Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Foundry Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture(autouse=True)
def _stub_cli_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests run the placeholder phase and no stages: no real agent, no GitHub."""
    from foundry.phases import StubPhase

    monkeypatch.setattr("foundry.cli._default_phases", lambda: [StubPhase()])
    monkeypatch.setattr("foundry.cli._build_stages", lambda *args: ([], None))


_FAKE_CLAUDE = r'''
import json, os, pathlib, sys, time

scenario = json.loads(pathlib.Path(os.environ["FAKE_CLAUDE_SCENARIO"]).read_text(encoding="utf-8"))
story = pathlib.Path(".foundry/current-story.md")
invocation = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "prompt_file": story.read_text(encoding="utf-8") if story.exists() else None,
}
pathlib.Path(scenario["record"]).write_text(json.dumps(invocation), encoding="utf-8")
with open(scenario["record"] + ".log", "a", encoding="utf-8") as log:  # one line per session
    log.write(json.dumps(invocation) + "\n")
for relative, text in scenario.get("writes", {}).items():
    target = pathlib.Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "system", "subtype": "init"})
for delta in scenario.get("deltas", []):
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": delta}}})
    time.sleep(scenario.get("delay", 0))
if scenario.get("garbage"):
    print("this is not json", flush=True)
if scenario.get("hang"):
    time.sleep(120)
if scenario.get("stderr"):
    print(scenario["stderr"], file=sys.stderr, flush=True)
if "result" in scenario:
    emit({"type": "result", "subtype": "success", "is_error": scenario.get("is_error", False),
          "result": scenario["result"], "api_error_status": scenario.get("api_error_status")})
sys.exit(scenario.get("exit", 0))
'''


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A stand-in `claude` executable. Call it with a scenario; returns (command, record_path)."""
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    record = tmp_path / "claude_record.json"
    scenario_file = tmp_path / "scenario.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(scenario_file))

    def _configure(**scenario) -> tuple[list[str], Path]:
        import json

        scenario_file.write_text(json.dumps({"record": str(record), **scenario}), encoding="utf-8")
        return [sys.executable, str(script)], record

    return _configure


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    return ws


@pytest.fixture
def make_editor(tmp_path: Path) -> Callable[[str], list[str]]:
    """Build a fake $EDITOR: a Python script run with the artifact path as argv[1].

    The body sees `path` (a pathlib.Path). Returns the editor argv.
    """
    counter = 0

    def _make(body: str = "pass") -> list[str]:
        nonlocal counter
        counter += 1
        script = tmp_path / f"fake_editor_{counter}.py"
        script.write_text(
            "import pathlib, sys, time\n"
            "path = pathlib.Path(sys.argv[1])\n" + textwrap.dedent(body) + "\n",
            encoding="utf-8",
        )
        return [sys.executable, str(script)]

    return _make


@pytest.fixture
def github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A local fake GitHub (real HTTP, real bare repos) plus the env that points Foundry at it."""
    from tests.fake_github import TOKEN, FakeGitHub

    server = FakeGitHub(tmp_path / "github").start()
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_API_URL", server.url)
    yield server
    server.stop()

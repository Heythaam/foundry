"""AgentRunner: invoke a BMad agent as a Claude Code subprocess (AD-7 pattern).

The prompt is written to `.foundry/current-story.md` in the workspace; the subprocess is told
to read it, so the agent never has to locate anything. Output is parsed from Claude Code's
`stream-json` and text deltas are forwarded to the terminal the moment they arrive (NFR-3).
Every failure leaves this module as a classified FoundryError (AD-3).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from foundry.context import (
    CodeFailure,
    EnvFailure,
    FoundryError,
    InfraFailure,
    RunContext,
    classify_exception,
)
from foundry.state import STATE_DIRNAME

PROMPT_FILENAME = "current-story.md"
PROMPT_RELATIVE_PATH = f"{STATE_DIRNAME}/{PROMPT_FILENAME}"

# Planning agents return their deliverable as text, so they get no Write/Edit/Bash.
DEFAULT_ALLOWED_TOOLS = ("Skill", "Read")
DEFAULT_TIMEOUT_SECONDS = 1800.0
_STREAM_LINE_LIMIT = 64 * 1024 * 1024  # a single result event can hold a whole document

_INFRA_PATTERN = re.compile(
    r"rate.?limit|overloaded|\b429\b|\b5\d\d\b|timed? ?out|econn|enotfound|network|"
    r"connection (?:reset|refused|error)|temporarily unavailable",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentRequest:
    name: str  # label for logs and errors, e.g. "brainstorm"
    prompt: str


@dataclass(frozen=True)
class AgentResult:
    text: str


@runtime_checkable
class AgentRunner(Protocol):
    async def run(self, request: AgentRequest, ctx: RunContext) -> AgentResult: ...


def print_to_terminal(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def classify_agent_failure(detail: str, status: int | None = None) -> FoundryError:
    """Infrastructure for rate limits / 5xx / network; environment for everything else."""
    if status is not None and (status == 429 or status >= 500):
        return InfraFailure(f"agent call failed (HTTP {status}): {detail}", {"status": status})
    if status is None and _INFRA_PATTERN.search(detail):
        return InfraFailure(f"agent call failed: {detail}")
    return EnvFailure(f"agent call failed: {detail}", {"status": status})


class ClaudeCodeRunner:
    def __init__(
        self,
        command: Sequence[str] = ("claude",),
        on_text: Callable[[str], None] = print_to_terminal,
        timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
        allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    ) -> None:
        self.command = list(command)
        self.on_text = on_text
        self.timeout_seconds = timeout_seconds
        self.allowed_tools = list(allowed_tools)

    async def run(self, request: AgentRequest, ctx: RunContext) -> AgentResult:
        prompt_path = ctx.workspace_path / STATE_DIRNAME / PROMPT_FILENAME
        try:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(request.prompt, encoding="utf-8")
        except OSError as exc:
            raise EnvFailure(f"cannot write agent prompt file {prompt_path}: {exc}") from exc
        try:
            return await self._invoke(request, ctx)
        except FoundryError:
            raise
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            try:  # AD-7: the prompt file never outlives its session
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------------- internals

    def _argv(self, ctx: RunContext) -> list[str]:
        executable = shutil.which(self.command[0]) or self.command[0]
        task = (
            f"Read {PROMPT_RELATIVE_PATH} and carry out the task it describes. "
            "Your final message is the deliverable."
        )
        # Variadic options go last so they cannot swallow the positional prompt.
        return [
            executable,
            *self.command[1:],
            "-p",
            task,
            "--model",
            ctx.model_id,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--allowedTools",
            *self.allowed_tools,
        ]

    async def _invoke(self, request: AgentRequest, ctx: RunContext) -> AgentResult:
        argv = self._argv(ctx)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=ctx.workspace_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LINE_LIMIT,
            )
        except FileNotFoundError as exc:
            raise EnvFailure(
                f"could not find {self.command[0]!r} on PATH; install Claude Code to run agents"
            ) from exc
        except OSError as exc:
            raise EnvFailure(f"could not launch {self.command[0]!r}: {exc}") from exc

        stderr_task = asyncio.create_task(proc.stderr.read())  # type: ignore[union-attr]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                state = await self._consume(proc)
                await proc.wait()
        except TimeoutError as exc:
            raise InfraFailure(
                f"agent {request.name!r} did not finish within {self.timeout_seconds:.0f}s"
            ) from exc
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()

        return self._finish(request, proc.returncode or 0, state, stderr)

    async def _consume(self, proc: asyncio.subprocess.Process) -> _StreamState:
        state = _StreamState()
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return state
            self._handle_line(line.decode("utf-8", errors="replace").strip(), state)

    def _handle_line(self, line: str, state: _StreamState) -> None:
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return  # not an event; ignore rather than corrupt the artifact
        if not isinstance(event, dict):
            return

        kind = event.get("type")
        if kind == "stream_event":
            delta = (event.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                state.saw_deltas = True
                state.streamed.append(delta["text"])
                self.on_text(delta["text"])
        elif kind == "assistant" and not state.saw_deltas:
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    state.streamed.append(block["text"])
                    self.on_text(block["text"])
        elif kind == "result":
            state.result = event

    @staticmethod
    def _finish(
        request: AgentRequest, returncode: int, state: _StreamState, stderr: str
    ) -> AgentResult:
        result = state.result
        if result is not None and result.get("is_error"):
            status = result.get("api_error_status")
            detail = str(result.get("result") or stderr or "agent reported an error")
            raise classify_agent_failure(detail, status if isinstance(status, int) else None)
        if returncode != 0:
            raise classify_agent_failure(stderr or f"exited with status {returncode}")

        text = result.get("result") if result is not None else "".join(state.streamed)
        text = text if isinstance(text, str) else ""
        if not text.strip():
            raise CodeFailure(f"agent {request.name!r} returned no output")
        return AgentResult(text=text)


@dataclass
class _StreamState:
    streamed: list[str] = field(default_factory=list)
    saw_deltas: bool = False
    result: dict[str, Any] | None = None

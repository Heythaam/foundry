"""The test gate: run the project's test command and capture what it said (FR-18).

The command comes from `[run.test_runner]` in foundry.lock. It runs through the shell from the
repository root so it behaves exactly as it does for a human or in CI. Its output is kept so a
failed story can carry it into a retry (Epic 4) and is forwarded to the terminal as it is
produced. Nothing here raises anything but FoundryError.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from foundry.agent import print_to_terminal
from foundry.console import print_test_result, print_test_running
from foundry.context import EnvFailure, classify_exception

DEFAULT_TIMEOUT_SECONDS = 1800.0
_DRAIN_GRACE_SECONDS = 5.0
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024  # keep the tail: the failure is at the end

# A shell that cannot find the program prints one short line; a real failing suite prints a lot.
_NOT_FOUND = re.compile(
    r"command not found|is not recognized as an internal or external command", re.IGNORECASE
)
_NOT_FOUND_MAX_OUTPUT = 500


@dataclass(frozen=True)
class GateResult:
    passed: bool
    exit_code: int
    output: str  # stdout and stderr interleaved, as the command printed them
    duration_seconds: float
    tests_passed: int | None  # None when the output has no recognisable count
    timed_out: bool = False

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.output.rstrip().splitlines()[-lines:])


_JEST = re.compile(r"^\s*Tests:.*?(\d+) passed", re.MULTILINE)
_CARGO = re.compile(r"test result: ok\. (\d+) passed")
_PYTEST = re.compile(r"\b(\d+) passed\b")
_MOCHA = re.compile(r"\b(\d+) passing\b")
_UNITTEST = re.compile(r"^Ran (\d+) tests?\b", re.MULTILINE)


def parse_tests_passed(output: str) -> int | None:
    """Best-effort count of passed tests from common runners' summaries."""
    if match := _JEST.search(output):
        return int(match.group(1))
    if cargo := _CARGO.findall(output):  # one summary per test binary
        return sum(int(count) for count in cargo)
    if pytest := _PYTEST.findall(output):
        return int(pytest[-1])
    if match := _MOCHA.search(output):
        return int(match.group(1))
    if match := _UNITTEST.search(output):
        return int(match.group(1))
    return None


async def run_test_command(
    command: str,
    cwd: Path,
    *,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    on_output: Callable[[str], None] = print_to_terminal,
) -> GateResult:
    print_test_running()
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=sys.platform != "win32",  # so a timeout can kill the whole tree
        )
    except OSError as exc:
        raise EnvFailure(f"could not start the test command {command!r}: {exc}") from exc

    chunks: list[bytes] = []
    captured = 0

    async def drain() -> None:
        nonlocal captured
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(8192):
            chunks.append(chunk)
            captured += len(chunk)
            while captured > _MAX_CAPTURE_BYTES and len(chunks) > 1:
                captured -= len(chunks.pop(0))
            if on_output is not None and on_output is not print_to_terminal:
                on_output(chunk.decode("utf-8", errors="replace"))

    reader = asyncio.create_task(drain())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        # Grandchildren can keep the pipe open after the shell exits; do not wait forever.
        try:
            await asyncio.wait_for(asyncio.shield(reader), _DRAIN_GRACE_SECONDS)
        except TimeoutError:
            pass
    except TimeoutError:
        timed_out = True
        _kill_tree(proc)
        await proc.wait()
    except Exception as exc:  # classify before propagating (AD-3)
        _kill_tree(proc)
        raise classify_exception(exc) from exc
    finally:
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    output = b"".join(chunks).decode("utf-8", errors="replace")
    exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    if timed_out:
        output += f"\n[foundry] test command timed out after {timeout:.0f}s and was killed\n"
    missing = _program_missing(command, cwd, exit_code, output)
    if exit_code != 0 and missing and not timed_out:
        raise EnvFailure(
            f"the test command could not run ({command!r}): {output.strip()[-300:]}. "
            "Fix `[run.test_runner]` in the architecture or install what it needs.",
            {"command": command, "exit_code": exit_code},
        )
    passed = exit_code == 0
    print_test_result(passed, output)
    return GateResult(
        passed=passed,
        exit_code=exit_code,
        output=output,
        duration_seconds=time.monotonic() - started,
        tests_passed=parse_tests_passed(output) if passed else None,
        timed_out=timed_out,
    )


# Shell builtins and keywords that are valid first words without being programs on PATH.
_SHELL_WORDS = frozenset(
    "cd set export echo call exit if for source . : true false test [ type pushd popd".split()
)


def _program_missing(command: str, cwd: Path, exit_code: int, output: str) -> bool:
    """Did the shell fail to find the program, as opposed to the program failing?

    Shells word this differently in every locale (and cmd.exe exits 1), so besides the
    well-known exit codes and English messages, a short failing output from a command whose
    first word is neither a builtin nor resolvable on PATH counts as missing.
    """
    if exit_code in (126, 127, 9009):
        return True
    if exit_code == 0 or len(output) >= _NOT_FOUND_MAX_OUTPUT:
        return False
    if _NOT_FOUND.search(output):
        return True
    try:
        words = shlex.split(command, posix=sys.platform != "win32")
    except ValueError:
        return False
    if not words or "=" in words[0] or words[0].lower() in _SHELL_WORDS:
        return False
    program = words[0].strip("\"'")
    return shutil.which(program, path=os.pathsep.join([str(cwd), os.environ.get("PATH", "")])) is None


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass

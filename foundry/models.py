"""Is the run's pinned model still available? (FR-33)

A run records the exact model it used (FR-26). Months later, on `foundry resume`, that model may
have been retired. The check asks Anthropic's Models API, which needs `ANTHROPIC_API_KEY`: Claude
Code itself may be signed in some other way, so without a key the answer is "could not tell",
which resume treats as "carry on with the pinned model" rather than as a reason to stop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from foundry.agent import print_to_terminal
from foundry.context import EnvFailure, ModelAliasError, check_model_alias

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
BASE_URL_ENV_VAR = "ANTHROPIC_BASE_URL"
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
_MAX_PAGES = 10
MAX_CHOICE_ATTEMPTS = 3


@dataclass(frozen=True)
class ModelAvailability:
    available: bool | None  # None: the check could not be made
    alternatives: tuple[str, ...] = ()  # models that are available, newest first
    reason: str = ""  # why the check could not be made


@runtime_checkable
class ModelChecker(Protocol):
    async def check(self, model_id: str) -> ModelAvailability: ...


class AnthropicModelChecker:
    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR) or None
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    async def check(self, model_id: str) -> ModelAvailability:
        if not self.api_key:
            return ModelAvailability(None, reason=f"{API_KEY_ENV_VAR} is not set")
        try:
            models = await asyncio.to_thread(self._list_models)
        except _Unavailable as exc:
            return ModelAvailability(None, reason=str(exc))
        return ModelAvailability(model_id in models, tuple(models))

    def _list_models(self) -> list[str]:
        import requests

        ids: list[str] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"limit": 1000}
            if after:
                params["after_id"] = after
            try:
                response = requests.get(
                    f"{self.base_url}/v1/models",
                    params=params,
                    headers={"x-api-key": str(self.api_key), "anthropic-version": API_VERSION},
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise _Unavailable(f"the Models API could not be reached ({type(exc).__name__})") from exc
            if response.status_code in (401, 403):
                raise _Unavailable(f"the Models API rejected {API_KEY_ENV_VAR} (HTTP {response.status_code})")
            if response.status_code != 200:
                raise _Unavailable(f"the Models API answered HTTP {response.status_code}")
            try:
                body = response.json()
                ids.extend(str(item["id"]) for item in body["data"])
            except (ValueError, KeyError, TypeError) as exc:
                raise _Unavailable("the Models API sent an answer foundry could not read") from exc
            if not body.get("has_more") or not body.get("last_id"):
                break
            after = str(body["last_id"])
        return ids


class _Unavailable(Exception):
    """The availability check itself failed (not: the model is gone)."""


@runtime_checkable
class ModelPrompt(Protocol):
    async def choose(self, unavailable: str, alternatives: Sequence[str]) -> str:
        """The model to continue with, picked by the human."""
        ...


class TerminalModelPrompt:
    async def choose(self, unavailable: str, alternatives: Sequence[str]) -> str:
        lines = [f"The model this run was built with, {unavailable}, is no longer available."]
        if alternatives:
            lines.append("Available models:")
            lines += [f"  {n}. {model}" for n, model in enumerate(alternatives, 1)]
            question = "Choose a number, or type a model ID: "
        else:
            question = "Type the ID of the model to continue with: "
        problem = ""
        for _ in range(MAX_CHOICE_ATTEMPTS):
            try:
                answer = await asyncio.to_thread(self._ask, "\n".join([*lines, problem]).strip(), question)
            except EOFError as exc:
                raise EnvFailure(
                    "cannot ask you to choose a replacement model: standard input is closed.",
                    fix="Run `foundry resume` from an interactive terminal.",
                ) from exc
            chosen = answer.strip()
            if chosen.isdigit() and 1 <= int(chosen) <= len(alternatives):
                chosen = alternatives[int(chosen) - 1]
            try:
                if not chosen:
                    raise ModelAliasError("no model given")
                check_model_alias(chosen)
            except ModelAliasError as exc:
                problem = f"{exc.message}"
                continue
            return chosen
        raise EnvFailure(
            "no usable replacement model was chosen.",
            fix="Run `foundry resume` again and pick one of the available models.",
        )

    @staticmethod
    def _ask(text: str, question: str) -> str:
        print_to_terminal(text + "\n")
        return input(question)

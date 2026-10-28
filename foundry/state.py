"""StateBuffer: aiosqlite as an ephemeral WAL for in-flight operations (AD-6).

`.foundry/state.db` is never the source of truth and is never committed. It exists so a
crash between the two provenance sinks is detectable: an operation that was begun but
never completed shows up in `pending()`. It can always be rebuilt from foundry.lock + git.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TypeVar

import aiosqlite

from foundry.context import EnvFailure, iso_utc, utc_now

STATE_DIRNAME = ".foundry"
STATE_DB_FILENAME = "state.db"

PENDING = "pending"
DONE = "done"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    section TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
)
"""

T = TypeVar("T")


class StateError(EnvFailure):
    """The state buffer could not be opened or written."""


@dataclass(frozen=True)
class PendingOp:
    id: int
    action: str
    detail: str
    section: str
    status: str


def state_db_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_DIRNAME / STATE_DB_FILENAME


class StateBuffer:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> StateBuffer:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute(_SCHEMA)
            await self._db.commit()
        except (aiosqlite.Error, OSError) as exc:
            await self.close()
            raise StateError(f"cannot open state buffer {self.path}: {exc}") from exc

    async def close(self) -> None:
        if self._db is not None:
            db, self._db = self._db, None
            try:
                await db.close()
            except aiosqlite.Error:
                pass

    async def journal_mode(self) -> str:
        async def query(db: aiosqlite.Connection) -> str:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            return str(row[0]) if row else ""

        return await self._guard(query)

    async def begin(self, action: str, detail: str, section: str) -> int:
        async def insert(db: aiosqlite.Connection) -> int:
            cursor = await db.execute(
                "INSERT INTO ops (action, detail, section, status, started_at) VALUES (?, ?, ?, ?, ?)",
                (action, detail, section, PENDING, iso_utc(utc_now())),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

        return await self._guard(insert)

    async def complete(self, op_id: int) -> None:
        await self._finish(op_id, DONE, None)

    async def fail(self, op_id: int, error: str) -> None:
        await self._finish(op_id, FAILED, error)

    async def pending(self) -> list[PendingOp]:
        async def query(db: aiosqlite.Connection) -> list[PendingOp]:
            cursor = await db.execute(
                "SELECT id, action, detail, section, status FROM ops WHERE status != ? ORDER BY id",
                (DONE,),
            )
            return [PendingOp(*row) for row in await cursor.fetchall()]

        return await self._guard(query)

    async def _finish(self, op_id: int, status: str, error: str | None) -> None:
        async def update(db: aiosqlite.Connection) -> None:
            await db.execute(
                "UPDATE ops SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, iso_utc(utc_now()), error, op_id),
            )
            await db.commit()

        await self._guard(update)

    async def _guard(self, fn: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        if self._db is None:
            raise StateError("state buffer is not open")
        try:
            return await fn(self._db)
        except aiosqlite.Error as exc:
            raise StateError(f"state buffer error: {exc}") from exc



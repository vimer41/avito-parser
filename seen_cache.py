"""
In-memory память об уже обработанных объявлениях каждого источника (URL выдачи).

Зачем: при ротации продвинутых объявлений топ-3 выдачи меняется, хотя новых лотов нет,
и каждый такой скан уходил в SQLite (fanout). Если все ID страницы уже прошли fanout
при той же ревизии группы радаров, результат fanout заведомо пустой — его можно пропустить.

Интерфейс асинхронный и не зависит от процесса, чтобы позже заменить реализацию
на общую (например, Redis SET с TTL) без изменения бизнес-логики.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Iterable, Optional, Protocol

import config


class SeenStore(Protocol):
    async def all_known(self, source: str, revision: str, item_ids: Iterable[str]) -> bool: ...

    async def none_known(self, source: str, revision: str, item_ids: Iterable[str]) -> bool: ...

    async def remember(self, source: str, revision: str, item_ids: Iterable[str]) -> None: ...

    async def forget(self, source: str) -> None: ...


class _SourceEntry:
    __slots__ = ("revision", "ids")

    def __init__(self, revision: str):
        self.revision = revision
        # item_id -> expires_at (monotonic); insertion order doubles as LRU order
        self.ids: OrderedDict[str, float] = OrderedDict()


class InMemorySeenStore:
    """Bounded per-source LRU with TTL. Single-process, event-loop only (no locks needed)."""

    def __init__(
        self,
        ttl_seconds: Optional[float] = None,
        max_per_source: Optional[int] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self.ttl = float(config.SEEN_CACHE_TTL_HOURS * 3600 if ttl_seconds is None else ttl_seconds)
        self.max_per_source = int(config.SEEN_CACHE_MAX_PER_SOURCE if max_per_source is None else max_per_source)
        self._monotonic = monotonic_fn
        self._sources: dict[str, _SourceEntry] = {}

    async def all_known(self, source: str, revision: str, item_ids: Iterable[str]) -> bool:
        """True only when every ID was already processed under the same radar-group revision."""
        entry = self._sources.get(source)
        if entry is None or entry.revision != revision:
            return False
        now = self._monotonic()
        ids = entry.ids
        checked = False
        for item_id in item_ids:
            checked = True
            expires_at = ids.get(item_id)
            if expires_at is None or expires_at <= now:
                return False
        return checked

    async def remember(self, source: str, revision: str, item_ids: Iterable[str]) -> None:
        entry = self._sources.get(source)
        if entry is None or entry.revision != revision:
            # A changed radar set must re-run fanout for the new members, so start from scratch.
            entry = _SourceEntry(revision)
            self._sources[source] = entry
        expires_at = self._monotonic() + self.ttl
        ids = entry.ids
        for item_id in item_ids:
            ids[item_id] = expires_at
            ids.move_to_end(item_id)
        while len(ids) > self.max_per_source:
            ids.popitem(last=False)

    async def none_known(self, source: str, revision: str, item_ids: Iterable[str]) -> bool:
        """True only when this source has history for the revision and none of the IDs is in it (a flood)."""
        entry = self._sources.get(source)
        if entry is None or entry.revision != revision or not entry.ids:
            return False
        now = self._monotonic()
        checked = False
        for item_id in item_ids:
            checked = True
            expires_at = entry.ids.get(item_id)
            if expires_at is not None and expires_at > now:
                return False
        return checked

    async def forget(self, source: str) -> None:
        self._sources.pop(source, None)

    def prune(self, active_sources: Iterable[str]) -> None:
        """Drop sources that no radar watches anymore."""
        keep = set(active_sources)
        for source in [key for key in self._sources if key not in keep]:
            del self._sources[source]

    def __len__(self) -> int:
        return sum(len(entry.ids) for entry in self._sources.values())

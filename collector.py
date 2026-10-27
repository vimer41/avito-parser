"""Bounded exact-source collection; never broaden subscriber searches."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import config
from freshness import publication_age_seconds
from parser import AvitoItem, FetchFailure

logger = logging.getLogger(__name__)


@dataclass
class SourceCollection:
    title: str
    items: tuple[AvitoItem, ...]
    captured_at: float
    pages: int
    coverage_gap: bool
    elapsed_ms: int
    discovery_times: dict[str, float] = field(default_factory=dict)


def reached_old_boundary(items, max_age_minutes):
    organic = [item for item in items if not item.is_promotion]
    ages = [publication_age_seconds(item.date, item.timestamp) for item in organic]
    return bool(ages and all(age is not None for age in ages)
                and all(ages[index] <= ages[index + 1] for index in range(len(ages) - 1))
                and ages[-1] > max_age_minutes * 60)


async def collect_source(parser, url: str, max_age_minutes: int = 5):
    started = time.monotonic()
    deadline = started + config.AVITO_FETCH_TIMEOUT
    items_by_id = {}
    discovery_times = {}
    page_url = url
    visited = set()
    pages = 0
    title = ""
    gap = False
    while page_url and pages < config.AVITO_MAX_PAGES_PER_SCAN:
        visited.add(page_url)
        html = await parser.fetch_page_async(page_url, deadline=deadline)
        captured_at = time.time()
        if not html:
            if pages == 0:
                return None
            gap = True
            break
        page_items = await asyncio.to_thread(parser.parse_items, html)
        if not page_items and not await asyncio.to_thread(parser.has_catalog, html):
            parser.last_failure = FetchFailure("catalog_missing")
            if pages == 0:
                return None
            gap = True
            break
        pages += 1
        if not title:
            title = await asyncio.to_thread(parser.extract_search_title, html, url)
        for item in page_items:
            discovery_times.setdefault(item.item_id, captured_at)
            items_by_id.setdefault(item.item_id, item)
        next_url = await asyncio.to_thread(parser.next_page_url, html, page_url)
        if next_url is not None and not isinstance(next_url, str):
            next_url = None  # Non-network test doubles must not invent pagination.
        if reached_old_boundary(page_items, max_age_minutes):
            gap = False
            break
        if next_url in visited:
            gap = True
            break
        page_url = next_url
        gap = bool(next_url)
        if time.monotonic() >= deadline:
            break
    elapsed_ms = int((time.monotonic() - started) * 1000)
    parser.last_coverage_gap = gap
    if gap:
        logger.warning("Покрытие источника ограничено: pages=%s items=%s limit=%s; продолжение не проверено", pages, len(items_by_id), config.AVITO_MAX_PAGES_PER_SCAN)
    return SourceCollection(title, tuple(items_by_id.values()), time.time(), pages, gap, elapsed_ms, discovery_times)
"""
Live probe of one network channel (a proxy or the server's own IP) against a real Avito search.

The probe goes through exactly the production scan path (ProxyClient pacing, curl_cffi
impersonation, anti-bot challenge handling, SSR catalog parsing), so its result predicts
how radars will behave on that channel.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from collector import collect_source
from parser import AvitoParser
from proxy_pool import ProxyClient

DEFAULT_PROBE_URL = "https://www.avito.ru/moskva?q=iphone&s=104"


@dataclass
class ProbeAttempt:
    ok: bool
    elapsed_ms: int
    items: int = 0
    failure: str = ""
    status: Optional[int] = None


@dataclass
class ProbeResult:
    proxy_id: str
    masked_url: str
    target_url: str
    attempts: list[ProbeAttempt] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.ok)

    @property
    def median_ok_ms(self) -> Optional[int]:
        times = [attempt.elapsed_ms for attempt in self.attempts if attempt.ok]
        return int(statistics.median(times)) if times else None


async def probe_channel(proxy_url: Optional[str], target_url: Optional[str] = None, attempts: int = 3) -> ProbeResult:
    """Run `attempts` back-to-back single-page scans through a throwaway client for this channel."""
    target = AvitoParser.normalize_url(target_url or DEFAULT_PROBE_URL)
    client = ProxyClient(proxy_url)
    parser = AvitoParser(proxy_client=client)
    result = ProbeResult(proxy_id=client.proxy_id, masked_url=client.masked_url, target_url=target)
    try:
        for _ in range(max(1, attempts)):
            started = time.monotonic()
            collection = await collect_source(parser, target, max_pages=1)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if collection is not None:
                result.attempts.append(ProbeAttempt(True, elapsed_ms, items=len(collection.items)))
                continue
            failure = parser.last_failure
            result.attempts.append(ProbeAttempt(
                False,
                elapsed_ms,
                failure=failure.kind if failure else "unknown",
                status=failure.status if failure else None,
            ))
    finally:
        parser.close()
        await client.close()
    return result

"""A single paced HTTP budget per configured external connection.

The starting rate is a local load limit, not an assertion about Avito's policy.
Synchronous sessions use a cancellable FIFO gate, including nested requests.
"""

import asyncio
import hashlib
import math
import time
from collections import Counter, deque
from contextlib import asynccontextmanager, contextmanager
from email.utils import parsedate_to_datetime
from threading import Condition, Event, Lock, RLock

import config
from anti_bot.pow_budget import check_cancelled

BLOCK_BASE_COOLDOWN = config.AVITO_BLOCK_BASE_COOLDOWN
BLOCK_MAX_COOLDOWN = config.AVITO_BLOCK_MAX_COOLDOWN


class RequestDeferred(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"shared HTTP cooldown: {retry_after:.1f}s")


class RequestBudgetExceeded(RuntimeError):
    pass


def parse_retry_after(headers) -> float:
    raw = next((value for key, value in (headers or {}).items() if key.lower() == "retry-after"), None)
    if raw is None:
        return 0.0
    try:
        result = float(raw)
    except (TypeError, ValueError):
        try:
            result = parsedate_to_datetime(str(raw)).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, result) if math.isfinite(result) else 0.0


class NetworkBlockState:
    """Shared rate, concurrency, cooldown and cautious recovery; no worker failover."""

    def __init__(self, requests_per_second: float = config.AVITO_REQUESTS_PER_SECOND,
                 max_in_flight: int = config.AVITO_MAX_IN_FLIGHT):
        if not math.isfinite(requests_per_second) or requests_per_second <= 0 or max_in_flight < 1:
            raise ValueError("HTTP rate and concurrency must be positive")
        self.requests_per_second = requests_per_second
        self.max_in_flight = max_in_flight
        self.blocked_until = 0.0
        self.block_streak = 0
        self.lock = RLock()
        self._condition = Condition(self.lock)
        self._waiting = deque()
        self._in_flight = 0
        self._next_start = 0.0
        self._rate_factor = 1.0
        self._recovery_successes = 0
        self._requests = 0
        self._statuses = Counter()

    def register_block(self, retry_after: float = 0) -> tuple[float, int]:
        with self._condition:
            self.block_streak += 1
            cooldown = max(min(BLOCK_BASE_COOLDOWN * 2 ** min(self.block_streak - 1, 10), BLOCK_MAX_COOLDOWN), retry_after)
            self.blocked_until = max(self.blocked_until, time.time() + cooldown)
            self._rate_factor = max(0.1, self._rate_factor * 0.5)
            self._recovery_successes = 0
            self._condition.notify_all()
            return cooldown, self.block_streak

    def register_success(self):
        with self._condition:
            # An older in-flight success cannot lift another session's cooldown.
            if self.blocked_until > time.time():
                return
            self._recovery_successes += 1
            if self._recovery_successes >= 5:
                self.block_streak = 0
            if self._recovery_successes >= 10:
                self._rate_factor = min(1.0, self._rate_factor * 1.25)
                self._recovery_successes = 0
            self._condition.notify_all()

    def cooldown_remaining(self) -> float:
        with self.lock:
            return max(0.0, self.blocked_until - time.time())

    def record_response(self, status: int):
        with self.lock:
            self._statuses[int(status)] += 1

    def register_throttle_response(self, status: int, headers=None) -> tuple[float, int] | None:
        """Atomically classify a rate-limit response and apply shared backoff."""
        retry_after = parse_retry_after(headers)
        if status in (403, 429) or (status == 503 and retry_after):
            return self.register_block(retry_after)
        return None

    def acquire(self, cancel_event: Event | None = None, deadline: float | None = None):
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while True:
                    check_cancelled(cancel_event)
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        raise RequestBudgetExceeded("HTTP operation deadline exceeded")
                    cooldown = self.blocked_until - time.time()
                    if cooldown > 0:
                        raise RequestDeferred(cooldown)
                    if self._waiting[0] is ticket and self._in_flight < self.max_in_flight and now >= self._next_start:
                        self._waiting.popleft()
                        self._in_flight += 1
                        self._requests += 1
                        self._next_start = now + 1 / (self.requests_per_second * self._rate_factor)
                        self._condition.notify_all()
                        return now
                    self._condition.wait(timeout=min(0.1, max(0.001, deadline - now)) if deadline is not None else 0.1)
            finally:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._in_flight <= 0:
                raise RuntimeError("HTTP slot released without acquisition")
            self._in_flight -= 1
            self._condition.notify_all()

    @contextmanager
    def request_slot(self, cancel_event: Event | None = None, deadline: float | None = None):
        granted_at = self.acquire(cancel_event, deadline)
        try:
            check_cancelled(cancel_event)
            yield granted_at
        finally:
            self.release()

    @asynccontextmanager
    async def request_slot_async(self, timeout: float = 10):
        event = Event()
        task = asyncio.create_task(asyncio.to_thread(self.acquire, event, time.monotonic() + timeout))
        acquired = False
        try:
            try:
                await asyncio.shield(task)
                acquired = True
            except asyncio.CancelledError:
                event.set()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled() and task.exception() is None:
                    self.release()
                raise
            yield task.result()
        finally:
            if acquired:
                self.release()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "requests": self._requests, "statuses": dict(self._statuses),
                "in_flight": self._in_flight, "waiting": len(self._waiting),
                "current_rps": self.requests_per_second * self._rate_factor,
                "cooldown_seconds": max(0.0, self.blocked_until - time.time()),
                "block_streak": self.block_streak,
            }


NETWORK_BLOCK_STATES: dict[str, NetworkBlockState] = {}
NETWORK_STATES_LOCK = Lock()


def get_network_block_state(proxy: str | None) -> NetworkBlockState:
    key = hashlib.sha256((proxy or "direct").encode()).hexdigest()
    with NETWORK_STATES_LOCK:
        return NETWORK_BLOCK_STATES.setdefault(key, NetworkBlockState())
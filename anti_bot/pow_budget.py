"""Finite, cooperatively cancellable CPU budgets for local proof-of-work."""

import math
import time
from threading import Event

DEFAULT_POW_TIMEOUT = 5.0
DEFAULT_POW_MAX_ITERATIONS = 2_000_000


class PowSearchError(RuntimeError):
    """A local PoW search stopped without a solution."""


class PowSearchCancelled(PowSearchError):
    pass


class PowSearchTimeout(PowSearchError):
    pass


class PowSearchLimit(PowSearchError):
    pass


def check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PowSearchCancelled("operation cancelled")


class SearchBudget:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_POW_TIMEOUT,
        max_iterations: int = DEFAULT_POW_MAX_ITERATIONS,
        cancel_event: Event | None = None,
    ):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or max_iterations <= 0:
            raise ValueError("max_iterations must be a positive integer")
        self.deadline = time.monotonic() + timeout
        self.max_iterations = max_iterations
        self.cancel_event = cancel_event

    def check(self, iteration: int) -> None:
        check_cancelled(self.cancel_event)
        if iteration >= self.max_iterations:
            raise PowSearchLimit("PoW iteration limit exceeded")
        if time.monotonic() >= self.deadline:
            raise PowSearchTimeout("PoW deadline exceeded")


def has_leading_zero_bits(digest_hex: str, bits: int) -> bool:
    """Check full zero nibbles AND the high bits of the next nibble."""
    if isinstance(bits, bool) or not isinstance(bits, int) or not 0 <= bits <= len(digest_hex) * 4:
        raise ValueError("bits must fit the hash digest")
    full_nibbles, remainder = divmod(bits, 4)
    if not digest_hex.startswith("0" * full_nibbles):
        return False
    return remainder == 0 or int(digest_hex[full_nibbles], 16) < (1 << (4 - remainder))
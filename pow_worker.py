"""Avito firewall proof-of-work search in a separate process.

The nonce search is a tight Python loop over sha256 of short strings, which holds the GIL: run in a
thread it slows the event loop (scans, deliveries, button presses) for up to POW timeout seconds.
Here it runs in one worker process. On Linux the worker forks from a forkserver that imports only
this module, so it costs a few MB of RAM instead of a copy of the bot.

Keep the imports light: the worker process imports this module and nothing else.
"""

import hashlib
import logging
import multiprocessing
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from typing import Optional

logger = logging.getLogger(__name__)

_POOL: Optional[ProcessPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def search_nonce(challenge_id: str, complexity: int, start: int, timeout: float, max_iterations: int) -> Optional[int]:
    """First nonce >= start whose sha256(f"{id}:{nonce}") hex starts with `complexity` zeros; None if
    the time or iteration budget runs out. Runs inside the worker process."""
    target = "0" * complexity
    prefix = f"{challenge_id}:".encode("utf-8")
    deadline = time.monotonic() + timeout
    nonce = start
    for iteration in range(max_iterations):
        if iteration % 4096 == 0 and time.monotonic() >= deadline:
            return None
        if hashlib.sha256(prefix + str(nonce).encode()).hexdigest().startswith(target):
            return nonce
        nonce += 1
    return None


def _get_pool() -> ProcessPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            if sys.platform.startswith("linux"):
                ctx = multiprocessing.get_context("forkserver")
                ctx.set_forkserver_preload(["pow_worker"])
            else:
                ctx = multiprocessing.get_context("spawn")
            _POOL = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
        return _POOL


def _drop_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


class PowProcessUnavailable(RuntimeError):
    """The worker process could not run the search; the caller falls back to an in-thread search."""


def run_search(challenge_id: str, complexity: int, *, start: int, timeout: float, max_iterations: int,
               cancel_event: Optional[threading.Event] = None) -> Optional[int]:
    """Blocking call for the challenge thread: waits for the worker without holding the GIL.
    Returns the nonce, None when the budget ran out, raises InterruptedError when cancelled."""
    try:
        future = _get_pool().submit(search_nonce, challenge_id, complexity, start, timeout, max_iterations)
    except (BrokenProcessPool, RuntimeError, OSError) as exc:
        _drop_pool()
        raise PowProcessUnavailable(str(exc)) from exc
    hard_deadline = time.monotonic() + timeout + 10.0  # worker start-up plus the search budget
    while True:
        if cancel_event is not None and cancel_event.is_set():
            future.cancel()
            raise InterruptedError("PoW search cancelled")
        try:
            return future.result(timeout=0.2)
        except FutureTimeout:
            if time.monotonic() >= hard_deadline:
                # A stuck worker would block every later challenge: replace it.
                _drop_pool()
                return None
        except BrokenProcessPool as exc:
            _drop_pool()
            raise PowProcessUnavailable(str(exc)) from exc


def shutdown() -> None:
    _drop_pool()

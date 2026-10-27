"""Offline-testable, event-loop-owned deadline scheduling for unique sources."""

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class IntervalPlan:
    intervals: dict[str, float]
    requested_rps: float
    planned_rps: float
    available_rps: float
    overloaded: bool


def calculate_interval_plan(base_intervals: dict[str, float], requests_per_second: float,
                            max_interval: float = 120, utilization: float = 0.8,
                            requests_per_scan: float = 1) -> IntervalPlan:
    values = [requests_per_second, max_interval, utilization, requests_per_scan, *base_intervals.values()]
    if any(not math.isfinite(value) or value <= 0 for value in values) or utilization > 1:
        raise ValueError("Intervals, request cost and budget must be positive and finite")
    available = requests_per_second * utilization
    demand = sum(requests_per_scan / interval for interval in base_intervals.values())
    scale = max(1.0, demand / available)
    intervals = {key: min(max(max_interval, base), base * scale) for key, base in base_intervals.items()}
    planned = sum(requests_per_scan / interval for interval in intervals.values())
    return IntervalPlan(intervals, demand, planned, available, planned > available * (1 + 1e-9))


@dataclass
class ScheduledScan:
    key: str
    radars: tuple[dict, ...]
    scheduled_at: float
    started_at: float
    retry_attempt: int
    generation: int


@dataclass
class SourceState:
    key: str
    radars: list[dict]
    interval: float
    due_at: float
    generation: int
    retry_attempt: int = 0
    last_started: Optional[float] = None
    last_success: Optional[float] = None


class DeadlineScheduler:
    """Oldest deadline first; retries have a bounded share and no absolute priority.

    There is at most one pending entry and one running scan per source. Deletion
    and re-addition cannot allow an obsolete task to overwrite a new generation.
    """

    def __init__(self, max_retries: int = 2, retry_base_delay: float = 5,
                 retry_max_delay: float = 60, max_retry_burst: int = 1):
        self.sources: dict[str, SourceState] = {}
        self.running: dict[str, ScheduledScan] = {}
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.max_retry_burst = max_retry_burst
        self._generation = 0
        self._retry_burst = 0
        self.completed = 0
        self.failures = 0

    def sync(self, groups: dict[str, list[dict]], intervals: dict[str, float], now: float,
             startup: bool = False, startup_spread: float = 15):
        for key in set(self.sources) - set(groups):
            del self.sources[key]
        for rank, key in enumerate(sorted(groups)):
            interval = intervals[key]
            state = self.sources.get(key)
            if state is None:
                self._generation += 1
                offset = rank / max(1, len(groups)) * min(startup_spread, interval) if startup else 0
                self.sources[key] = SourceState(key, groups[key], interval, now + offset, self._generation)
            else:
                state.radars = groups[key]
                if interval < state.interval and state.retry_attempt == 0 and key not in self.running:
                    anchor = state.last_started if state.last_started is not None else now
                    state.due_at = min(state.due_at, anchor + interval)
                state.interval = interval

    def take_due(self, now: float) -> Optional[ScheduledScan]:
        ready = [state for key, state in self.sources.items() if key not in self.running and state.due_at <= now]
        if not ready:
            return None
        regular = [state for state in ready if state.retry_attempt == 0]
        if regular and self._retry_burst >= self.max_retry_burst:
            ready = regular
        state = min(ready, key=lambda entry: (entry.due_at, entry.generation))
        self._retry_burst = self._retry_burst + 1 if state.retry_attempt else 0
        task = ScheduledScan(state.key, tuple(state.radars), state.due_at, now, state.retry_attempt, state.generation)
        self.running[state.key] = task
        state.last_started = now
        return task

    def complete(self, task: ScheduledScan, now: float, success: bool, retry_after: float = 0,
                 retryable: bool = True):
        if self.running.get(task.key) is not task:
            return
        del self.running[task.key]
        state = self.sources.get(task.key)
        if state is None or state.generation != task.generation:
            return
        self.completed += 1
        if success:
            state.last_success = now
            state.retry_attempt = 0
            # Anchor to the start, not completion; never replay missed ticks.
            state.due_at = max(now, task.started_at + state.interval)
        else:
            self.failures += 1
            if retryable and task.retry_attempt < self.max_retries:
                state.retry_attempt = task.retry_attempt + 1
                delay = min(self.retry_max_delay, self.retry_base_delay * 2 ** task.retry_attempt)
            else:
                state.retry_attempt = 0
                delay = state.interval
            state.due_at = now + max(delay, retry_after)

    def seconds_until_next(self, now: float) -> float:
        pending = [state.due_at for key, state in self.sources.items() if key not in self.running]
        return max(0.0, min(pending) - now) if pending else 1.0

    def snapshot(self, now: float) -> dict:
        pending = [state for key, state in self.sources.items() if key not in self.running]
        due = [state for state in pending if state.due_at <= now]
        return {
            "sources": len(self.sources), "subscriptions": sum(len(state.radars) for state in self.sources.values()),
            "running": len(self.running), "due": len(due),
            "deferred_retries": sum(state.retry_attempt > 0 for state in pending),
            "oldest_due_seconds": max((now - state.due_at for state in due), default=0.0),
            "max_revisit_seconds": max((now - state.last_started for state in self.sources.values() if state.last_started is not None), default=0.0),
            "completed": self.completed, "failures": self.failures,
        }
"""Deterministic offline capacity model. Does not import the bot or make requests."""

import argparse
import heapq
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan_planner import DeadlineScheduler, calculate_interval_plan


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def simulate(subscriptions=350, sources=350, seconds=1800, rps=0.5, latency=1.5, workers=2, failure_every=0):
    if not 1 <= sources <= subscriptions or min(seconds, rps, latency, workers) <= 0 or failure_every < 0:
        raise ValueError("Invalid simulation parameters")
    groups = {f"source-{index:04}": [] for index in range(sources)}
    for index in range(subscriptions):
        groups[f"source-{index % sources:04}"].append({"id": index, "pro": index < subscriptions // 5})
    base = {key: 15.0 if any(radar["pro"] for radar in radars) else 25.0 for key, radars in groups.items()}
    plan = calculate_interval_plan(base, rps)
    scheduler = DeadlineScheduler()
    scheduler.sync(groups, plan.intervals, 0, startup=True)
    now = next_request = 0.0
    requests = succeeded = 0
    completions = []
    revisits, lateness, last_started = [], [], {}
    while now + 1e-9 < seconds:
        while completions and completions[0][0] <= now:
            finished, serial, task, success = heapq.heappop(completions)
            scheduler.complete(task, finished, success)
            succeeded += int(success)
        if len(completions) < workers and now >= next_request:
            task = scheduler.take_due(now)
            if task is not None:
                requests += 1
                if task.key in last_started:
                    revisits.append(now - last_started[task.key])
                last_started[task.key] = now
                lateness.append(now - task.scheduled_at)
                success = not failure_every or requests % failure_every != 0
                heapq.heappush(completions, (now + latency, requests, task, success))
                next_request = now + 1 / rps
                continue
        candidates = [entry[0] for entry in completions]
        if len(completions) < workers:
            candidates.append(max(next_request, now + scheduler.seconds_until_next(now)))
        future = [value for value in candidates if value > now + 1e-9]
        now = min(future) if future else now + 0.001
    return {
        "subscriptions": subscriptions, "unique_sources": sources, "simulated_seconds": seconds,
        "budget_rps": rps, "requested_catalog_rps": round(plan.requested_rps, 3),
        "planned_catalog_rps": round(plan.planned_rps, 3), "overloaded": plan.overloaded,
        "requests_started": requests, "successes_completed": succeeded, "sources_visited": len(last_started),
        "revisit_p95_seconds": percentile(revisits, 0.95), "deadline_lateness_p95_seconds": percentile(lateness, 0.95),
        "state_at_end": scheduler.snapshot(seconds),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscriptions", type=int, default=350)
    parser.add_argument("--sources", type=int, nargs="+", default=[10, 50, 100, 350])
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--rps", type=float, default=0.5)
    parser.add_argument("--latency", type=float, default=1.5)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--failure-every", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps({"model_only": True, "assumptions": "One HTTP request per scan; no redirects, pagination, cooldown or Telegram latency. Not an Avito safe-rate benchmark.",
                      "scenarios": [simulate(args.subscriptions, count, args.seconds, args.rps, args.latency, args.workers, args.failure_every) for count in args.sources]}, indent=2))


if __name__ == "__main__":
    main()
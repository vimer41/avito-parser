"""Deterministic offline capacity model for Avito Radar (single-channel & multi-proxy up to 300 URLs).
Does not import the bot or make network requests.
"""

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


def calculate_proxy_capacity_table(
    url_counts=(25, 50, 100, 200, 250, 300),
    target_interval_seconds=15.0,
    max_target_interval_seconds=20.0,
    proxy_rps=1.0,
    req_per_scan_p95=1.3,
    utilization=0.8,
):
    """Calculate required healthy proxies and scan workers for target 15-20s intervals."""
    rows = []
    eff_rps_per_proxy = max(0.05, float(proxy_rps) * float(utilization))
    cost = max(1.0, float(req_per_scan_p95))
    for urls in url_counts:
        req_rps_15 = (urls / float(target_interval_seconds)) * cost
        req_rps_20 = (urls / float(max_target_interval_seconds)) * cost
        proxies_for_15s = max(1, int(math.ceil(req_rps_15 / eff_rps_per_proxy)))
        proxies_for_20s = max(1, int(math.ceil(req_rps_20 / eff_rps_per_proxy)))
        workers_15s = min(32, max(4, proxies_for_15s * 2))
        workers_20s = min(32, max(4, proxies_for_20s * 2))
        rows.append(
            {
                "unique_urls": int(urls),
                "req_per_scan_p95": round(cost, 2),
                "required_rps_at_15s": round(req_rps_15, 2),
                "required_rps_at_20s": round(req_rps_20, 2),
                "min_healthy_proxies_20s": proxies_for_20s,
                "recommended_proxies_15s": proxies_for_15s,
                "scan_workers_20s": workers_20s,
                "scan_workers_15s": workers_15s,
            }
        )
    return rows


def simulate(
    subscriptions=350,
    sources=350,
    seconds=1800,
    rps=0.5,
    latency=1.5,
    workers=2,
    failure_every=0,
    *,
    proxies=1,
    proxy_rps=None,
    req_per_scan=1.0,
    proxy_attempts=2,
):
    if not 1 <= sources <= subscriptions or min(seconds, rps, latency, workers) <= 0 or failure_every < 0:
        raise ValueError("Invalid simulation parameters")

    total_rps = float(proxies) * float(proxy_rps) if proxy_rps is not None else float(rps)
    groups = {f"source-{index:04}": [] for index in range(sources)}
    for index in range(subscriptions):
        groups[f"source-{index % sources:04}"].append({"id": index, "pro": index < subscriptions // 5})

    if proxy_rps is not None:
        base = {key: 15.0 for key in groups}
    else:
        base = {key: 15.0 if any(radar["pro"] for radar in radars) else 25.0 for key, radars in groups.items()}

    plan = calculate_interval_plan(base, total_rps, requests_per_scan=max(1.0, float(req_per_scan)))
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
                failed_primary = bool(failure_every and requests % failure_every == 0)
                if failed_primary and proxies > 1 and proxy_attempts >= 2:
                    # Second proxy attempt within the same scan
                    requests += 1
                    success = not (failure_every and requests % failure_every == 0)
                else:
                    success = not failed_primary
                heapq.heappush(completions, (now + latency * max(1.0, float(req_per_scan)), requests, task, success))
                next_request = now + max(1.0, float(req_per_scan)) / total_rps
                continue
        candidates = [entry[0] for entry in completions]
        if len(completions) < workers:
            candidates.append(max(next_request, now + scheduler.seconds_until_next(now)))
        future = [value for value in candidates if value > now + 1e-9]
        now = min(future) if future else now + 0.001

    return {
        "subscriptions": subscriptions,
        "unique_sources": sources,
        "simulated_seconds": seconds,
        "proxies": proxies,
        "budget_rps": round(total_rps, 3),
        "requested_catalog_rps": round(plan.requested_rps, 3),
        "planned_catalog_rps": round(plan.planned_rps, 3),
        "overloaded": plan.overloaded,
        "requests_started": requests,
        "successes_completed": succeeded,
        "sources_visited": len(last_started),
        "revisit_p95_seconds": percentile(revisits, 0.95),
        "deadline_lateness_p95_seconds": percentile(lateness, 0.95),
        "state_at_end": scheduler.snapshot(seconds),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscriptions", type=int, default=350)
    parser.add_argument("--sources", type=int, nargs="+", default=[10, 50, 100, 300, 350])
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--rps", type=float, default=0.5)
    parser.add_argument("--proxies", type=int, default=1)
    parser.add_argument("--proxy-rps", type=float, default=None)
    parser.add_argument("--req-per-scan", type=float, default=1.0)
    parser.add_argument("--latency", type=float, default=1.5)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--failure-every", type=int, default=0)
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "model_only": True,
                "assumptions": "Deterministic capacity model for single-channel and multi-proxy pool.",
                "capacity_table_15_20s": calculate_proxy_capacity_table(),
                "scenarios": [
                    simulate(
                        max(args.subscriptions, count),
                        count,
                        args.seconds,
                        args.rps,
                        args.latency,
                        args.workers,
                        args.failure_every,
                        proxies=args.proxies,
                        proxy_rps=args.proxy_rps,
                        req_per_scan=args.req_per_scan,
                    )
                    for count in args.sources
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

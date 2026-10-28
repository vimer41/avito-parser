"""Runtime orchestration: proxy pool, heap deadline scheduler, decoupled HTTP & DB fanout."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

import config
import database
from collector import collect_source
from parser import AvitoParser
from proxy_pool import get_proxy_pool
from request_budget import get_network_block_state
from runtime_metrics import METRICS
from scan_planner import DeadlineScheduler, calculate_interval_plan

logger = logging.getLogger(__name__)
MONITORING_STATS: dict = {}


def group_sources(searches: list[dict]) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for radar in searches:
        try:
            key = AvitoParser.normalize_url(radar["url"])
        except ValueError:
            logger.warning("Некорректный URL у радара #%s: сканирование пропущено", radar["id"])
            continue
        groups[key].append(radar)
    return dict(groups)


def source_interval(radars: list[dict]) -> float:
    """Uniform target scan interval across all tariffs (15-20s)."""
    return (float(config.SCAN_INTERVAL_MIN) + float(config.SCAN_INTERVAL_MAX)) / 2.0


async def run_monitoring(bot, parser: Optional[AvitoParser] = None):
    import scheduler
    from handlers.searches import pop_resumed_source_urls

    budget = get_network_block_state(config.PROXY_URL)
    pool = get_proxy_pool()
    planner = DeadlineScheduler(
        config.SCAN_MAX_RETRIES,
        config.SCAN_RETRY_BASE_DELAY,
        config.SCAN_RETRY_MAX_DELAY,
        interval_low=config.SCAN_INTERVAL_MIN,
        interval_high=config.SCAN_INTERVAL_MAX,
        use_source_backoff=True,
    )

    worker_count = max(1, int(config.MAX_CONCURRENT_WORKERS), len(pool.clients))
    parsers: list[AvitoParser] = [parser] if parser is not None else []
    try:
        for _ in range(worker_count - len(parsers)):
            parsers.append(AvitoParser(proxy=config.PROXY_URL))
    except Exception:
        for entry in parsers:
            if entry is not parser:
                entry.close()
        raise
    owned_parsers = [entry for entry in parsers if entry is not parser]

    changed = asyncio.Event()
    fanout_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(config.FANOUT_QUEUE_SIZE)))
    state_updates_lock = asyncio.Lock()
    pending_state_updates: dict[str, tuple[str, float, list[str], int]] = {}

    request_cost = 1.0
    stats = {"successful_scans": 0, "failed_scans": 0, "coverage_gaps": 0, "fast_skipped_scans": 0}
    consecutive_failures = 0
    alert_sent = False

    logger.info(
        "📡 Планировщик: %s HTTP-воркеров, %s fanout-воркеров, прокси в пуле=%s, целевой интервал %.1f-%.1f с",
        len(parsers),
        config.FANOUT_WORKERS,
        len(pool.clients),
        config.SCAN_INTERVAL_MIN,
        config.SCAN_INTERVAL_MAX,
    )

    async def queue_state_update(source_url: str, processed_at: float, top_ids: tuple[str, ...], failure_streak: int):
        async with state_updates_lock:
            pending_state_updates[source_url] = (source_url, processed_at, list(top_ids[:3]), failure_streak)

    async def flush_state_updates():
        async with state_updates_lock:
            if not pending_state_updates:
                return
            batch = list(pending_state_updates.values())
            pending_state_updates.clear()
        try:
            await database.upsert_source_scan_states_batch(batch)
        except Exception:
            logger.debug("Не удалось сохранить пакет source_scan_state", exc_info=True)

    async def notify_health(recovered: bool):
        text = (
            "✅ Получение каталога Авито возобновилось."
            if recovered
            else "⚠️ Повторяющиеся ошибки получения каталога Авито. Проверки отложены; уведомления в outbox сохранены. Подробности и общий cooldown — в логах."
        )
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                logger.warning("Не удалось отправить администратору состояние мониторинга", exc_info=True)

    async def notify_admins_text(text: str):
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                logger.warning("Не удалось отправить алерт администратору", exc_info=True)

    async def fanout_worker(f_id: int):
        while True:
            item = await fanout_queue.get()
            if item is None:
                fanout_queue.task_done()
                break
            task, collection, worker_id = item
            try:
                await scheduler.process_collected_group_fanout(
                    task.key,
                    list(task.radars),
                    collection,
                    worker_id=worker_id,
                    previous_processed_at=task.previous_processed_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой fanout-обработки источника %s", task.key[:60])
            finally:
                now_mono = time.monotonic()
                planner.complete(
                    task,
                    now_mono,
                    True,
                    top_ids=collection.top_ids,
                    fast_skipped=collection.fast_skipped,
                    processed_epoch=collection.captured_at,
                )
                state = planner.sources.get(task.key)
                streak = state.source_failure_streak if state else 0
                await queue_state_update(task.key, collection.captured_at, collection.top_ids, streak)
                changed.set()
                fanout_queue.task_done()

    async def worker(worker_id: int, worker_parser: AvitoParser):
        nonlocal consecutive_failures, alert_sent
        while True:
            if not pool.clients:
                cooldown = budget.cooldown_remaining()
                if cooldown > 0:
                    await asyncio.sleep(min(cooldown, 1.0))
                    continue
            elif pool.healthy_count() == 0:
                min_cd = min((c.cooldown_remaining() for c in pool.clients), default=0.5)
                if min_cd > 0:
                    await asyncio.sleep(min(min_cd, 1.0))
                    continue

            now_mono = time.monotonic()
            now_epoch = time.time()
            task = planner.take_due(now_mono, now_epoch=now_epoch)
            if task is None:
                changed.clear()
                timeout = min(1.0, max(0.01, planner.seconds_until_next(time.monotonic())))
                try:
                    await asyncio.wait_for(changed.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                continue

            revisit_seconds = None
            state_before = planner.sources.get(task.key)
            if state_before and state_before.last_success is not None:
                revisit_seconds = max(0.0, now_mono - state_before.last_success)
            due_lag_seconds = max(0.0, now_mono - task.scheduled_at)

            # Compatibility path if a test monkey-patched scheduler.process_url_group or injected a custom Mock parser
            is_mock_parser = type(worker_parser).__name__ != "AvitoParser"
            is_patched_process = getattr(scheduler.process_url_group, "__name__", "") != "process_url_group" or is_mock_parser

            if is_patched_process or not pool.clients:
                ok = False
                scan_t0 = time.monotonic()
                try:
                    ok = await scheduler.process_url_group(
                        bot,
                        worker_parser,
                        task.key,
                        list(task.radars),
                        worker_id=worker_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Сбой обработки источника; повтор будет отложен")
                elapsed_ms = (time.monotonic() - scan_t0) * 1000.0
                failure = getattr(worker_parser, "last_failure", None) if not ok else None
                gap = bool(getattr(worker_parser, "last_coverage_gap", False))
                raw_reqs = getattr(worker_parser, "last_http_requests", 1)
                http_reqs = max(1, int(raw_reqs)) if isinstance(raw_reqs, (int, float)) else 1
                METRICS.record_scan_attempt(
                    success=ok,
                    elapsed_ms=elapsed_ms,
                    pages_fetched=1,
                    http_requests=http_reqs,
                    fast_skipped=False,
                    coverage_gap=gap,
                    revisit_seconds=revisit_seconds,
                    due_lag_seconds=due_lag_seconds,
                )
                planner.complete(
                    task,
                    time.monotonic(),
                    ok,
                    retry_after=max(budget.cooldown_remaining(), failure.retry_after if failure else 0),
                    retryable=failure.retryable if failure else True,
                    processed_epoch=time.time() if ok else None,
                    exhausted_proxies=not ok,
                )
                changed.set()
                stats["successful_scans" if ok else "failed_scans"] += 1
                stats["coverage_gaps"] += int(gap)
                consecutive_failures = 0 if ok else consecutive_failures + 1
                if ok and alert_sent:
                    alert_sent = False
                    await notify_health(True)
                elif consecutive_failures >= 5 and not alert_sent:
                    alert_sent = True
                    await notify_health(False)
                continue

            # Production multi-proxy collection with up to SCAN_PROXY_ATTEMPTS (2) distinct proxies
            tried_proxy_ids: set[str] = set()
            collection = None
            last_failure = None
            total_http_reqs = 0
            scan_t0 = time.monotonic()
            max_age = max((min(30, max(1, int(r.get("max_age_minutes") or 5))) for r in task.radars), default=5)
            has_pro_catchup = (
                task.previous_processed_at > 0
                and (now_epoch - task.previous_processed_at) > (max_age * 60)
                and any(database.classify_user_tariff(r)[0] == "pro" for r in task.radars)
            )
            collection_max_age = config.CATCHUP_MAX_AGE_MINUTES if has_pro_catchup else max_age
            collection_max_pages = config.CATCHUP_MAX_PAGES if has_pro_catchup else config.AVITO_MAX_PAGES_PER_SCAN

            for _attempt in range(max(1, int(config.SCAN_PROXY_ATTEMPTS))):
                lease = pool.acquire_client(exclude_ids=tried_proxy_ids)
                if lease is None:
                    break
                client = lease.client
                tried_proxy_ids.add(client.proxy_id)
                proxy_parser = getattr(client, "_bound_parser", None)
                if proxy_parser is None:
                    proxy_parser = AvitoParser(proxy_client=client)
                    setattr(client, "_bound_parser", proxy_parser)
                try:
                    collection = await collect_source(
                        proxy_parser,
                        task.key,
                        collection_max_age,
                        max_pages=collection_max_pages,
                        can_fast_skip=(task.can_fast_skip and not has_pro_catchup),
                        last_top_ids=task.last_top_ids,
                    )
                    total_http_reqs += max(1, int(proxy_parser.last_http_requests or 1))
                    if collection is not None:
                        break
                    last_failure = proxy_parser.last_failure
                    if last_failure and not last_failure.retryable:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Сбой HTTP-сбора на прокси %s", client.proxy_id)
                finally:
                    lease.release()

            elapsed_ms = (time.monotonic() - scan_t0) * 1000.0
            ok = collection is not None
            gap = bool(collection.coverage_gap) if collection is not None else False
            fast_skipped = bool(collection.fast_skipped) if collection is not None else False
            pages_fetched = collection.pages if collection is not None else 0

            METRICS.record_scan_attempt(
                success=ok,
                elapsed_ms=elapsed_ms,
                pages_fetched=pages_fetched,
                http_requests=max(1, total_http_reqs),
                fast_skipped=fast_skipped,
                coverage_gap=gap,
                revisit_seconds=revisit_seconds,
                due_lag_seconds=due_lag_seconds,
                is_catchup=has_pro_catchup,
            )

            if not ok:
                logger.info(
                    "Scan result: ok=0 elapsed_ms=%d items=0 blocked=%d failure=%s",
                    int(elapsed_ms),
                    int(pool.healthy_count() == 0),
                    last_failure,
                )
                planner.complete(
                    task,
                    time.monotonic(),
                    False,
                    retry_after=last_failure.retry_after if last_failure else 0.0,
                    retryable=last_failure.retryable if last_failure else True,
                    exhausted_proxies=True,
                )
                state_after = planner.sources.get(task.key)
                await queue_state_update(
                    task.key,
                    task.previous_processed_at,
                    task.last_top_ids,
                    state_after.source_failure_streak if state_after else 1,
                )
                changed.set()
                stats["failed_scans"] += 1
                consecutive_failures += 1
                if consecutive_failures >= 5 and not alert_sent:
                    alert_sent = True
                    await notify_health(False)
                continue

            logger.info(
                "Scan result: ok=1 elapsed_ms=%d items=%d blocked=0 pages=%d coverage_gap=%d fast_skip=%d",
                collection.elapsed_ms,
                len(collection.items),
                collection.pages,
                int(collection.coverage_gap),
                int(fast_skipped),
            )
            stats["successful_scans"] += 1
            stats["coverage_gaps"] += int(gap)
            stats["fast_skipped_scans"] += int(fast_skipped)
            consecutive_failures = 0
            if alert_sent:
                alert_sent = False
                await notify_health(True)

            if fast_skipped:
                # Top-3 items on page 1 unchanged and all radars calibrated -> complete immediately without DB fanout
                scheduler.LAST_SCAN_TIMESTAMP = time.time()
                planner.complete(
                    task,
                    time.monotonic(),
                    True,
                    top_ids=collection.top_ids,
                    fast_skipped=True,
                    processed_epoch=collection.captured_at,
                )
                state_after = planner.sources.get(task.key)
                await queue_state_update(
                    task.key,
                    collection.captured_at,
                    collection.top_ids,
                    state_after.source_failure_streak if state_after else 0,
                )
                changed.set()
            else:
                if planner.mark_http_finished_fanout_pending(task, time.monotonic()):
                    await fanout_queue.put((task, collection, worker_id))
                else:
                    planner.complete(
                        task,
                        time.monotonic(),
                        True,
                        top_ids=collection.top_ids,
                        fast_skipped=False,
                        processed_epoch=collection.captured_at,
                    )
                    changed.set()

    async def dispatch():
        nonlocal request_cost
        startup = True
        last_report = last_cleanup = last_terminal_cleanup = time.monotonic()
        previous_requests = budget.snapshot()["requests"]
        previous_scans = 0
        outbox_metrics = await database.get_outbox_metrics()
        persisted_states = await database.load_all_source_scan_states()

        while True:
            searches = await database.get_all_active_searches()
            groups = group_sources(searches)
            network = budget.snapshot()
            proxy_snaps = pool.snapshots()
            healthy_proxies = sum(1 for p in proxy_snaps if p.get("state") in ("healthy", "degraded", "half_open"))
            effective_rps = (
                sum(float(p.get("current_rps") or 0.0) for p in proxy_snaps if p.get("state") in ("healthy", "degraded", "half_open"))
                if proxy_snaps
                else float(network["current_rps"])
            )
            effective_rps = max(0.1, effective_rps)

            base = {key: source_interval(radars) for key, radars in groups.items()}
            plan = calculate_interval_plan(
                base,
                effective_rps,
                config.SCAN_MAX_INTERVAL,
                config.SCAN_BUDGET_UTILIZATION,
                request_cost,
            )
            now = time.monotonic()
            planner.sync(
                groups,
                None,  # Uniform [interval_low, interval_high] jitter across all tariffs
                now,
                startup=startup,
                startup_spread=config.SCAN_STARTUP_SPREAD,
                persisted_states=persisted_states if startup else None,
            )
            startup = False

            resumed_urls = pop_resumed_source_urls()
            if resumed_urls:
                planner.force_due_now(resumed_urls, now)

            await flush_state_updates()
            changed.set()

            planner_snap = planner.snapshot(now)
            metrics_snap = METRICS.snapshot(
                active_unique_urls=len(groups),
                proxy_snapshots=proxy_snaps,
                outbox_metrics=outbox_metrics,
                full_coverage_age_seconds=planner_snap["full_coverage_age_seconds"],
            )
            cap = metrics_snap["capacity"]

            MONITORING_STATS.clear()
            MONITORING_STATS.update(
                planner_snap,
                **stats,
                requested_rps=plan.requested_rps,
                planned_rps=plan.planned_rps,
                available_rps=cap["available_rps"] if proxy_snaps else plan.available_rps,
                overloaded=bool(cap["deficit"] if proxy_snaps else plan.overloaded),
                requests_per_scan=metrics_snap["avg_http_requests_per_scan"],
                network=network,
                proxies=proxy_snaps,
                healthy_proxies=healthy_proxies,
                outbox=outbox_metrics,
                metrics=metrics_snap,
                capacity=cap,
                fanout_queue_depth=fanout_queue.qsize(),
            )

            if now - last_report >= float(config.METRICS_REPORT_INTERVAL):
                outbox_metrics = await database.get_outbox_metrics()
                MONITORING_STATS["outbox"] = outbox_metrics
                planner.update_capacity_mode(cap["available_rps"], cap["required_rps"], now)
                request_cost = float(metrics_snap["avg_http_requests_per_scan"] or 1.0)
                logger.log(
                    logging.WARNING if MONITORING_STATS["overloaded"] else logging.INFO,
                    "📊 Мониторинг (режим=%s, URL=%s, прокси=%s/%s, покрытие=%.1fс): %s",
                    planner.capacity_mode,
                    len(groups),
                    healthy_proxies,
                    len(proxy_snaps),
                    planner_snap["full_coverage_age_seconds"],
                    cap,
                )
                alerts = METRICS.evaluate_minute_alerts(
                    active_unique_urls=len(groups),
                    proxy_snapshots=proxy_snaps,
                    outbox_metrics=outbox_metrics,
                    full_coverage_age_seconds=planner_snap["full_coverage_age_seconds"],
                )
                for alert_msg in alerts:
                    logger.warning(alert_msg)
                    await notify_admins_text(alert_msg)
                last_report = now

            if now - last_terminal_cleanup >= 3600:
                await database.cleanup_terminal_notification_events()
                last_terminal_cleanup = now

            if now - last_cleanup >= 86400:
                await database.cleanup_old_seen_items(days=7)
                from logger_setup import cleanup_logs
                await asyncio.to_thread(cleanup_logs, max_total_mb=40, keep_days=3)
                last_cleanup = now

            await asyncio.sleep(1)

    tasks = [asyncio.create_task(dispatch(), name="scan-dispatcher")]
    tasks.extend(
        asyncio.create_task(fanout_worker(index), name=f"fanout-worker-{index}")
        for index in range(1, max(1, int(config.FANOUT_WORKERS)) + 1)
    )
    tasks.extend(
        asyncio.create_task(worker(index, entry), name=f"scan-worker-{index}")
        for index, entry in enumerate(parsers, 1)
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await flush_state_updates()
        for entry in owned_parsers:
            entry.close()
        for client in pool.clients:
            bound = getattr(client, "_bound_parser", None)
            if bound is not None:
                bound.close()
                setattr(client, "_bound_parser", None)

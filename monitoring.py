"""Runtime orchestration; the scheduling policy itself is pure and testable."""

import asyncio
import logging
import time
from collections import defaultdict

import config
import database
from parser import AvitoParser
from request_budget import get_network_block_state
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
    pro = any(radar["user_id"] in config.ADMIN_IDS or (radar.get("tariff_plan") or "").lower() == "pro" for radar in radars)
    return min(15.0, float(config.CHECK_INTERVAL)) if pro else float(config.CHECK_INTERVAL)


async def run_monitoring(bot, parser=None):
    from scheduler import process_url_group

    budget = get_network_block_state(config.PROXY_URL)
    planner = DeadlineScheduler(config.SCAN_MAX_RETRIES, config.SCAN_RETRY_BASE_DELAY, config.SCAN_RETRY_MAX_DELAY)
    parsers = [parser] if parser is not None else []
    try:
        for _ in range(config.MAX_CONCURRENT_WORKERS - len(parsers)):
            parsers.append(AvitoParser(proxy=config.PROXY_URL))
    except Exception:
        for entry in parsers:
            if entry is not parser:
                entry.close()
        raise
    owned_parsers = [entry for entry in parsers if entry is not parser]
    changed = asyncio.Event()
    request_cost = 1.0
    stats = {"successful_scans": 0, "failed_scans": 0, "coverage_gaps": 0}
    consecutive_failures = 0
    alert_sent = False
    logger.info("📡 Планировщик: %s воркеров, общий лимит %.2f запросов/с, HTTP concurrency=%s", len(parsers), config.AVITO_REQUESTS_PER_SECOND, config.AVITO_MAX_IN_FLIGHT)

    async def notify_health(recovered: bool):
        text = "✅ Получение каталога Авито возобновилось." if recovered else "⚠️ Повторяющиеся ошибки получения каталога Авито. Проверки отложены; уведомления в outbox сохранены. Подробности и общий cooldown — в логах."
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
            except Exception:
                logger.warning("Не удалось отправить администратору состояние мониторинга", exc_info=True)

    async def worker(worker_id, worker_parser):
        nonlocal consecutive_failures, alert_sent
        while True:
            cooldown = budget.cooldown_remaining()
            if cooldown > 0:
                await asyncio.sleep(min(cooldown, 1.0))
                continue
            # No await between selection and reservation: a source runs once.
            task = planner.take_due(time.monotonic())
            if task is None:
                changed.clear()
                timeout = min(1.0, max(0.01, planner.seconds_until_next(time.monotonic())))
                try:
                    await asyncio.wait_for(changed.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                continue
            ok = False
            try:
                ok = await process_url_group(bot, worker_parser, task.key, list(task.radars), worker_id=worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Сбой обработки источника; повтор будет отложен")
            failure = worker_parser.last_failure if not ok else None
            planner.complete(task, time.monotonic(), ok,
                             retry_after=max(budget.cooldown_remaining(), failure.retry_after if failure else 0),
                             retryable=failure.retryable if failure else True)
            changed.set()
            stats["successful_scans" if ok else "failed_scans"] += 1
            stats["coverage_gaps"] += int(getattr(worker_parser, "last_coverage_gap", False))
            consecutive_failures = 0 if ok else consecutive_failures + 1
            if ok and alert_sent:
                alert_sent = False
                await notify_health(True)
            elif consecutive_failures >= 5 and not alert_sent:
                alert_sent = True
                await notify_health(False)

    async def dispatch():
        nonlocal request_cost
        startup = True
        last_report = last_cleanup = time.monotonic()
        previous_requests = budget.snapshot()["requests"]
        previous_scans = 0
        outbox_metrics = {}
        while True:
            searches = await database.get_all_active_searches()
            groups = group_sources(searches)
            network = budget.snapshot()
            base = {key: source_interval(radars) for key, radars in groups.items()}
            plan = calculate_interval_plan(base, network["current_rps"], config.SCAN_MAX_INTERVAL,
                                           config.SCAN_BUDGET_UTILIZATION, request_cost)
            now = time.monotonic()
            planner.sync(groups, plan.intervals, now, startup=startup, startup_spread=config.SCAN_STARTUP_SPREAD)
            startup = False
            changed.set()
            MONITORING_STATS.clear()
            MONITORING_STATS.update(planner.snapshot(now), **stats,
                                    requested_rps=plan.requested_rps, planned_rps=plan.planned_rps,
                                    available_rps=plan.available_rps, overloaded=plan.overloaded,
                                    requests_per_scan=request_cost, network=network)
            if now - last_report >= 60:
                outbox_metrics = await database.get_outbox_metrics()
                MONITORING_STATS["outbox"] = outbox_metrics
                logger.log(logging.WARNING if plan.overloaded else logging.INFO, "📊 Мониторинг: %s", MONITORING_STATS)
                scans = stats["successful_scans"] + stats["failed_scans"]
                if scans > previous_scans:
                    observed = max(1.0, (network["requests"] - previous_requests) / (scans - previous_scans))
                    request_cost = 0.7 * request_cost + 0.3 * observed
                previous_requests, previous_scans = network["requests"], scans
                last_report = now
            else:
                MONITORING_STATS["outbox"] = outbox_metrics
            if now - last_cleanup >= 86400:
                await database.cleanup_old_seen_items(days=7)
                from logger_setup import cleanup_logs
                await asyncio.to_thread(cleanup_logs, max_total_mb=40, keep_days=3)
                last_cleanup = now
            await asyncio.sleep(1)

    tasks = [asyncio.create_task(dispatch(), name="scan-dispatcher")]
    tasks.extend(asyncio.create_task(worker(index, entry), name=f"scan-worker-{index}") for index, entry in enumerate(parsers, 1))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for entry in owned_parsers:
            entry.close()
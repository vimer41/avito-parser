"""Кольцевые runtime-метрики (60 минутных бакетов), калькулятор емкости и алерты."""

from __future__ import annotations

import bisect
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

import config

SUBSECOND_LATENCY_BOUNDS: tuple[float, ...] = (
    0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00, 10.0, 30.0, 60.0, float("inf")
)
HTTP_LATENCY_BOUNDS: tuple[float, ...] = (
    0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 5.00, 8.00, 12.0, 20.0, float("inf")
)
SCAN_INTERVAL_BOUNDS: tuple[float, ...] = (
    0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 17.5, 20.0, 25.0, 30.0, 45.0, 60.0, 120.0, 300.0, float("inf")
)
HTTP_COST_BOUNDS: tuple[float, ...] = (
    1.0, 1.2, 1.3, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0, float("inf")
)
PUB_DELAY_BOUNDS: tuple[float, ...] = (
    5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0, 1800.0, float("inf")
)

TARIFF_KEYS: tuple[str, ...] = ("pro", "standart", "base")

_URL_CREDENTIALS_RE = re.compile(
    r"(?i)\b(https?|socks5h?)://([^\s/@:]+):([^\s/@]+)@([^\s/]+)"
)
_COLON_PROXY_RE = re.compile(
    r"\b((?:\d{1,3}\.){3}\d{1,3}:\d{2,5}):([^\s:]+):([^\s]+)\b"
)


def sanitize_proxy_credentials(text: str | None) -> str:
    """Маскирует логин и пароль прокси в любом тексте лога или ошибки."""
    if not text:
        return ""
    masked = _URL_CREDENTIALS_RE.sub(r"\1://***@\4", str(text))
    masked = _COLON_PROXY_RE.sub(r"\1:***", masked)
    return masked


def mask_proxy_url(proxy_url: str | None) -> str:
    if not proxy_url or not proxy_url.strip():
        return "direct://local"
    normalized = config.parse_proxy(proxy_url) or proxy_url.strip()
    return sanitize_proxy_credentials(normalized)


@dataclass
class HistogramSlice:
    bounds: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    sample_count: int = 0
    total_sum: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.bounds)

    def reset(self) -> None:
        for i in range(len(self.counts)):
            self.counts[i] = 0
        self.sample_count = 0
        self.total_sum = 0.0

    def observe(self, value: float) -> None:
        if not math.isfinite(value):
            return
        v = max(0.0, float(value))
        idx = bisect.bisect_left(self.bounds, v)
        if idx >= len(self.bounds):
            idx = len(self.bounds) - 1
        self.counts[idx] += 1
        self.sample_count += 1
        self.total_sum += v


def summarize_histograms(slices: Iterable[HistogramSlice], bounds: tuple[float, ...]) -> dict[str, Any]:
    merged = [0] * len(bounds)
    total_samples = 0
    total_sum = 0.0
    for sl in slices:
        if sl.sample_count <= 0:
            continue
        total_samples += sl.sample_count
        total_sum += sl.total_sum
        for idx, c in enumerate(sl.counts):
            merged[idx] += c

    if total_samples == 0:
        return {
            "sample_count": 0,
            "mean": None,
            "p50": None,
            "p95": None,
        }

    def _percentile(q: float) -> float:
        target = max(1, math.ceil(total_samples * q))
        running = 0
        for idx, count in enumerate(merged):
            running += count
            if running >= target:
                bound = bounds[idx]
                if math.isinf(bound) and idx > 0:
                    return bounds[idx - 1]
                return bound
        return bounds[-2] if len(bounds) > 1 and math.isinf(bounds[-1]) else bounds[-1]

    return {
        "sample_count": total_samples,
        "mean": round(total_sum / total_samples, 4),
        "p50": _percentile(0.50),
        "p95": _percentile(0.95),
    }


def _make_tariff_histograms(bounds: tuple[float, ...]) -> dict[str, HistogramSlice]:
    return {k: HistogramSlice(bounds) for k in TARIFF_KEYS}


@dataclass
class ProxyMinuteSlice:
    minute_id: int = -1
    raw_http_total: int = 0
    raw_http_2xx_3xx: int = 0
    raw_http_403: int = 0
    raw_http_429: int = 0
    raw_http_5xx: int = 0
    transport_errors: int = 0
    collection_total: int = 0
    collection_success: int = 0
    challenges_solved: int = 0
    final_blocks: int = 0
    http_latency: HistogramSlice = field(default_factory=lambda: HistogramSlice(HTTP_LATENCY_BOUNDS))

    def reset(self, minute_id: int) -> None:
        self.minute_id = minute_id
        self.raw_http_total = 0
        self.raw_http_2xx_3xx = 0
        self.raw_http_403 = 0
        self.raw_http_429 = 0
        self.raw_http_5xx = 0
        self.transport_errors = 0
        self.collection_total = 0
        self.collection_success = 0
        self.challenges_solved = 0
        self.final_blocks = 0
        self.http_latency.reset()


@dataclass
class MinuteBucket:
    minute_id: int = -1
    # Histograms (normal flow)
    pub_to_seen: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(PUB_DELAY_BOUNDS))
    seen_to_queued: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    queued_to_sent: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    deliver_at_to_sent: dict[str, HistogramSlice] = field(default_factory=lambda: _make_tariff_histograms(SUBSECOND_LATENCY_BOUNDS))
    deliver_at_to_sent_all: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    queued_to_sent_all: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))

    # Catch-up flow histograms (separated from normal SLA)
    catchup_queued_to_sent: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    catchup_deliver_at_to_sent: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))

    # Scan & HTTP & Bot API
    http_cost_per_scan: HistogramSlice = field(default_factory=lambda: HistogramSlice(HTTP_COST_BOUNDS))
    scan_duration: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    source_interval: HistogramSlice = field(default_factory=lambda: HistogramSlice(SCAN_INTERVAL_BOUNDS))
    fanout_duration: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))
    bot_api_service_time: HistogramSlice = field(default_factory=lambda: HistogramSlice(SUBSECOND_LATENCY_BOUNDS))

    # Counters
    pub_timestamp_missing_count: int = 0
    scans_completed: int = 0
    scans_fast_skipped: int = 0
    scans_unchanged: int = 0
    scans_with_new_items: int = 0
    scans_failed: int = 0
    bot_api_sent_total: int = 0
    bot_api_retry_after_count: int = 0
    photo_file_id_hit_count: int = 0
    photo_url_direct_count: int = 0
    photo_cdn_upload_count: int = 0
    photo_fallback_text_count: int = 0
    text_only_sent_count: int = 0
    catchup_sent_count: int = 0
    catchup_omitted_count: int = 0

    def reset(self, minute_id: int) -> None:
        self.minute_id = minute_id
        for d in (self.pub_to_seen, self.seen_to_queued, self.queued_to_sent, self.deliver_at_to_sent):
            for sl in d.values():
                sl.reset()
        self.deliver_at_to_sent_all.reset()
        self.queued_to_sent_all.reset()
        self.catchup_queued_to_sent.reset()
        self.catchup_deliver_at_to_sent.reset()
        self.http_cost_per_scan.reset()
        self.scan_duration.reset()
        self.source_interval.reset()
        self.fanout_duration.reset()
        self.bot_api_service_time.reset()
        self.pub_timestamp_missing_count = 0
        self.scans_completed = 0
        self.scans_fast_skipped = 0
        self.scans_unchanged = 0
        self.scans_with_new_items = 0
        self.scans_failed = 0
        self.bot_api_sent_total = 0
        self.bot_api_retry_after_count = 0
        self.photo_file_id_hit_count = 0
        self.photo_url_direct_count = 0
        self.photo_cdn_upload_count = 0
        self.photo_fallback_text_count = 0
        self.text_only_sent_count = 0
        self.catchup_sent_count = 0
        self.catchup_omitted_count = 0


def calculate_capacity_report(
    *,
    unique_sources: int,
    proxy_endpoints: list[dict[str, Any]],
    rolling_mean_cost: float | None,
    p95_cost: float | None,
    completed_scans_count: int,
    interval_min: float | None = None,
    interval_max: float | None = None,
    initial_cost: float | None = None,
    min_samples: int | None = None,
    reserve_fraction: float | None = None,
    recommendation_max_rate: float | None = None,
) -> dict[str, Any]:
    s_min = float(config.SCAN_INTERVAL_MIN if interval_min is None else interval_min)
    s_max = float(config.SCAN_INTERVAL_MAX if interval_max is None else interval_max)
    s_target = (s_min + s_max) / 2.0
    init_cost = float(config.PROXY_INITIAL_HTTP_COST_PER_SCAN if initial_cost is None else initial_cost)
    min_samp = int(config.HTTP_COST_MIN_SAMPLES if min_samples is None else min_samples)
    reserve = float(config.PROXY_CAPACITY_RESERVE if reserve_fraction is None else reserve_fraction)
    rec_max = float(
        config.PROXY_RATE_RECOMMENDATION_MAX if recommendation_max_rate is None else recommendation_max_rate
    )

    use_cold_start = completed_scans_count < min_samp or rolling_mean_cost is None
    effective_mean_cost = init_cost if use_cold_start else max(1.0, float(rolling_mean_cost))
    effective_p95_cost = init_cost if (use_cold_start or p95_cost is None) else max(effective_mean_cost, float(p95_cost))

    u = max(0, int(unique_sources))
    required_rate_at_max_mean = round(u * effective_mean_cost / s_max, 4)
    required_rate_target_mean = round(u * effective_mean_cost / s_target, 4)
    required_rate_at_max_p95 = round(u * effective_p95_cost / s_max, 4)
    required_rate_target_p95 = round(u * effective_p95_cost / s_target, 4)

    total_proxies = len(proxy_endpoints)
    healthy_list = [
        p for p in proxy_endpoints
        if p.get("state") in ("healthy", "half_open")
        and float(p.get("effective_rate") or p.get("current_rps") or 0.0) > 0
    ]
    quarantined_list = [p for p in proxy_endpoints if p.get("state") == "quarantined"]
    healthy_count = len(healthy_list)
    quarantined_count = len(quarantined_list)

    available_rate_now = round(
        sum(float(p.get("effective_rate") or p.get("current_rps") or 0.0) for p in healthy_list), 4
    )

    # Reserve: exclude top 20% endpoints by effective_rate; already quarantined count toward the 20%
    reserve_target_healthy = math.floor(total_proxies * (1.0 - reserve)) if total_proxies > 0 else 0
    if total_proxies == 1 and healthy_count == 1 and reserve < 1.0:
        # With 1 proxy, reserve cannot drop below 0; reserve_keep_count is 0 for strict 20% reserve or 1 for capacity_proxy_count
        reserve_keep_count = 0
    else:
        reserve_keep_count = min(healthy_count, max(0, reserve_target_healthy))

    sorted_healthy = sorted(
        healthy_list,
        key=lambda item: float(item.get("effective_rate") or item.get("current_rps") or 0.0),
    )
    kept_for_reserve = sorted_healthy[:reserve_keep_count]
    available_rate_with_reserve = round(
        sum(float(p.get("effective_rate") or p.get("current_rps") or 0.0) for p in kept_for_reserve), 4
    )

    deficit_now = round(max(0.0, required_rate_at_max_mean - available_rate_now), 4)
    deficit_with_reserve = round(max(0.0, required_rate_target_mean - available_rate_with_reserve), 4)
    deficit_now_p95 = round(max(0.0, required_rate_at_max_p95 - available_rate_now), 4)
    deficit_with_reserve_p95 = round(max(0.0, required_rate_target_p95 - available_rate_with_reserve), 4)

    if total_proxies > 0 and healthy_count > 0:
        capacity_proxy_count = max(1, min(healthy_count, max(1, reserve_target_healthy)))
    elif total_proxies > 0:
        capacity_proxy_count = max(1, reserve_target_healthy)
    else:
        capacity_proxy_count = 1

    required_configured_rate_hard = round(required_rate_at_max_mean / capacity_proxy_count, 4)
    required_configured_rate_target = round(required_rate_target_mean / capacity_proxy_count, 4)
    required_configured_rate_hard_p95 = round(required_rate_at_max_p95 / capacity_proxy_count, 4)
    required_configured_rate_target_p95 = round(required_rate_target_p95 / capacity_proxy_count, 4)

    # Determine planner capacity mode
    if u == 0 or available_rate_with_reserve >= required_rate_target_mean:
        capacity_mode = "normal"
        recommended_interval_low = s_min
    elif available_rate_now >= required_rate_target_mean:
        capacity_mode = "full_capacity"
        recommended_interval_low = min(s_max, max(s_min, 16.0))
    elif available_rate_now >= required_rate_at_max_mean:
        capacity_mode = "boundary"
        recommended_interval_low = min(s_max, max(s_min, 18.0))
    else:
        capacity_mode = "deficit"
        recommended_interval_low = min(s_max, max(s_min, 19.0))

    # Next safe manual step evaluation across pool
    total_raw_60m = sum(int(p.get("raw_http_total_60m", 0)) for p in proxy_endpoints)
    total_2xx_3xx_60m = sum(int(p.get("raw_http_2xx_3xx_60m", 0)) for p in proxy_endpoints)
    total_403_429_10m = sum(int(p.get("raw_http_403_429_10m", 0)) for p in proxy_endpoints)
    raw_success_ratio_60m = (total_2xx_3xx_60m / total_raw_60m) if total_raw_60m > 0 else None

    p95_latencies = [
        float(p["p95_latency_seconds"])
        for p in proxy_endpoints
        if p.get("p95_latency_seconds") is not None and float(p["p95_latency_seconds"]) > 0
    ]
    pool_p95_latency = max(p95_latencies) if p95_latencies else None
    configured_rates = [float(p.get("configured_rate", config.PROXY_RATE_LIMIT)) for p in proxy_endpoints]
    current_configured_rate = min(configured_rates) if configured_rates else float(config.PROXY_RATE_LIMIT)

    latency_rate_cap = round(0.85 / pool_p95_latency, 4) if (pool_p95_latency and pool_p95_latency > 0) else rec_max

    eligible_for_step = (
        total_raw_60m >= 100
        and total_403_429_10m == 0
        and raw_success_ratio_60m is not None
        and raw_success_ratio_60m >= 0.99
    )

    needs_more_proxies = (
        required_configured_rate_target > rec_max
        or required_configured_rate_target > latency_rate_cap
        or total_proxies == 0
    )

    if eligible_for_step:
        next_safe_manual_step = round(
            min(current_configured_rate * 1.25, latency_rate_cap, rec_max),
            4,
        )
    else:
        next_safe_manual_step = None

    if total_proxies == 0:
        recommendation_ru = "добавить прокси (пул прокси пуст)"
    elif needs_more_proxies and deficit_with_reserve > 0:
        recommendation_ru = (
            f"добавить прокси (требуется {required_configured_rate_target:.2f} req/s/прокси "
            f"при потолке {min(rec_max, latency_rate_cap):.2f} req/s)"
        )
    elif deficit_now > 0:
        if next_safe_manual_step and next_safe_manual_step > current_configured_rate:
            recommendation_ru = (
                f"дефицит емкости: можно осторожно поднять PROXY_RATE_LIMIT до {next_safe_manual_step:.2f} "
                "или добавить прокси"
            )
        else:
            recommendation_ru = "дефицит емкости: добавить прокси (условия для повышения PROXY_RATE_LIMIT не выполнены)"
    elif eligible_for_step and next_safe_manual_step and next_safe_manual_step > current_configured_rate:
        recommendation_ru = f"емкость в норме; допустим ручной шаг PROXY_RATE_LIMIT до {next_safe_manual_step:.2f}"
    else:
        recommendation_ru = "емкость в норме; изменение PROXY_RATE_LIMIT не требуется"

    return {
        "unique_sources": u,
        "cold_start_cost_used": use_cold_start,
        "rolling_mean_http_requests_per_scan": round(effective_mean_cost, 4),
        "p95_http_requests_per_scan": round(effective_p95_cost, 4),
        "required_rate_at_max": required_rate_at_max_mean,
        "required_rate_target": required_rate_target_mean,
        "required_rate_at_max_p95": required_rate_at_max_p95,
        "required_rate_target_p95": required_rate_target_p95,
        "total_proxies": total_proxies,
        "healthy_proxies": healthy_count,
        "quarantined_proxies": quarantined_count,
        "capacity_proxy_count": capacity_proxy_count,
        "available_rate_now": available_rate_now,
        "available_rate_with_reserve": available_rate_with_reserve,
        "deficit_now": deficit_now,
        "deficit_with_reserve": deficit_with_reserve,
        "deficit_now_p95": deficit_now_p95,
        "deficit_with_reserve_p95": deficit_with_reserve_p95,
        "required_configured_rate_hard": required_configured_rate_hard,
        "required_configured_rate_target": required_configured_rate_target,
        "required_configured_rate_hard_p95": required_configured_rate_hard_p95,
        "required_configured_rate_target_p95": required_configured_rate_target_p95,
        "capacity_mode": capacity_mode,
        "recommended_interval_low": recommended_interval_low,
        "recommended_interval_high": s_max,
        "eligible_for_rate_increase": eligible_for_step,
        "next_safe_manual_step": next_safe_manual_step,
        "latency_rate_cap": latency_rate_cap,
        "needs_more_proxies": needs_more_proxies,
        "recommendation_ru": recommendation_ru,
    }


def calculate_required_bot_workers(
    p95_service_time_seconds: float | None,
    target_rate: float | None = None,
) -> int:
    rate = float(config.TELEGRAM_GLOBAL_RATE if target_rate is None else target_rate)
    if p95_service_time_seconds is None or p95_service_time_seconds <= 0:
        return config.NOTIFICATION_WORKERS
    return max(1, math.ceil(rate * float(p95_service_time_seconds) * 1.2))


@dataclass
class AlertRuleState:
    consecutive_bad: int = 0
    active: bool = False
    last_sent_at: float = -1e9


class RuntimeMetricsRegistry:
    """Потокобезопасный in-memory реестр 60-минутных метрик и детектор алертов."""

    NUM_BUCKETS = 60

    def __init__(
        self,
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._monotonic = monotonic_fn
        self._wall_clock = wall_clock_fn
        self._buckets: list[MinuteBucket] = [MinuteBucket() for _ in range(self.NUM_BUCKETS)]
        self._proxy_rings: dict[str, list[ProxyMinuteSlice]] = {}
        self._proxy_meta: dict[str, dict[str, Any]] = {}
        self._alert_states: dict[str, AlertRuleState] = {
            "delivery_sla": AlertRuleState(),
            "outbox_depth": AlertRuleState(),
            "capacity_deficit": AlertRuleState(),
        }
        self._pending_depth_history: list[int] = []
        self._alert_cooldown_seconds: float = 300.0
        self._last_capacity_report: dict[str, Any] = {}
        self._outbox_live_state: dict[str, Any] = {
            "pending_depth": 0,
            "leased_depth": 0,
            "oldest_pending_age_seconds": 0.0,
        }
        self._scheduler_live_state: dict[str, Any] = {
            "unique_sources": 0,
            "overdue_sources": 0,
            "backoff_sources": 0,
            "full_coverage_age_seconds": None,
            "capacity_mode": "normal",
        }

    def _current_minute_id(self) -> int:
        return int(self._monotonic() // 60.0)

    def _bucket(self) -> MinuteBucket:
        mid = self._current_minute_id()
        idx = mid % self.NUM_BUCKETS
        bucket = self._buckets[idx]
        if bucket.minute_id != mid:
            bucket.reset(mid)
        return bucket

    def _active_buckets(self, window_minutes: int = 60) -> list[MinuteBucket]:
        now_mid = self._current_minute_id()
        min_mid = now_mid - max(1, min(self.NUM_BUCKETS, window_minutes)) + 1
        return [b for b in self._buckets if min_mid <= b.minute_id <= now_mid]

    def _proxy_slice(self, proxy_id: str, masked_url: str | None = None) -> ProxyMinuteSlice:
        if proxy_id not in self._proxy_rings:
            self._proxy_rings[proxy_id] = [ProxyMinuteSlice() for _ in range(self.NUM_BUCKETS)]
        if masked_url and proxy_id not in self._proxy_meta:
            self._proxy_meta[proxy_id] = {
                "proxy_id": proxy_id,
                "masked_url": sanitize_proxy_credentials(masked_url),
                "state": "healthy",
                "configured_rate": config.PROXY_RATE_LIMIT,
                "effective_rate": config.PROXY_RATE_LIMIT,
                "quarantined_until": 0.0,
            }
        mid = self._current_minute_id()
        idx = mid % self.NUM_BUCKETS
        sl = self._proxy_rings[proxy_id][idx]
        if sl.minute_id != mid:
            sl.reset(mid)
        return sl

    def update_proxy_state(
        self,
        proxy_id: str,
        *,
        masked_url: str,
        state: str,
        configured_rate: float,
        effective_rate: float,
        quarantined_until: float = 0.0,
    ) -> None:
        self._proxy_slice(proxy_id, masked_url)
        self._proxy_meta[proxy_id] = {
            "proxy_id": proxy_id,
            "masked_url": sanitize_proxy_credentials(masked_url),
            "state": state,
            "configured_rate": float(configured_rate),
            "effective_rate": float(effective_rate),
            "quarantined_until": float(quarantined_until),
        }

    def record_http_request(
        self,
        proxy_id: str,
        masked_url: str,
        status_code: int | None,
        latency_seconds: float,
        *,
        is_transport_error: bool = False,
    ) -> None:
        sl = self._proxy_slice(proxy_id, masked_url)
        sl.raw_http_total += 1
        sl.http_latency.observe(latency_seconds)
        if is_transport_error or status_code is None:
            sl.transport_errors += 1
            return
        if 200 <= status_code < 400:
            sl.raw_http_2xx_3xx += 1
        elif status_code == 403:
            sl.raw_http_403 += 1
        elif status_code == 429:
            sl.raw_http_429 += 1
        elif status_code >= 500:
            sl.raw_http_5xx += 1

    def record_collection(
        self,
        proxy_id: str,
        masked_url: str,
        *,
        success: bool,
        http_requests_count: int,
        duration_seconds: float,
        challenge_solved: bool = False,
        final_block: bool = False,
        fast_skipped: bool = False,
        unchanged: bool = False,
        has_new_items: bool = False,
    ) -> None:
        b = self._bucket()
        sl = self._proxy_slice(proxy_id, masked_url)
        sl.collection_total += 1
        if challenge_solved:
            sl.challenges_solved += 1
        if final_block:
            sl.final_blocks += 1

        if success:
            sl.collection_success += 1
            b.scans_completed += 1
            if http_requests_count > 0:
                b.http_cost_per_scan.observe(float(http_requests_count))
            b.scan_duration.observe(duration_seconds)
            if fast_skipped:
                b.scans_fast_skipped += 1
            if unchanged:
                b.scans_unchanged += 1
            if has_new_items:
                b.scans_with_new_items += 1
        else:
            b.scans_failed += 1

    def record_source_interval(self, interval_seconds: float) -> None:
        if interval_seconds > 0:
            self._bucket().source_interval.observe(interval_seconds)

    def record_fanout_batch(self, duration_seconds: float) -> None:
        self._bucket().fanout_duration.observe(duration_seconds)

    def record_publication_delay(
        self,
        tariff: str,
        pub_to_seen_seconds: float | None,
        *,
        reliable_t_pub: bool = True,
        is_catchup: bool = False,
    ) -> None:
        b = self._bucket()
        if not reliable_t_pub or pub_to_seen_seconds is None:
            b.pub_timestamp_missing_count += 1
            return
        if is_catchup:
            return
        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.pub_to_seen[t_key].observe(pub_to_seen_seconds)

    def record_seen_to_queued(self, tariff: str, seconds: float, *, is_catchup: bool = False) -> None:
        if is_catchup:
            return
        b = self._bucket()
        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.seen_to_queued[t_key].observe(seconds)

    def record_delivery(
        self,
        tariff: str,
        *,
        queued_to_sent_seconds: float | None = None,
        deliver_at_to_sent_seconds: float | None = None,
        bot_api_service_seconds: float = 0.0,
        delivery_mode: str = "text",
        is_catchup: bool = False,
        t_pub: float | None = None,
        t_pub_reliable: bool = False,
        t_seen: float | None = None,
        t_queued: float | None = None,
        deliver_at: float | None = None,
        t_sent: float | None = None,
    ) -> None:
        if t_sent is not None and t_queued is not None and queued_to_sent_seconds is None:
            queued_to_sent_seconds = max(0.0, float(t_sent) - float(t_queued))
        if t_sent is not None and deliver_at is not None and deliver_at_to_sent_seconds is None:
            deliver_at_to_sent_seconds = max(0.0, float(t_sent) - float(deliver_at))
        if t_seen is not None:
            pub_delay = max(0.0, float(t_seen) - float(t_pub)) if (t_pub is not None and t_pub_reliable) else None
            self.record_publication_delay(
                tariff,
                pub_delay,
                reliable_t_pub=bool(t_pub_reliable and t_pub is not None),
                is_catchup=is_catchup,
            )
            if t_queued is not None:
                self.record_seen_to_queued(tariff, max(0.0, float(t_queued) - float(t_seen)), is_catchup=is_catchup)

        q_sec = float(queued_to_sent_seconds or 0.0)
        d_sec = float(deliver_at_to_sent_seconds or 0.0)

        b = self._bucket()
        b.bot_api_sent_total += 1
        b.bot_api_service_time.observe(float(bot_api_service_seconds or 0.0))

        if delivery_mode == "photo_file_id":
            b.photo_file_id_hit_count += 1
        elif delivery_mode == "photo_url":
            b.photo_url_direct_count += 1
        elif delivery_mode == "photo_upload":
            b.photo_cdn_upload_count += 1
        elif delivery_mode == "photo_fallback_text":
            b.photo_fallback_text_count += 1
        else:
            b.text_only_sent_count += 1

        if is_catchup:
            b.catchup_sent_count += 1
            b.catchup_queued_to_sent.observe(q_sec)
            b.catchup_deliver_at_to_sent.observe(d_sec)
            return

        t_key = tariff if tariff in TARIFF_KEYS else "base"
        b.queued_to_sent[t_key].observe(q_sec)
        b.deliver_at_to_sent[t_key].observe(d_sec)
        b.queued_to_sent_all.observe(q_sec)
        b.deliver_at_to_sent_all.observe(d_sec)

    def record_outbox_expired(self, count: int = 1) -> None:
        pass

    def record_outbox_failed(self, count: int = 1) -> None:
        pass

    def record_scan_attempt(
        self,
        *,
        success: bool,
        elapsed_ms: float = 0.0,
        pages_fetched: int = 0,
        http_requests: int = 1,
        fast_skipped: bool = False,
        coverage_gap: bool = False,
        revisit_seconds: float | None = None,
        due_lag_seconds: float | None = None,
        is_catchup: bool = False,
        proxy_id: str = "direct",
        masked_url: str = "direct",
    ) -> None:
        if revisit_seconds is not None and revisit_seconds > 0:
            self.record_source_interval(revisit_seconds)
        self.record_collection(
            proxy_id,
            masked_url,
            success=success,
            http_requests_count=max(0, int(http_requests)),
            duration_seconds=max(0.0, float(elapsed_ms) / 1000.0),
            fast_skipped=fast_skipped,
        )

    def record_catchup_omitted(self, omitted_count: int) -> None:
        if omitted_count > 0:
            self._bucket().catchup_omitted_count += int(omitted_count)

    def record_bot_api_retry_after(self) -> None:
        self._bucket().bot_api_retry_after_count += 1

    def update_outbox_state(
        self,
        *,
        pending_depth: int,
        leased_depth: int = 0,
        oldest_pending_age_seconds: float = 0.0,
    ) -> None:
        self._outbox_live_state = {
            "pending_depth": max(0, int(pending_depth)),
            "leased_depth": max(0, int(leased_depth)),
            "oldest_pending_age_seconds": round(max(0.0, float(oldest_pending_age_seconds)), 2),
        }

    def update_scheduler_state(
        self,
        *,
        unique_sources: int,
        overdue_sources: int = 0,
        backoff_sources: int = 0,
        full_coverage_age_seconds: float | None = None,
        capacity_mode: str = "normal",
    ) -> None:
        self._scheduler_live_state = {
            "unique_sources": max(0, int(unique_sources)),
            "overdue_sources": max(0, int(overdue_sources)),
            "backoff_sources": max(0, int(backoff_sources)),
            "full_coverage_age_seconds": (
                round(float(full_coverage_age_seconds), 2)
                if full_coverage_age_seconds is not None
                else None
            ),
            "capacity_mode": capacity_mode,
        }

    def get_proxy_summaries(self) -> list[dict[str, Any]]:
        now_mid = self._current_minute_id()
        min_60 = now_mid - 59
        min_10 = now_mid - 9
        results: list[dict[str, Any]] = []
        for proxy_id, meta in sorted(self._proxy_meta.items()):
            ring = self._proxy_rings.get(proxy_id, [])
            slices_60 = [s for s in ring if min_60 <= s.minute_id <= now_mid]
            slices_10 = [s for s in ring if min_10 <= s.minute_id <= now_mid]

            raw_60 = sum(s.raw_http_total for s in slices_60)
            ok_60 = sum(s.raw_http_2xx_3xx for s in slices_60)
            c403_60 = sum(s.raw_http_403 for s in slices_60)
            c429_60 = sum(s.raw_http_429 for s in slices_60)
            c5xx_60 = sum(s.raw_http_5xx for s in slices_60)
            terr_60 = sum(s.transport_errors for s in slices_60)
            coll_total_60 = sum(s.collection_total for s in slices_60)
            coll_ok_60 = sum(s.collection_success for s in slices_60)
            chal_60 = sum(s.challenges_solved for s in slices_60)
            fblk_60 = sum(s.final_blocks for s in slices_60)

            raw_10 = sum(s.raw_http_total for s in slices_10)
            c403_429_10 = sum(s.raw_http_403 + s.raw_http_429 for s in slices_10)

            lat_summary = summarize_histograms([s.http_latency for s in slices_60], HTTP_LATENCY_BOUNDS)

            results.append({
                "proxy_id": proxy_id,
                "masked_url": meta["masked_url"],
                "state": meta["state"],
                "configured_rate": meta["configured_rate"],
                "effective_rate": meta["effective_rate"],
                "quarantined_until": meta["quarantined_until"],
                "raw_http_total_60m": raw_60,
                "raw_http_2xx_3xx_60m": ok_60,
                "raw_http_success_ratio_60m": round(ok_60 / raw_60, 4) if raw_60 > 0 else None,
                "raw_http_403_60m": c403_60,
                "raw_http_429_60m": c429_60,
                "raw_http_5xx_60m": c5xx_60,
                "transport_errors_60m": terr_60,
                "raw_http_total_10m": raw_10,
                "raw_http_403_429_10m": c403_429_10,
                "collection_total_60m": coll_total_60,
                "collection_success_60m": coll_ok_60,
                "collection_success_ratio_60m": round(coll_ok_60 / coll_total_60, 4) if coll_total_60 > 0 else None,
                "challenges_solved_60m": chal_60,
                "final_blocks_60m": fblk_60,
                "p50_latency_seconds": lat_summary["p50"],
                "p95_latency_seconds": lat_summary["p95"],
                "latency_samples": lat_summary["sample_count"],
            })
        return results

    def snapshot(
        self,
        window_minutes: int = 60,
        *,
        active_unique_urls: int | None = None,
        proxy_snapshots: list[dict[str, Any]] | None = None,
        outbox_metrics: dict[str, Any] | None = None,
        full_coverage_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        if active_unique_urls is not None or full_coverage_age_seconds is not None:
            self.update_scheduler_state(
                unique_sources=(
                    active_unique_urls
                    if active_unique_urls is not None
                    else self._scheduler_live_state["unique_sources"]
                ),
                overdue_sources=self._scheduler_live_state["overdue_sources"],
                backoff_sources=self._scheduler_live_state["backoff_sources"],
                full_coverage_age_seconds=(
                    full_coverage_age_seconds
                    if full_coverage_age_seconds is not None
                    else self._scheduler_live_state["full_coverage_age_seconds"]
                ),
                capacity_mode=self._scheduler_live_state["capacity_mode"],
            )
        if outbox_metrics is not None:
            self.update_outbox_state(
                pending_depth=outbox_metrics.get("pending", outbox_metrics.get("pending_depth", 0)),
                leased_depth=outbox_metrics.get("leased", outbox_metrics.get("leased_depth", 0)),
                oldest_pending_age_seconds=outbox_metrics.get(
                    "oldest_seconds", outbox_metrics.get("oldest_pending_age_seconds", 0.0)
                ),
            )
        if proxy_snapshots is not None:
            for ps in proxy_snapshots:
                pid = ps.get("proxy_id")
                if pid:
                    self.update_proxy_state(
                        pid,
                        masked_url=ps.get("masked_url") or "direct",
                        state=ps.get("state") or "healthy",
                        configured_rate=float(ps.get("configured_rate") or config.PROXY_RATE_LIMIT),
                        effective_rate=float(ps.get("effective_rate") or ps.get("current_rps") or config.PROXY_RATE_LIMIT),
                        quarantined_until=float(ps.get("quarantined_until") or 0.0),
                    )
        buckets = self._active_buckets(window_minutes)
        cost_summary = summarize_histograms([b.http_cost_per_scan for b in buckets], HTTP_COST_BOUNDS)
        scan_dur_summary = summarize_histograms([b.scan_duration for b in buckets], SCAN_INTERVAL_BOUNDS)
        interval_summary = summarize_histograms([b.source_interval for b in buckets], SCAN_INTERVAL_BOUNDS)
        fanout_summary = summarize_histograms([b.fanout_duration for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        bot_api_summary = summarize_histograms([b.bot_api_service_time for b in buckets], SUBSECOND_LATENCY_BOUNDS)

        deliver_all = summarize_histograms([b.deliver_at_to_sent_all for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        queued_all = summarize_histograms([b.queued_to_sent_all for b in buckets], SUBSECOND_LATENCY_BOUNDS)
        catchup_deliver = summarize_histograms(
            [b.catchup_deliver_at_to_sent for b in buckets], SUBSECOND_LATENCY_BOUNDS
        )
        catchup_queued = summarize_histograms(
            [b.catchup_queued_to_sent for b in buckets], SUBSECOND_LATENCY_BOUNDS
        )

        by_tariff: dict[str, dict[str, Any]] = {}
        for t_key in TARIFF_KEYS:
            by_tariff[t_key] = {
                "pub_to_seen_seconds": summarize_histograms([b.pub_to_seen[t_key] for b in buckets], PUB_DELAY_BOUNDS),
                "seen_to_queued_seconds": summarize_histograms(
                    [b.seen_to_queued[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
                "queued_to_sent_seconds": summarize_histograms(
                    [b.queued_to_sent[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
                "deliver_at_to_sent_seconds": summarize_histograms(
                    [b.deliver_at_to_sent[t_key] for b in buckets], SUBSECOND_LATENCY_BOUNDS
                ),
            }

        scans_completed = sum(b.scans_completed for b in buckets)
        scans_fast_skipped = sum(b.scans_fast_skipped for b in buckets)
        scans_unchanged = sum(b.scans_unchanged for b in buckets)
        scans_with_new = sum(b.scans_with_new_items for b in buckets)
        scans_failed = sum(b.scans_failed for b in buckets)

        proxy_summaries = self.get_proxy_summaries()
        capacity = calculate_capacity_report(
            unique_sources=self._scheduler_live_state["unique_sources"],
            proxy_endpoints=proxy_summaries,
            rolling_mean_cost=cost_summary["mean"],
            p95_cost=cost_summary["p95"],
            completed_scans_count=scans_completed,
        )
        capacity["available_rps"] = capacity["available_rate_now"]
        capacity["required_rps"] = capacity["required_rate_target"]
        capacity["deficit"] = capacity["deficit_now"] > 0
        self._last_capacity_report = capacity

        req_workers = calculate_required_bot_workers(bot_api_summary["p95"])

        return {
            "window_minutes": window_minutes,
            "scheduler": dict(self._scheduler_live_state),
            "outbox": dict(self._outbox_live_state),
            "capacity": capacity,
            "avg_http_requests_per_scan": capacity["rolling_mean_http_requests_per_scan"],
            "proxies": proxy_summaries,
            "by_tariff": by_tariff,
            "delivery_sla": {
                "deliver_at_to_sent_seconds": deliver_all,
                "queued_to_sent_seconds": queued_all,
                "catchup_deliver_at_to_sent_seconds": catchup_deliver,
                "catchup_queued_to_sent_seconds": catchup_queued,
                "by_tariff": by_tariff,
                "pub_timestamp_missing_count": sum(b.pub_timestamp_missing_count for b in buckets),
            },
            "scan_pipeline": {
                "scans_completed": scans_completed,
                "scans_fast_skipped": scans_fast_skipped,
                "scans_unchanged": scans_unchanged,
                "scans_with_new_items": scans_with_new,
                "scans_failed": scans_failed,
                "fast_skip_ratio": round(scans_fast_skipped / scans_completed, 4) if scans_completed > 0 else None,
                "http_cost_per_scan": cost_summary,
                "scan_duration_seconds": scan_dur_summary,
                "source_interval_seconds": interval_summary,
                "fanout_duration_seconds": fanout_summary,
            },
            "bot_api": {
                "sent_total": sum(b.bot_api_sent_total for b in buckets),
                "retry_after_count": sum(b.bot_api_retry_after_count for b in buckets),
                "service_time_seconds": bot_api_summary,
                "configured_workers": config.NOTIFICATION_WORKERS,
                "required_workers_estimate": req_workers,
                "photo_file_id_hit_count": sum(b.photo_file_id_hit_count for b in buckets),
                "photo_url_direct_count": sum(b.photo_url_direct_count for b in buckets),
                "photo_cdn_upload_count": sum(b.photo_cdn_upload_count for b in buckets),
                "photo_fallback_text_count": sum(b.photo_fallback_text_count for b in buckets),
                "text_only_sent_count": sum(b.text_only_sent_count for b in buckets),
                "catchup_sent_count": sum(b.catchup_sent_count for b in buckets),
                "catchup_omitted_count": sum(b.catchup_omitted_count for b in buckets),
            },
        }

    def evaluate_minute_alerts(
        self,
        *,
        active_unique_urls: int | None = None,
        proxy_snapshots: list[dict[str, Any]] | None = None,
        outbox_metrics: dict[str, Any] | None = None,
        full_coverage_age_seconds: float | None = None,
    ) -> list[str]:
        """
        Вызывается раз в минуту. Возвращает список сообщений алертов и восстановлений на русском языке.
        Учитывает:
        - p95(deliver_at -> t_sent) > 2 с в течение 5 минут подряд (только при sample_count > 0)
        - pending outbox > 200 или монотонный рост 3 минуты подряд
        - deficit_now > 0 или число здоровых прокси ниже резервного порога 3 минуты подряд
        """
        now = self._monotonic()
        current_bucket = self._bucket()
        snap = self.snapshot(
            window_minutes=60,
            active_unique_urls=active_unique_urls,
            proxy_snapshots=proxy_snapshots,
            outbox_metrics=outbox_metrics,
            full_coverage_age_seconds=full_coverage_age_seconds,
        )
        cap = snap["capacity"]
        outbox = snap["outbox"]

        messages: list[str] = []

        def _process_rule(
            key: str,
            is_bad: bool,
            threshold_minutes: int,
            has_data: bool,
            alert_text: str,
            recovery_text: str,
        ) -> None:
            st = self._alert_states[key]
            if not has_data:
                return
            if is_bad:
                st.consecutive_bad += 1
                if st.consecutive_bad >= threshold_minutes:
                    if not st.active or (now - st.last_sent_at) >= self._alert_cooldown_seconds:
                        st.active = True
                        st.last_sent_at = now
                        messages.append(alert_text)
            else:
                if st.active:
                    st.active = False
                    st.consecutive_bad = 0
                    st.last_sent_at = now
                    messages.append(recovery_text)
                else:
                    st.consecutive_bad = 0

        # 1. Delivery SLA (current 1-minute bucket)
        cur_deliv = summarize_histograms([current_bucket.deliver_at_to_sent_all], SUBSECOND_LATENCY_BOUNDS)
        has_deliv_data = cur_deliv["sample_count"] > 0
        deliv_bad = bool(has_deliv_data and cur_deliv["p95"] is not None and cur_deliv["p95"] > 2.0)
        _process_rule(
            "delivery_sla",
            deliv_bad,
            5,
            has_deliv_data,
            f"⚠️ Алерт SLA доставки: p95(deliver_at -> sent) = {cur_deliv['p95']} с (> 2.0 с) 5 минут подряд.",
            "✅ Восстановлено SLA доставки: p95(deliver_at -> sent) вернулся в норму (<= 2.0 с).",
        )

        # 2. Outbox depth
        pending_depth = int(outbox["pending_depth"])
        self._pending_depth_history.append(pending_depth)
        if len(self._pending_depth_history) > 4:
            self._pending_depth_history = self._pending_depth_history[-4:]
        growing_3m = (
            len(self._pending_depth_history) >= 4
            and self._pending_depth_history[-4] < self._pending_depth_history[-3] < self._pending_depth_history[-2] < self._pending_depth_history[-1]
            and self._pending_depth_history[-1] > 0
        )
        outbox_bad = pending_depth > config.OUTBOX_ALERT_DEPTH or growing_3m
        _process_rule(
            "outbox_depth",
            outbox_bad,
            1 if pending_depth > config.OUTBOX_ALERT_DEPTH else 1,
            True,
            f"⚠️ Алерт очереди доставки: pending={pending_depth} (порог {config.OUTBOX_ALERT_DEPTH}, рост={growing_3m}).",
            f"✅ Восстановлено: очередь доставки стабилизировалась (pending={pending_depth}).",
        )

        # 3. Capacity deficit / proxy health
        total_p = cap["total_proxies"]
        healthy_p = cap["healthy_proxies"]
        reserve_floor = math.floor(total_p * (1.0 - config.PROXY_CAPACITY_RESERVE)) if total_p >= 2 else total_p
        has_cap_data = cap["unique_sources"] > 0 or total_p > 0
        cap_bad = cap["deficit_now"] > 0 or (total_p > 0 and healthy_p < max(1, reserve_floor))
        _process_rule(
            "capacity_deficit",
            cap_bad,
            3,
            has_cap_data,
            (
                f"⚠️ Алерт емкости прокси: deficit_now={cap['deficit_now']:.2f} req/s, "
                f"здоровых прокси {healthy_p}/{total_p}. Рекомендация: {cap['recommendation_ru']}."
            ),
            f"✅ Восстановлено: емкость пула прокси достаточна (здоровых {healthy_p}/{total_p}).",
        )

        return messages

    def format_stats_messages(self, max_len: int = 3800) -> list[str]:
        snap = self.snapshot(window_minutes=60)
        sched = snap["scheduler"]
        outbox = snap["outbox"]
        cap = snap["capacity"]
        sla = snap["delivery_sla"]
        pipe = snap["scan_pipeline"]
        bapi = snap["bot_api"]

        def _fmt_hist(h: dict[str, Any], unit: str = "с") -> str:
            if not h or h.get("sample_count", 0) == 0:
                return "no data (n=0)"
            return f"p50={h['p50']}{unit}, p95={h['p95']}{unit} (n={h['sample_count']})"

        cov = (
            f"{sched['full_coverage_age_seconds']:.1f} с"
            if sched["full_coverage_age_seconds"] is not None
            else "no data"
        )
        fskip_ratio = (
            f"{pipe['fast_skip_ratio'] * 100:.1f}%"
            if pipe["fast_skip_ratio"] is not None
            else "no data"
        )

        header_lines = [
            "📊 <b>СТАТИСТИКА МОНИТОРИНГА AVITO RADAR (окно 60 мин)</b>",
            "",
            "🧭 <b>Планировщик и покрытие:</b>",
            f"• Уникальных URL: <code>{sched['unique_sources']}</code> | Просрочено: <code>{sched['overdue_sources']}</code> | Backoff: <code>{sched['backoff_sources']}</code>",
            f"• Режим емкости: <code>{cap['capacity_mode']}</code> (интервал <code>{cap['recommended_interval_low']:.1f}-{cap['recommended_interval_high']:.1f} с</code>)",
            f"• Возраст полного покрытия всех URL: <code>{cov}</code>",
            f"• Фактический интервал URL: <code>{_fmt_hist(pipe['source_interval_seconds'])}</code>",
            f"• Длительность скана: <code>{_fmt_hist(pipe['scan_duration_seconds'])}</code>",
            f"• Сканов: ok=<code>{pipe['scans_completed']}</code>, fast-skip=<code>{pipe['scans_fast_skipped']}</code> ({fskip_ratio}), новые=<code>{pipe['scans_with_new_items']}</code>, сбои=<code>{pipe['scans_failed']}</code>",
            f"• Fanout БД: <code>{_fmt_hist(pipe['fanout_duration_seconds'])}</code>",
            "",
            "🌐 <b>Емкость пула прокси:</b>",
            f"• Прокси: всего=<code>{cap['total_proxies']}</code>, здоровых=<code>{cap['healthy_proxies']}</code>, карантин=<code>{cap['quarantined_proxies']}</code>",
            f"• Цена 1 скана (HTTP): mean=<code>{cap['rolling_mean_http_requests_per_scan']:.2f}</code>, p95=<code>{cap['p95_http_requests_per_scan']:.2f}</code> ({'cold-start' if cap['cold_start_cost_used'] else 'measured'})",
            f"• Требуется: max(20с)=<code>{cap['required_rate_at_max']:.2f} req/s</code>, target(17.5с)=<code>{cap['required_rate_target']:.2f} req/s</code>",
            f"• Доступно: сейчас=<code>{cap['available_rate_now']:.2f} req/s</code>, с резервом 20%=<code>{cap['available_rate_with_reserve']:.2f} req/s</code>",
            f"• Дефицит: сейчас=<code>{cap['deficit_now']:.2f} req/s</code>, с резервом=<code>{cap['deficit_with_reserve']:.2f} req/s</code>",
            f"• Требуемый PROXY_RATE_LIMIT: hard=<code>{cap['required_configured_rate_hard']:.2f}</code>, target=<code>{cap['required_configured_rate_target']:.2f}</code>",
            f"• Следующий безопасный шаг: <code>{cap['next_safe_manual_step'] if cap['next_safe_manual_step'] is not None else 'недоступен'}</code>",
            f"• Рекомендация: {cap['recommendation_ru']}",
            "",
            "📬 <b>Очередь и SLA доставки:</b>",
            f"• Outbox: pending=<code>{outbox['pending_depth']}</code>, leased=<code>{outbox['leased_depth']}</code>, макс. возраст=<code>{outbox['oldest_pending_age_seconds']:.1f} с</code>",
            f"• SLA deliver_at → sent (все): <code>{_fmt_hist(sla['deliver_at_to_sent_seconds'])}</code>",
            f"• Полный queued → sent (все): <code>{_fmt_hist(sla['queued_to_sent_seconds'])}</code>",
            f"• Догон после паузы (catch-up): sent=<code>{bapi['catchup_sent_count']}</code>, пропущено=<code>{bapi['catchup_omitted_count']}</code>, deliver_at→sent: <code>{_fmt_hist(sla['catchup_deliver_at_to_sent_seconds'])}</code>",
            f"• Лот без точного sortTimeStamp: <code>{sla['pub_timestamp_missing_count']}</code>",
        ]

        for t_key in TARIFF_KEYS:
            ts = sla["by_tariff"][t_key]
            header_lines.append(
                f"• Тариф <b>{t_key.upper()}</b>: "
                f"deliver→sent <code>{_fmt_hist(ts['deliver_at_to_sent_seconds'])}</code> | "
                f"queued→sent <code>{_fmt_hist(ts['queued_to_sent_seconds'])}</code> | "
                f"seen→queued <code>{_fmt_hist(ts['seen_to_queued_seconds'])}</code> | "
                f"pub→seen <code>{_fmt_hist(ts['pub_to_seen_seconds'])}</code>"
            )

        header_lines.extend([
            "",
            "🤖 <b>Telegram Bot API:</b>",
            f"• Отправлено: <code>{bapi['sent_total']}</code> | RetryAfter: <code>{bapi['retry_after_count']}</code>",
            f"• Время вызова Bot API: <code>{_fmt_hist(bapi['service_time_seconds'])}</code>",
            f"• Воркеры доставки: активных=<code>{bapi['configured_workers']}</code>, расчетно требуется=<code>{bapi['required_workers_estimate']}</code>",
            f"• Фото: file_id=<code>{bapi['photo_file_id_hit_count']}</code>, direct_url=<code>{bapi['photo_url_direct_count']}</code>, upload=<code>{bapi['photo_cdn_upload_count']}</code>, fallback_text=<code>{bapi['photo_fallback_text_count']}</code>, text_only=<code>{bapi['text_only_sent_count']}</code>",
        ])

        main_msg = "\n".join(header_lines)
        messages = [main_msg]

        proxies = snap["proxies"]
        if proxies:
            proxy_lines = ["🔌 <b>Состояние прокси-клиентов (60 мин):</b>"]
            for p in proxies:
                raw_ratio = (
                    f"{p['raw_http_success_ratio_60m'] * 100:.1f}%"
                    if p["raw_http_success_ratio_60m"] is not None
                    else "no data"
                )
                coll_ratio = (
                    f"{p['collection_success_ratio_60m'] * 100:.1f}%"
                    if p["collection_success_ratio_60m"] is not None
                    else "no data"
                )
                p95_lat = f"{p['p95_latency_seconds']}с" if p["p95_latency_seconds"] is not None else "no data"
                line = (
                    f"• <code>{p['proxy_id']}</code> ({sanitize_proxy_credentials(p['masked_url'])}) "
                    f"[{p['state']}] rate=<code>{p['effective_rate']:.2f}/{p['configured_rate']:.2f}</code> "
                    f"raw_ok=<code>{raw_ratio}</code> (n={p['raw_http_total_60m']}) "
                    f"coll_ok=<code>{coll_ratio}</code> "
                    f"403/429=<code>{p['raw_http_403_60m']}/{p['raw_http_429_60m']}</code> "
                    f"blocks=<code>{p['final_blocks_60m']}</code> p95=<code>{p95_lat}</code>"
                )
                proxy_lines.append(line)

            current_chunk: list[str] = []
            current_len = 0
            for line in proxy_lines:
                if current_chunk and current_len + len(line) + 1 > max_len:
                    messages.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_len = len(line)
                else:
                    current_chunk.append(line)
                    current_len += len(line) + 1
            if current_chunk:
                if len(messages[-1]) + 2 + current_len <= max_len:
                    messages[-1] = messages[-1] + "\n\n" + "\n".join(current_chunk)
                else:
                    messages.append("\n".join(current_chunk))

        return messages

    def format_stats_report(self, max_len: int = 3800) -> str:
        return "\n\n".join(self.format_stats_messages(max_len=max_len))


METRICS = RuntimeMetricsRegistry()

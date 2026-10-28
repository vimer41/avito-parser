"""Пул статических прокси с независимыми лимитерами, карантином, half-open и горячей перезагрузкой."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests

import logging
import config
from runtime_metrics import METRICS, mask_proxy_url, sanitize_proxy_credentials

log = logging.getLogger("proxy_pool")

ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def compute_proxy_id(normalized_url: str | None) -> str:
    if not normalized_url:
        return "direct"
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:8]
    return f"p-{digest}"


def compute_cookie_path(normalized_url: str | None, data_dir: Path | None = None) -> Path:
    base_dir = config.DATA_DIR if data_dir is None else data_dir
    key = (normalized_url or "direct").strip().encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:16]
    return base_dir / f"avito_cookies_{digest}.json"


def parse_and_validate_proxy_line(raw_line: str, line_number: int = 1) -> str:
    """
    Парсит и валидирует одну строку прокси.
    Требует percent-encoding для спецсимволов (@, :) в логине/пароле.
    Никогда не включает сырые учетные данные в текст исключения.
    """
    stripped = (raw_line or "").strip()
    if not stripped:
        raise ValueError(f"Строка {line_number}: пустой адрес прокси")

    # Проверяем количество '@' до нормализации: если больше одного '@', значит спецсимвол не закодирован
    if stripped.count("@") > 1:
        raise ValueError(
            f"Строка {line_number}: некорректный формат учетных данных прокси "
            "(требуется percent-encoding спецсимволов '@' и ':')"
        )

    normalized = config.parse_proxy(stripped)
    if not normalized:
        raise ValueError(f"Строка {line_number}: не удалось распознать прокси")

    if normalized.count("@") > 1:
        raise ValueError(
            f"Строка {line_number}: некорректный формат учетных данных прокси "
            "(требуется percent-encoding спецсимволов '@' и ':')"
        )

    try:
        parsed = urlsplit(normalized)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ALLOWED_PROXY_SCHEMES:
            raise ValueError(
                f"Строка {line_number}: неподдерживаемая схема прокси '{scheme}' "
                "(разрешены http, https, socks5, socks5h)"
            )
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        if "Строка " in str(exc):
            raise
        raise ValueError(
            f"Строка {line_number}: синтаксическая ошибка URL прокси "
            "(проверьте порт и percent-encoding логина/пароля)"
        ) from exc

    if not host or port is None or not (1 <= int(port) <= 65535):
        raise ValueError(f"Строка {line_number}: должен быть указан корректный хост и порт (1..65535)")

    if "@" in parsed.netloc:
        userinfo, _ = parsed.netloc.rsplit("@", 1)
        if userinfo.count(":") != 1:
            raise ValueError(
                f"Строка {line_number}: некорректный формат логина/пароля прокси "
                "(требуется percent-encoding двоеточий)"
            )
        user, password = userinfo.split(":", 1)
        if not user or not password:
            raise ValueError(f"Строка {line_number}: пустой логин или пароль прокси")

    return f"{scheme}://{parsed.netloc}"


def load_proxy_urls_from_file(path: str | Path) -> list[str]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise ValueError(f"Файл пула прокси не найден: {file_path}")

    try:
        st = file_path.stat()
        if os.name != "nt" and (st.st_mode & 0o077):
            log.warning(
                f"⚠️ Файл прокси {file_path} имеет слишком широкие права доступа ({oct(st.st_mode & 0o777)}); "
                "рекомендуется chmod 600."
            )
    except OSError:
        pass

    text = file_path.read_text(encoding="utf-8")
    seen: set[str] = set()
    urls: list[str] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        normalized = parse_and_validate_proxy_line(line, line_number=idx)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    if not urls:
        raise ValueError(f"Файл пула прокси {file_path} не содержит ни одного активного прокси")
    return urls


class ProxyClient:
    """Отдельный прокси-клиент со своим AsyncSession(max_clients=1), rate-лимитером и состоянием здоровья."""

    def __init__(
        self,
        proxy_url: str | None,
        *,
        configured_rate: float | None = None,
        max_in_flight: int | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        sync_sleep_fn: Callable[[float], None] = time.sleep,
        impersonate: str = "chrome120",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.proxy_url: str | None = config.parse_proxy(proxy_url) if proxy_url else None
        self.proxy_key: str = hashlib.sha256((self.proxy_url or "direct").strip().encode("utf-8")).hexdigest()
        self.proxy_id: str = compute_proxy_id(self.proxy_url)
        self.masked_url: str = mask_proxy_url(self.proxy_url)
        self.cookie_file: Path = compute_cookie_path(self.proxy_url)

        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._sync_sleep = sync_sleep_fn
        self.impersonate = impersonate
        self.default_headers: dict[str, str] = dict(headers or {})

        self.configured_rate: float = float(
            config.PROXY_RATE_LIMIT if configured_rate is None else configured_rate
        )
        self.effective_rate: float = self.configured_rate
        self.max_in_flight: int = max(
            1, int(config.PROXY_MAX_IN_FLIGHT if max_in_flight is None else max_in_flight)
        )

        self._state: str = "healthy"  # healthy | quarantined | half_open | draining
        self.quarantined_until: float = 0.0
        self.quarantine_reason: str = ""
        self.block_streak: int = 0
        self.transport_failure_streak: int = 0
        self.recovery_success_streak: int = 0
        self._half_open_probe_active: bool = False

        # Pacing & concurrency state (protected by threading.Lock so both event loop and challenge thread are safe)
        self._pacing_lock = threading.Lock()
        self._next_request_at: float = 0.0
        self.in_flight: int = 0

        # Sliding 10m window of (monotonic_ts, status_code, is_transport_error)
        self._history_10m: deque[tuple[float, int | None, bool]] = deque()

        # Persistent AsyncSession (created lazily)
        self._async_session: curl_requests.AsyncSession | None = None
        self._session_generation: int = 0
        self._draining: bool = False

        self._sync_metrics_state()

    @property
    def state(self) -> str:
        if self._draining:
            return "draining"
        now = self._monotonic()
        if self._state == "quarantined" and now >= self.quarantined_until:
            self._state = "half_open"
            self.quarantine_reason = ""
            self._sync_metrics_state()
        return self._state

    def _sync_metrics_state(self) -> None:
        METRICS.update_proxy_state(
            self.proxy_id,
            masked_url=self.masked_url,
            state="draining" if self._draining else self._state,
            configured_rate=self.configured_rate,
            effective_rate=self.effective_rate,
            quarantined_until=self.quarantined_until,
        )

    def is_available_for_scan(self, now: float | None = None) -> bool:
        if self._draining:
            return False
        cur = self._monotonic() if now is None else now
        if self._state == "quarantined":
            if cur < self.quarantined_until:
                return False
            self._state = "half_open"
            self.quarantine_reason = ""
            self._sync_metrics_state()
        if self._state == "half_open" and self._half_open_probe_active:
            return False
        return True

    def is_available(self, now: float | None = None) -> bool:
        return self.is_available_for_scan(now)

    def cooldown_remaining(self, now: float | None = None) -> float:
        cur = self._monotonic() if now is None else now
        if self.state == "quarantined":
            return max(0.0, self.quarantined_until - cur)
        return 0.0

    def estimated_ready_at(self, now: float | None = None) -> float:
        cur = self._monotonic() if now is None else now
        if not self.is_available_for_scan(cur):
            return max(cur + 9999.0, self.quarantined_until)
        with self._pacing_lock:
            base = max(cur, self._next_request_at)
            if self.in_flight >= self.max_in_flight:
                base += 1.0 / max(config.PROXY_MIN_RATE, self.effective_rate)
            return base

    async def get_or_create_async_session(
        self,
        factory_or_headers: Callable[[], curl_requests.AsyncSession] | dict[str, str] | None = None,
    ) -> curl_requests.AsyncSession:
        if self._async_session is None:
            if callable(factory_or_headers):
                self._async_session = factory_or_headers()
            else:
                merged_headers = dict(self.default_headers)
                if isinstance(factory_or_headers, dict):
                    merged_headers.update(factory_or_headers)
                kwargs: dict[str, Any] = {
                    "impersonate": self.impersonate,
                    "headers": merged_headers,
                    "max_clients": 1,
                }
                if self.proxy_url:
                    kwargs["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}
                self._async_session = curl_requests.AsyncSession(**kwargs)
            self._session_generation += 1
        return self._async_session

    async def reset_async_session(self) -> None:
        session, self._async_session = self._async_session, None
        if session is not None:
            try:
                close_fn = getattr(session, "close", None)
                if close_fn is not None:
                    res = close_fn()
                    if asyncio.isfuture(res) or asyncio.iscoroutine(res):
                        await res
            except Exception:
                pass

    async def close(self) -> None:
        self._draining = True
        self._sync_metrics_state()
        await self.reset_async_session()

    def _reserve_pacing_delay(self) -> float:
        now = self._monotonic()
        spacing = 1.0 / max(config.PROXY_MIN_RATE, self.effective_rate)
        with self._pacing_lock:
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + spacing
            return max(0.0, scheduled - now)

    async def wait_for_rate_slot_async(self, deadline: float | None = None) -> None:
        delay = self._reserve_pacing_delay()
        if deadline is not None and self._monotonic() + delay > deadline:
            raise TimeoutError("HTTP operation deadline exceeded")
        if delay > 0:
            await self._sleep(delay)

    async def wait_for_rate_slot(self, deadline: float | None = None) -> None:
        await self.wait_for_rate_slot_async(deadline=deadline)

    def wait_for_rate_slot_sync(self, deadline: float | None = None, cancel_event: threading.Event | None = None) -> None:
        delay = self._reserve_pacing_delay()
        if deadline is not None and self._monotonic() + delay > deadline:
            raise TimeoutError("HTTP operation deadline exceeded")
        if delay > 0:
            if cancel_event is not None:
                if cancel_event.wait(timeout=delay):
                    raise TimeoutError("HTTP operation cancelled")
            else:
                self._sync_sleep(delay)

    def _prune_10m_history(self, now: float) -> None:
        cutoff = now - float(config.PROXY_BLOCK_WINDOW_SECONDS)
        while self._history_10m and self._history_10m[0][0] < cutoff:
            self._history_10m.popleft()

    def _count_recent_403_429(self, now: float) -> tuple[int, int]:
        self._prune_10m_history(now)
        total = len(self._history_10m)
        bad = sum(1 for _, code, _ in self._history_10m if code in (403, 429))
        return total, bad

    def record_raw_http_response(
        self,
        status_code: int | None,
        latency_seconds: float,
        *,
        is_transport_error: bool = False,
    ) -> None:
        now = self._monotonic()
        self._prune_10m_history(now)
        self._history_10m.append((now, status_code, is_transport_error))

        METRICS.record_http_request(
            self.proxy_id,
            self.masked_url,
            status_code,
            latency_seconds,
            is_transport_error=is_transport_error,
        )

        if is_transport_error or status_code is None or status_code >= 500:
            self.recovery_success_streak = 0
            self.transport_failure_streak += 1
            if self.transport_failure_streak >= config.PROXY_FAILURE_THRESHOLD:
                exp = self.transport_failure_streak - config.PROXY_FAILURE_THRESHOLD
                backoff = min(
                    float(config.PROXY_TRANSPORT_BACKOFF_MAX),
                    float(config.PROXY_TRANSPORT_BACKOFF_BASE) * (2 ** min(exp, 6)),
                )
                self._enter_quarantine(now, backoff, reason=f"transport_or_5xx_streak={self.transport_failure_streak}")
            return

        if 200 <= status_code < 400:
            self.transport_failure_streak = 0
            self.recovery_success_streak += 1
            total_10m, bad_10m = self._count_recent_403_429(now)
            if (
                self.recovery_success_streak >= config.PROXY_RECOVERY_SUCCESS_STREAK
                and bad_10m == 0
                and self.effective_rate < self.configured_rate
            ):
                self.effective_rate = min(
                    self.configured_rate,
                    round(self.effective_rate * config.PROXY_RECOVERY_FACTOR, 4),
                )
                self.recovery_success_streak = 0
                log.info(
                    f"📈 Прокси {self.proxy_id} ({self.masked_url}) восстановил эффективную скорость до "
                    f"{self.effective_rate:.2f}/{self.configured_rate:.2f} req/s"
                )
            self._sync_metrics_state()
            return

        if status_code in (403, 429):
            self.recovery_success_streak = 0
            total_10m, bad_10m = self._count_recent_403_429(now)
            if (
                total_10m >= config.PROXY_BLOCK_RATIO_MIN_REQUESTS
                and (bad_10m / total_10m) > config.PROXY_BLOCK_RATIO_THRESHOLD
            ):
                self.effective_rate = max(
                    config.PROXY_MIN_RATE,
                    round(self.effective_rate * 0.5, 4),
                )
                self._enter_quarantine(
                    now,
                    float(config.PROXY_QUARANTINE_BASE),
                    reason=f"block_ratio_10m={bad_10m}/{total_10m}",
                )

    def record_final_block(
        self,
        reason: str = "block",
        *,
        retry_after_seconds: float | None = None,
    ) -> float:
        now = self._monotonic()
        self.block_streak += 1
        self.recovery_success_streak = 0
        self.effective_rate = max(
            config.PROXY_MIN_RATE,
            round(self.effective_rate * 0.5, 4),
        )
        duration = min(
            float(config.PROXY_QUARANTINE_MAX),
            float(config.PROXY_QUARANTINE_BASE) * (2 ** min(self.block_streak - 1, 8)),
        )
        if retry_after_seconds is not None and math.isfinite(retry_after_seconds) and retry_after_seconds > 0:
            duration = min(float(config.PROXY_QUARANTINE_MAX), max(duration, float(retry_after_seconds)))
        self._enter_quarantine(now, duration, reason=reason)
        return duration

    def record_block_or_challenge_failure(
        self,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        reason: str = "block",
    ) -> float:
        return self.record_final_block(reason=reason, retry_after_seconds=retry_after)

    def record_transport_error(self, *, reason: str = "transport") -> float:
        self.record_raw_http_response(None, 0.0, is_transport_error=True)
        return self.cooldown_remaining()

    def record_success(self) -> None:
        if self._state == "half_open":
            self._state = "healthy"
            self.quarantine_reason = ""
        if self.block_streak > 0:
            self.block_streak = max(0, self.block_streak - 1)
        self._sync_metrics_state()

    def record_collection_outcome(
        self,
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
        if success:
            if self._state == "half_open":
                self._state = "healthy"
                self.quarantine_reason = ""
            if self.block_streak > 0:
                self.block_streak = max(0, self.block_streak - 1)
            self._sync_metrics_state()

        METRICS.record_collection(
            self.proxy_id,
            self.masked_url,
            success=success,
            http_requests_count=http_requests_count,
            duration_seconds=duration_seconds,
            challenge_solved=challenge_solved,
            final_block=final_block,
            fast_skipped=fast_skipped,
            unchanged=unchanged,
            has_new_items=has_new_items,
        )

    def _enter_quarantine(self, now: float, duration_seconds: float, *, reason: str) -> None:
        until = now + max(1.0, float(duration_seconds))
        if until > self.quarantined_until:
            self.quarantined_until = until
        self._state = "quarantined"
        self.quarantine_reason = reason
        self._sync_metrics_state()
        log.warning(
            f"🛑 Прокси {self.proxy_id} ({self.masked_url}) переведен в карантин на {duration_seconds:.1f} с "
            f"(причина: {sanitize_proxy_credentials(reason)}, rate={self.effective_rate:.2f} req/s)"
        )

    def snapshot(self) -> dict[str, Any]:
        st = self.state
        now = self._monotonic()
        return {
            "proxy_id": self.proxy_id,
            "masked_url": self.masked_url,
            "state": st,
            "configured_rate": self.configured_rate,
            "effective_rate": self.effective_rate,
            "current_rps": self.effective_rate,
            "in_flight": self.in_flight,
            "quarantined_until": self.quarantined_until,
            "quarantine_remaining": max(0.0, round(self.quarantined_until - now, 1)) if st == "quarantined" else 0.0,
            "quarantine_reason": self.quarantine_reason,
            "cookie_file": str(self.cookie_file),
        }


@dataclass
class ProxyLease:
    client: ProxyClient
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with self.client._pacing_lock:
            self.client.in_flight = max(0, self.client.in_flight - 1)
            if self.client._half_open_probe_active:
                self.client._half_open_probe_active = False


class ProxyPool:
    """Статический пул прокси-клиентов с поддержкой /reload_proxies без остановки процесса."""

    def __init__(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
        configured_rate: float | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        sync_sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._sync_sleep = sync_sleep_fn
        self._configured_rate = configured_rate
        self._clients: dict[str, ProxyClient] = {}
        self._draining_clients: list[ProxyClient] = []
        self._rr_cursor: int = 0
        self._lock = threading.Lock()

        self.load_configuration(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )

    def _resolve_urls(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> list[str | None]:
        pool_file = config.PROXY_POOL_FILE if proxy_pool_file is None else str(proxy_pool_file or "").strip()
        single_proxy = config.PROXY_URL if fallback_proxy_url is None else fallback_proxy_url
        direct_ok = config.ALLOW_DIRECT_AVITO if allow_direct is None else bool(allow_direct)

        if pool_file:
            p_path = Path(pool_file).expanduser()
            if proxy_pool_file is not None or p_path.is_file():
                return list(load_proxy_urls_from_file(pool_file))

        if single_proxy and str(single_proxy).strip():
            validated = parse_and_validate_proxy_line(str(single_proxy).strip(), line_number=1)
            return [validated]

        if direct_ok:
            return [None]

        log.warning(
            "⚠️ Не настроен ни один прокси (PROXY_POOL_FILE или PROXY_URL), "
            "а прямое подключение без прокси выключено (ALLOW_DIRECT_AVITO=0). "
            "Сканирование Авито ожидает включения прямого режима в боте или добавления прокси."
        )
        return []

    def load_configuration(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> dict[str, Any]:
        target_urls = self._resolve_urls(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )

        with self._lock:
            new_map: dict[str, ProxyClient] = {}
            added = 0
            kept = 0
            for url in target_urls:
                pid = compute_proxy_id(url)
                if pid in self._clients:
                    client = self._clients[pid]
                    if self._configured_rate is not None:
                        client.configured_rate = float(self._configured_rate)
                    new_map[pid] = client
                    kept += 1
                else:
                    client = ProxyClient(
                        url,
                        configured_rate=self._configured_rate,
                        monotonic_fn=self._monotonic,
                        sleep_fn=self._sleep,
                        sync_sleep_fn=self._sync_sleep,
                    )
                    new_map[pid] = client
                    added += 1

            removed_clients: list[ProxyClient] = []
            for pid, old_client in self._clients.items():
                if pid not in new_map:
                    old_client._draining = True
                    old_client._sync_metrics_state()
                    removed_clients.append(old_client)

            self._clients = new_map
            self._draining_clients.extend(removed_clients)

        return {
            "total": len(new_map),
            "added": added,
            "kept": kept,
            "removed": len(removed_clients),
        }

    async def reload_async(
        self,
        *,
        proxy_pool_file: str | Path | None = None,
        fallback_proxy_url: str | None = None,
        allow_direct: bool | None = None,
    ) -> dict[str, Any]:
        result = self.load_configuration(
            proxy_pool_file=proxy_pool_file,
            fallback_proxy_url=fallback_proxy_url,
            allow_direct=allow_direct,
        )
        await self._cleanup_draining()
        return result

    async def set_direct_mode_async(self, enabled: bool) -> dict[str, Any]:
        config.ALLOW_DIRECT_AVITO = bool(enabled)
        return await self.reload_async(allow_direct=bool(enabled))

    async def _cleanup_draining(self) -> None:
        still_draining: list[ProxyClient] = []
        for client in list(self._draining_clients):
            if client.in_flight <= 0:
                await client.close()
            else:
                still_draining.append(client)
        self._draining_clients = still_draining

    @property
    def clients(self) -> list[ProxyClient]:
        with self._lock:
            return list(self._clients.values())

    def healthy_count(self) -> int:
        now = self._monotonic()
        return sum(1 for c in self.clients if c.is_available_for_scan(now))

    def acquire_client(self, *, exclude_ids: set[str] | None = None) -> ProxyLease | None:
        """
        Выбирает здоровый или half-open прокси-клиент с минимальным временем готовности,
        учитывая лимит max_in_flight и исключая уже опробованные в этом скане прокси.
        """
        now = self._monotonic()
        excluded = exclude_ids or set()
        with self._lock:
            all_clients = list(self._clients.values())
            if not all_clients:
                return None

            n = len(all_clients)
            candidates: list[tuple[int, float, int, ProxyClient]] = []
            for offset in range(n):
                idx = (self._rr_cursor + offset) % n
                client = all_clients[idx]
                if client.proxy_id in excluded:
                    continue
                if not client.is_available_for_scan(now):
                    continue
                with client._pacing_lock:
                    busy_rank = 1 if client.in_flight >= client.max_in_flight else 0
                    ready_at = max(now, client._next_request_at)
                candidates.append((busy_rank, ready_at, offset, client))

            if not candidates:
                return None

            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            _, _, chosen_offset, chosen = candidates[0]
            self._rr_cursor = (self._rr_cursor + chosen_offset + 1) % n

            with chosen._pacing_lock:
                chosen.in_flight += 1
                if chosen._state == "half_open":
                    chosen._half_open_probe_active = True

            return ProxyLease(client=chosen)

    def earliest_available_delay(self, *, exclude_ids: set[str] | None = None) -> float:
        now = self._monotonic()
        excluded = exclude_ids or set()
        clients = [c for c in self.clients if c.proxy_id not in excluded and not c._draining]
        if not clients:
            return 5.0
        min_ready = min(
            (c.quarantined_until if c.state == "quarantined" else c.estimated_ready_at(now))
            for c in clients
        )
        return max(0.05, min_ready - now)

    def snapshots(self) -> list[dict[str, Any]]:
        return [c.snapshot() for c in self.clients]

    async def close(self) -> None:
        for client in self.clients:
            await client.close()
        for client in list(self._draining_clients):
            await client.close()
        self._draining_clients.clear()


_GLOBAL_PROXY_POOL: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    global _GLOBAL_PROXY_POOL
    if _GLOBAL_PROXY_POOL is None:
        _GLOBAL_PROXY_POOL = ProxyPool()
    return _GLOBAL_PROXY_POOL


async def close_proxy_pool() -> None:
    global _GLOBAL_PROXY_POOL
    pool, _GLOBAL_PROXY_POOL = _GLOBAL_PROXY_POOL, None
    if pool is not None:
        await pool.close()

import asyncio
import base64
import copy
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie
from threading import Event, Lock, RLock
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests
from config import DATA_DIR
from anti_bot import (
    get_browser_user_agent,
    handle_anti_bot_challenge,
    is_qrator_challenge_response,
    extract_pow_challenge,
    solve_pow_challenge,
    is_geetest_challenge,
)
from anti_bot.manager import BROWSER_PROFILE
from anti_bot.pow_budget import PowSearchCancelled, PowSearchError, check_cancelled

logger = logging.getLogger(__name__)

COOKIE_FILE = DATA_DIR / "avito_cookies.json"

# Пауза при блокировке сессии Авито: 15 сек → 30 сек → 60 сек
BLOCK_BASE_COOLDOWN = 15
BLOCK_MAX_COOLDOWN = 5 * 60
COOKIE_IO_LOCK = Lock()


class NetworkBlockState:
    """A shared cooldown for sessions with the same external connection."""
    def __init__(self):
        self.blocked_until = 0.0
        self.block_streak = 0
        self.lock = Lock()

    def register_block(self, retry_after: float = 0) -> tuple[float, int]:
        with self.lock:
            self.block_streak += 1
            cooldown = max(min(BLOCK_BASE_COOLDOWN * 2 ** min(self.block_streak - 1, 5), BLOCK_MAX_COOLDOWN), retry_after)
            self.blocked_until = max(self.blocked_until, time.time() + cooldown)
            return cooldown, self.block_streak

    def register_success(self):
        with self.lock:
            # An in-flight success must not lift another worker's active rate limit.
            if self.blocked_until <= time.time():
                self.block_streak = 0


NETWORK_BLOCK_STATES: dict[str, NetworkBlockState] = {}
NETWORK_STATES_LOCK = Lock()


def get_network_block_state(proxy: Optional[str]) -> NetworkBlockState:
    key = hashlib.sha256((proxy or "direct").encode()).hexdigest()
    with NETWORK_STATES_LOCK:
        return NETWORK_BLOCK_STATES.setdefault(key, NetworkBlockState())


def parse_retry_after(headers) -> float:
    value = next((str(v).strip() for k, v in (headers or {}).items() if k.lower() == "retry-after"), "")
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(86400, max(0, delay)) if math.isfinite(delay) else 0

@dataclass
class AvitoItem:
    item_id: str
    title: str
    price: str
    url: str
    image_url: Optional[str] = None
    address: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    seller: Optional[str] = None
    seller_name: Optional[str] = None
    delivery: Optional[str] = None
    condition: Optional[str] = None
    timestamp: Optional[int] = None
    is_reserved: bool = False
    is_promotion: bool = False
    views_total: Optional[int] = None
    views_today: Optional[int] = None
    raw_price: Optional[int] = None
    was_lowered: bool = False
    string_without_discount: Optional[str] = None
    discount_percent: Optional[int] = None
    badges: Optional[List[str]] = None


class AvitoParser:
    def __init__(self, proxy: Optional[str] = None, *, block_state: Optional[NetworkBlockState] = None):
        self.current_proxy: Optional[str] = proxy
        self.network_block = block_state if block_state is not None else get_network_block_state(proxy)
        self._session_lock = RLock()
        self._async_fetch_lock = asyncio.Lock()
        self._proxy_key = hashlib.sha256((proxy or "direct").encode()).hexdigest()
        self.cookie_file = COOKIE_FILE if not proxy else DATA_DIR / f"avito_cookies_{self._proxy_key[:16]}.json"
        self.session = self._create_session()
        self._last_saved_cookies: list = []
        self._load_cookies()

        # Защита от бана: пока активна пауза, к Авито не отправляется ни одного запроса
        self.blocked_until: float = 0.0
        self.block_streak: int = 0

        # Кэш SSR-редиректов Авито: исходный URL -> канонический URL (экономит 1 запрос на радар)
        self._canonical_cache: dict = {}

    # ─── Состояние блокировки ────────────────────────────────────────────
    def is_blocked(self) -> bool:
        return time.time() < max(self.blocked_until, self.network_block.blocked_until)

    def block_remaining(self) -> int:
        return max(0, math.ceil(max(self.blocked_until, self.network_block.blocked_until) - time.time()))

    def _register_block(self, status: int, headers=None):
        cooldown, self.block_streak = self.network_block.register_block(parse_retry_after(headers))
        self.blocked_until = time.time() + cooldown
        logger.warning(
            f"🛑 Авито ограничил доступ (статус {status}). "
            f"Общая пауза {cooldown:g} сек., сессия сохранена (блок #{self.block_streak} подряд)"
        )

    @staticmethod
    def _is_firewall(r) -> bool:
        if r is None:
            return False
        text = getattr(r, "text", "") or ""
        return (
            r.status_code in (403, 429, 439)
            or is_qrator_challenge_response(r)
            or is_geetest_challenge(r)
            or "firewall-container" in text
        )

    def _create_session(self) -> requests.Session:
        ua = get_browser_user_agent()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": ua,
        }
        proxies = {"http": self.current_proxy, "https": self.current_proxy} if self.current_proxy else None
        # All uses are serialized by _session_lock, so one Curl handle preserves
        # the connection and can be closed even when to_thread uses another worker.
        return requests.Session(impersonate=BROWSER_PROFILE, headers=headers, timeout=20, proxies=proxies, use_thread_local_curl=False)

    def close(self):
        with self._session_lock:
            self.session.close()

    def _save_cookies(self):
        """Atomically save scopes, expiry and flags, without merging proxy identities."""
        temporary_path = None
        try:
            cookies = []
            for cookie in self.session.cookies.jar:
                domain = cookie.domain.lstrip(".").lower()
                if cookie.name in ("pow_challenge", "pow_solved") or cookie.is_expired() or not (domain == "avito.ru" or domain.endswith(".avito.ru")):
                    continue
                cookies.append({
                    "name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                    "domain_specified": cookie.domain_specified, "path": cookie.path,
                    "secure": cookie.secure, "expires": cookie.expires, "discard": cookie.discard,
                    "rest": copy.deepcopy(cookie._rest),
                })
            cookies.sort(key=lambda c: (c["domain"], c["path"], c["name"]))
            if cookies == self._last_saved_cookies:
                return
            payload = {"version": 2, "profile": BROWSER_PROFILE, "proxy_key": self._proxy_key, "cookies": cookies}
            with COOKIE_IO_LOCK:
                self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cookie_file.parent, delete=False) as f:
                    temporary_path = f.name
                    json.dump(payload, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary_path, self.cookie_file)
                temporary_path = None
            self._last_saved_cookies = cookies
            logger.debug("Cookies saved (%d scoped records)", len(cookies))
        except Exception as e:
            logger.debug(f"Не удалось сохранить куки: {e}")
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _load_cookies(self):
        """Загружает сохраненные куки с диска в текущую сессию."""
        if not self.cookie_file.exists():
            return
        try:
            with COOKIE_IO_LOCK, open(self.cookie_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return
            if payload.get("version") == 2:
                if payload.get("profile") != BROWSER_PROFILE or payload.get("proxy_key") != self._proxy_key:
                    return
                for entry in payload.get("cookies", []):
                    domain = entry.get("domain", "").lstrip(".").lower()
                    expires = entry.get("expires")
                    if not (domain == "avito.ru" or domain.endswith(".avito.ru")) or entry.get("name") in ("pow_challenge", "pow_solved"):
                        continue
                    if expires is not None and expires <= time.time():
                        continue
                    self.session.cookies.jar.set_cookie(Cookie(
                        version=0, name=entry["name"], value=entry["value"], port=None, port_specified=False,
                        domain=entry["domain"], domain_specified=entry.get("domain_specified", True),
                        domain_initial_dot=entry["domain"].startswith("."), path=entry["path"], path_specified=True,
                        secure=bool(entry.get("secure")), expires=expires, discard=bool(entry.get("discard")),
                        comment=None, comment_url=None, rest=entry.get("rest", {}), rfc2109=False,
                    ))
            elif not self.current_proxy and "version" not in payload:
                # Legacy snapshots lost scope; import once, without duplicate domains.
                for name, value in payload.items():
                    if name not in ("pow_challenge", "pow_solved") and isinstance(value, str):
                        self.session.cookies.set(name, value, domain=".avito.ru", path="/")
        except Exception as e:
            logger.warning(f"Ошибка загрузки кук из файла: {e}")




    @staticmethod
    def _human_url(url: str) -> str:
        """Превращает длинный технический URL Авито в читаемое название для логов."""
        try:
            from urllib.parse import unquote, urlparse
            p = urlparse(url)
            path = unquote(p.path).strip("/")
            parts = [x for x in path.split("/") if x]
            name = parts[-1].split("-")[0] if parts else "каталог"
            qs = unquote(p.query)
            m = re.search(r"q=([^&]+)", qs)
            if m:
                query = m.group(1).replace("+", " ").strip()
                return f"«{query}»"
            return f"«{name}»"
        except Exception:
            return url[:45]

    @staticmethod
    def _validate_avito_url(url: str):
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or not (hostname == "avito.ru" or hostname.endswith(".avito.ru")):
            raise ValueError("Ссылка должна быть с сайта avito.ru")
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 80, 443):
            raise ValueError("Ссылка Авито не должна содержать логин, пароль или нестандартный порт")
        return parsed

    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """
        Нормализует ссылку:
        - добавляет https:// если нет
        - проверяет, что это avito.ru
        - устанавливает s=104 (сортировка 'Сначала новые')
        - удаляет динамические параметры сессий и трекинга (context, localPriority, utm_*, etc.)
        - сортирует query params для дедупликации одинаковых поисков
        """
        raw_url = raw_url.strip()
        if not raw_url.lower().startswith("http://") and not raw_url.lower().startswith("https://"):
            raw_url = "https://" + raw_url

        parsed = AvitoParser._validate_avito_url(raw_url)

        # Парсим параметры запроса
        query_params = parse_qs(parsed.query)

        # Удаляем мусорные / сессионные параметры, меняющиеся между запросами
        junk_keys = {"context", "localPriority", "presentationType", "from", "searchRadius", "view"}
        for k in list(query_params.keys()):
            if k in junk_keys or k.startswith("utm_"):
                query_params.pop(k, None)

        # Устанавливаем сортировку по дате (сначала новые)
        query_params["s"] = ["104"]

        # Если bt=0 (по умолчанию 'везде'), убираем для каноничности и дедупликации
        if query_params.get("bt") == ["0"]:
            query_params.pop("bt", None)

        # Сортируем параметры для стабильного хеша URL при группировке
        sorted_params = sorted(query_params.items())
        new_query = urlencode(sorted_params, doseq=True)

        normalized = urlunparse((
            "https",
            parsed.hostname.lower().rstrip("."),
            parsed.path,
            parsed.params,
            new_query,
            ""  # fragment убираем
        ))
        return normalized

    def _solve_pow_challenge(self, pow_challenge: str) -> bool:
        """Решает PoW challenge через модуль anti_bot."""
        with self._session_lock:
            return solve_pow_challenge(self.session, pow_challenge)

    async def fetch_page_async(self, url: str) -> Optional[str]:
        """Signal cancellation to the worker and wait for it before reusing its session."""
        async with self._async_fetch_lock:
            cancel_event = Event()
            task = asyncio.create_task(asyncio.to_thread(self.fetch_page, url, cancel_event=cancel_event))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel_event.set()
                # Even repeated shutdown cancellations must not orphan the CPU/network worker.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    try:
                        task.result()
                    except Exception:
                        pass
                raise

    def _get_page_response(self, url: str, cancel_event: Event | None):
        """Follow a finite number of redirects, validating each target before sending cookies."""
        for _ in range(5):
            check_cancelled(cancel_event)
            self._validate_avito_url(url)
            response = self.session.get(url, timeout=20, allow_redirects=False)
            check_cancelled(cancel_event)
            if self._is_firewall(response) or extract_pow_challenge(response, self.session):
                return response, url
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, url
            location = response.headers.get("Location") or response.headers.get("location")
            if not location:
                return response, url
            url = urljoin(url, location)
        raise ValueError("Avito redirect limit exceeded")

    def fetch_page(self, url: str, *, cancel_event: Event | None = None) -> Optional[str]:
        """Only an unchallenged target-page response is success, never a solver token."""
        with self._session_lock:
            check_cancelled(cancel_event)
            if self.is_blocked():
                return None
            target = self._canonical_cache.get(url, url)
            human = self._human_url(url)
            challenge_attempts = 0
            ssr_redirects = 0
            network_failures = 0
            while True:
                check_cancelled(cancel_event)
                if self.is_blocked():
                    return None
                try:
                    response, actual_url = self._get_page_response(target, cancel_event)
                    challenged = self._is_firewall(response) or extract_pow_challenge(response, self.session)
                    if challenged:
                        if challenge_attempts >= 2:
                            self._register_block(response.status_code, response.headers)
                            return None
                        challenge_attempts += 1
                        logger.info("🛡 %s: проверка HTTP %s (попытка %d/2)", human, response.status_code, challenge_attempts)
                        accepted = handle_anti_bot_challenge(self.session, response, actual_url, cancel_event=cancel_event)
                        check_cancelled(cancel_event)
                        if not accepted:
                            self._register_block(response.status_code, response.headers)
                            return None
                        # Preserve the same session/IP. Do not save clearance cookies yet.
                        continue

                    if response.status_code != 200:
                        logger.warning("⚠️ %s: Авито ответил HTTP %s", human, response.status_code)
                        return None
                    page_html = response.text
                    if '"isSuspenseRedirect":true' in page_html:
                        match = re.search(r'"redirect"\s*:\s*"([^"]+)"', page_html)
                        if not match or ssr_redirects >= 2:
                            return None
                        redirect_path = match.group(1).replace(r"\u0026", "&")
                        target = urljoin(actual_url, redirect_path)
                        self._validate_avito_url(target)
                        ssr_redirects += 1
                        continue

                    if not page_html or not re.search(r"<(?:!doctype\s+html|html|head|body|script)\b", page_html, re.I):
                        logger.warning("⚠️ %s: HTTP 200 без HTML страницы", human)
                        return None
                    if self.block_streak:
                        logger.info("✅ Доступ к целевой странице Авито восстановлен")
                    self.block_streak = 0
                    self.blocked_until = 0
                    self.network_block.register_success()
                    if ssr_redirects:
                        if len(self._canonical_cache) >= 1000:
                            self._canonical_cache.pop(next(iter(self._canonical_cache)))
                        self._canonical_cache[url] = target
                    self._save_cookies()
                    return page_html
                except PowSearchCancelled:
                    raise
                except PowSearchError as exc:
                    logger.warning("PoW stopped: %s", exc)
                    self._register_block(439)
                    return None
                except ValueError as exc:
                    logger.warning("Некорректный ответ/URL Авито: %s", exc)
                    return None
                except Exception as exc:
                    check_cancelled(cancel_event)
                    network_failures += 1
                    if network_failures >= 2:
                        logger.warning("🌐 %s: сетевой сбой (%s)", human, type(exc).__name__)
                        return None
                    if cancel_event is not None:
                        if cancel_event.wait(1.0):
                            check_cancelled(cancel_event)
                    else:
                        time.sleep(1.0)

    def fetch_item_details(self, item_id: str | int) -> Optional[dict]:
        """
        Загружает карточку лота через легковесный эндпоинт Mobile API (m.avito.ru):
        - точное количество просмотров (всего и за сегодня)
        - полное развернутое описание лота
        """
        if self.is_blocked() or not str(item_id).isdigit():
            return None
        with self._session_lock:
            if self.is_blocked():
                return None
            return self._fetch_item_details(item_id)

    def _fetch_item_details(self, item_id: str | int) -> Optional[dict]:
        try:
            url = f"https://m.avito.ru/api/1/card/items/{item_id}"
            headers = {
                "accept": "application/json, text/plain, */*",
                "referer": "https://m.avito.ru/",
                "user-agent": get_browser_user_agent(),
            }
            r = self.session.get(url, headers=headers, timeout=10, allow_redirects=False)
            if self._is_firewall(r) or extract_pow_challenge(r, self.session):
                self._register_block(r.status_code, r.headers)
                return None
            if r.status_code == 200:
                payload = r.json()
                if isinstance(payload, dict):
                    mobile = payload.get("success", {}).get("mobile", {})
                    stats = mobile.get("stats", {}) or {}
                    views = stats.get("views", {}) or {}
                    total_views = views.get("total")
                    today_views = views.get("today")
                    full_desc = mobile.get("description")
                    return {
                        "total_views": total_views,
                        "today_views": today_views,
                        "full_description": full_desc.strip() if isinstance(full_desc, str) else None,
                    }
            return None
        except Exception as e:
            logger.debug(f"Ошибка получения деталей лота {item_id}: {e}")
            return None


    @staticmethod
    def extract_search_title(html: str, fallback_url: str) -> str:
        """Извлекает читаемое название поиска из HTML (например 'iPhone 15')."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.find("h1")
            if h1 and h1.text.strip():
                title = h1.text.replace("\xa0", " ").strip()
                return title

            title_tag = soup.find("title")
            if title_tag and title_tag.text.strip():
                t = title_tag.text.replace("\xa0", " ").strip()
                t = t.split("—")[0].split("|")[0].split("купить")[-1].strip()
                if t:
                    return t
        except Exception:
            pass

        parsed = urlparse(fallback_url)
        parts = [p for p in parsed.path.split("/") if p and p != "all"]
        return parts[-1] if parts else "Поиск Авито"

    @staticmethod
    def _clean_seller_info(seller_elem) -> Tuple[Optional[str], Optional[str]]:
        if not seller_elem:
            return None, None
        full_text = seller_elem.get_text(separator=" ", strip=True)

        name = None
        a_tag = seller_elem.find("a")
        if a_tag and a_tag.text.strip():
            name = a_tag.text.strip()
        else:
            p_tag = seller_elem.find("p")
            if p_tag and p_tag.text.strip():
                name = p_tag.text.strip()

        if not name:
            m = re.search(r"^[^\d]+", full_text)
            name = m.group(0).strip() if m else "Продавец"

        for bad in ["Самозанятый", "Компания", "Частное лицо", "Реквизиты проверены"]:
            name = name.replace(bad, "").strip()

        name = re.sub(r"\s+", " ", name).strip()
        if not name or len(name) < 2:
            name = "Продавец"

        pure_name = name if name != "Продавец" else None

        rating_match = re.search(r"([1-5][,\.]\d)", full_text)
        rating = rating_match.group(1).replace(".", ",") if rating_match else None

        reviews_match = re.search(r"(\d+[\s\d]*\s*отзыв(?:а|ов)?)", full_text)
        reviews = reviews_match.group(1).strip() if reviews_match else None

        full_str = name
        if rating and reviews:
            full_str = f"{name} · ⭐ {rating} ({reviews})"
        elif rating:
            full_str = f"{name} · ⭐ {rating}"

        return full_str, pure_name

    @staticmethod
    def _clean_description(desc_elem, date_str: Optional[str] = None) -> Optional[str]:
        if not desc_elem:
            return None

        # Удаляем вложенные теги даты
        for d in desc_elem.find_all(attrs={"data-marker": lambda m: m and "date" in m}):
            d.decompose()
        for d in desc_elem.find_all(class_=lambda c: c and "date" in c):
            d.decompose()

        txt = desc_elem.get_text(separator=" ", strip=True)
        if date_str and date_str in txt:
            txt = txt.replace(date_str, "").strip()

        # Отрезаем прилипшую дату с конца
        txt = re.sub(r"[\.\,\s]*(\d+\s+(?:секунд|минут|час|дня|дней|недел|месяц)\w*\s+назад|только\s+что)[\.\s]*$", "", txt, flags=re.I).strip()
        return txt if txt else None

    @staticmethod
    def _extract_delivery_and_condition(it) -> Tuple[Optional[str], Optional[str]]:
        full_text = it.text

        condition = None
        for c in ["Новое", "Идеальное", "Отличное", "Хорошее", "Удовлетворительное"]:
            if c.lower() in full_text.lower():
                condition = c
                break

        delivery = None
        m = re.search(r"(?:Авито\s+)?Доставка(?:\s+от\s+\d+\s+дн[яей]+)?", full_text, re.I)
        if m:
            deliv = m.group(0).strip()
            if not deliv.lower().startswith("авито"):
                deliv = f"Авито {deliv}"
            delivery = deliv
        elif "доставка" in full_text.lower():
            delivery = "Авито Доставка"

        return delivery, condition

    @staticmethod
    def _extract_image(it) -> Optional[str]:
        """
        Извлекает фото товара:
        1. Из data-marker="slider-image/image-..." (Avito lazy load)
        2. Из тегов <img> (src, data-src, srcset)
        3. Из блока item-photo с ссылками на img.avito.st
        """
        raw_url = None
        # 1. Слайдер изображений Авито
        li_img = it.find(attrs={"data-marker": lambda m: m and "slider-image/image-" in m})
        if li_img:
            marker = li_img.get("data-marker", "")
            url = marker.split("slider-image/image-")[-1].strip()
            if url.startswith("http"):
                raw_url = url

        # 2. Обычные теги img и srcset
        if not raw_url:
            for img in it.find_all("img"):
                src = img.get("src") or img.get("data-src")
                if src and src.startswith("http"):
                    raw_url = src
                    break
                srcset = img.get("srcset")
                if srcset:
                    parts = srcset.split(",")
                    if parts:
                        first = parts[0].strip().split(" ")[0]
                        if first.startswith("http"):
                            raw_url = first
                            break

        # 3. Блок item-photo
        if not raw_url:
            photo_div = it.find(attrs={"data-marker": "item-photo"})
            if photo_div:
                for tag in photo_div.find_all(True):
                    for attr_name, attr_val in tag.attrs.items():
                        if isinstance(attr_val, str) and "img.avito.st" in attr_val:
                            m = re.search(r"(https?://[^\s\"\']+\.img\.avito\.st/[^\s\"\']+)", attr_val)
                            if m:
                                raw_url = m.group(1)
                                break
                    if raw_url:
                        break

        if raw_url:
            return re.sub(r"https?://[^.]+\.img\.avito\.st/", "https://c80.img.avito.st/", raw_url)
        return None

    @classmethod
    def _parse_items_from_ssr_json(cls, html: str) -> Optional[List[AvitoItem]]:
        """
        Извлекает структурированный каталог объявлений из встроенного в страницу SSR JSON (loaderData -> catalog -> items).
        Работает в 100 раз быстрее BeautifulSoup и отдает точный timestamp публикации в миллисекундах.
        """
        try:
            catalog_items = None
            candidates: List[str] = []

            # Быстрый путь: блок состояния страницы всегда начинается с {"i18n" внутри <script>
            start = html.find('{"i18n"')
            if start != -1:
                end = html.find("</script>", start)
                if end != -1:
                    candidates.append(html[start:end])

            # Запасной путь: перебор всех script-тегов
            if not candidates:
                candidates = [
                    s for s in re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
                    if '"catalog"' in s and '"items"' in s and len(s) > 10000
                ]

            for raw in candidates:
                raw = raw.strip()
                if not raw.startswith("{"):
                    m = re.search(r'(\{[\s\S]*\})', raw)
                    if not m:
                        continue
                    raw = m.group(1)
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                items = data.get("loaderData", {}).get("data", {}).get("catalog", {}).get("items")
                if items and isinstance(items, list):
                    catalog_items = items
                    break

            if not catalog_items:
                return None

            results: List[AvitoItem] = []
            for it in catalog_items[:25]:
                raw_id = it.get("id")
                if not raw_id:
                    continue
                item_id = str(raw_id)
                title = (it.get("title") or "Объявление").replace("\xa0", " ").strip()

                url_path = it.get("urlPath") or ""
                clean_path = url_path.split("?")[0] if url_path else f"/item/{item_id}"
                full_url = f"https://www.avito.ru{clean_path}" if clean_path.startswith("/") else clean_path

                # Цена
                price_det = it.get("priceDetailed") or {}
                raw_price = None
                if price_det.get("value") is not None:
                    try:
                        raw_price = int(price_det.get("value"))
                    except Exception:
                        pass
                was_lowered = bool(price_det.get("wasLowered"))
                string_without_discount = price_det.get("stringWithoutDiscount")
                discount_percent = price_det.get("discountPercent")

                price = price_det.get("fullString")
                if not price and raw_price is not None:
                    try:
                        price = f"{raw_price:,} ₽".replace(",", " ")
                    except Exception:
                        price = f"{raw_price} ₽"
                if not price:
                    price = "Цена не указана"

                # Фото (выбираем наибольшее доступное разрешение)
                image_url = None
                imgs = it.get("images") or []
                if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
                    first_img = imgs[0]
                    for res in ["636x636", "472x472", "416x416", "318x318", "236x236", "208x208"]:
                        if res in first_img and first_img[res].startswith("http"):
                            image_url = first_img[res]
                            break
                    if not image_url and first_img:
                        for v in first_img.values():
                            if isinstance(v, str) and v.startswith("http"):
                                image_url = v
                                break

                if image_url:
                    # Нормализуем на быстрый глобальный Anycast CDN c80 (без региональных сбросов соединений)
                    image_url = re.sub(r"https?://[^.]+\.img\.avito\.st/", "https://c80.img.avito.st/", image_url)

                # Точный timestamp публикации (в секундах)
                sort_ts = it.get("sortTimeStamp") or it.get("allowTimeStamp")
                exact_ts = int(sort_ts / 1000) if sort_ts else None

                # Компоненты IVA (описание, продавец, доставка)
                iva = it.get("iva") or {}

                # Описание
                description = None
                desc_steps = iva.get("DescriptionStep") or []
                if desc_steps and isinstance(desc_steps, list):
                    description = (desc_steps[0].get("payload") or {}).get("description")

                # Продавец и рейтинг
                seller = None
                seller_name = None
                user_steps = iva.get("UserInfoStep") or []
                for step in user_steps:
                    payload = step.get("payload") or {}
                    if "profile" in payload:
                        prof_title = (payload.get("profile") or {}).get("title")
                        if prof_title:
                            seller_name = prof_title.strip()
                        rating_data = payload.get("rating") or {}
                        score = rating_data.get("score")
                        summary = rating_data.get("summary")
                        if seller_name and score:
                            seller = f"{seller_name} · ⭐ {score}" + (f" ({summary})" if summary else "")
                        elif seller_name:
                            seller = seller_name
                        break

                if not seller_name:
                    raw_seller = it.get("seller") or {}
                    if isinstance(raw_seller, dict):
                        seller_name = raw_seller.get("name")
                        score = raw_seller.get("score")
                        if seller_name and score:
                            seller = f"{seller_name} · ⭐ {score}"
                        elif seller_name:
                            seller = seller_name

                # Доставка
                delivery = None
                for step in iva.get("GeoStep") or []:
                    if (step.get("componentData") or {}).get("component") == "delivery":
                        deliv_text = (step.get("payload") or {}).get("text")
                        if deliv_text:
                            delivery = f"Авито {deliv_text}" if not deliv_text.lower().startswith("авито") else deliv_text
                        break
                if not delivery:
                    for step in iva.get("PriceStep") or []:
                        if (step.get("componentData") or {}).get("component") == "delivery":
                            delivery = "Авито Доставка"
                            break

                # Адрес (выбираем максимально подробный адрес: улица, метро, город)
                address = None
                geo = it.get("geo") or {}
                coords = it.get("coords") or {}
                addr_det = it.get("addressDetailed") or {}
                loc = it.get("location") or {}
                gallery = it.get("gallery") or {}
                if geo.get("formattedAddress"):
                    address = geo["formattedAddress"]
                elif coords.get("address_user"):
                    address = coords["address_user"]
                elif addr_det.get("locationName"):
                    address = addr_det["locationName"]
                elif loc.get("name"):
                    address = loc["name"]
                elif gallery.get("imageAlt"):
                    alt = gallery["imageAlt"]
                    if alt and "," in alt:
                        address = alt.split(",")[-1].strip()

                # Дата в читаемом виде: ПРИОРИТЕТ ТОЧНОМУ TIMESTAMP!
                date_info = "Только что"
                date_steps = iva.get("DateInfoStep") or []
                if exact_ts:
                    diff_sec = max(0, int(time.time() - exact_ts))
                    if diff_sec < 60:
                        date_info = f"{diff_sec} сек. назад"
                    elif diff_sec < 3600:
                        date_info = f"{diff_sec // 60} мин. назад"
                    else:
                        date_info = f"{diff_sec // 3600} ч. назад"
                elif date_steps:
                    date_info = (date_steps[0].get("payload") or {}).get("relative") or "Только что"

                # Резерв и Авито Доставка
                is_reserved = bool(it.get("isReserved"))

                # Продвижение (VAS / Promotion)
                is_promotion = bool(it.get("isPromotion") or it.get("isReMapPromotion") or it.get("vip"))
                if not is_promotion:
                    for step in date_steps:
                        vas = (step.get("payload") or {}).get("vas") or []
                        if any(isinstance(v, dict) and (v.get("title") == "Продвинуто" or v.get("name") in ("vas", "xl", "highlight")) for v in vas):
                            is_promotion = True
                            break

                # Преимущества и бейджи (скидка на доставку, 14 дней на возврат, торг и т.д.)
                badges_list = []
                raw_badges = it.get("badges") or []
                if isinstance(raw_badges, list):
                    for b in raw_badges:
                        if isinstance(b, dict):
                            t = b.get("title") or b.get("text") or b.get("description")
                            if t and isinstance(t, str) and t.strip():
                                badges_list.append(t.strip())
                        elif isinstance(b, str) and b.strip():
                            badges_list.append(b.strip())

                for step in (iva.get("BadgeStep") or []):
                    payload = step.get("payload") or {}
                    t = payload.get("text") or payload.get("title")
                    if t and t not in badges_list:
                        badges_list.append(t)

                for step in (iva.get("TrustStep") or []):
                    payload = step.get("payload") or {}
                    t = payload.get("text") or payload.get("title")
                    if t and t not in badges_list:
                        badges_list.append(t)

                if it.get("hasBargain") or it.get("bargain"):
                    if "Торг уместен" not in badges_list:
                        badges_list.append("Торг уместен")

                results.append(AvitoItem(
                    item_id=item_id,
                    title=title,
                    price=price,
                    url=full_url,
                    image_url=image_url,
                    address=address,
                    date=date_info,
                    description=description,
                    seller=seller,
                    seller_name=seller_name,
                    delivery=delivery,
                    condition=None,
                    timestamp=exact_ts,
                    is_reserved=is_reserved,
                    is_promotion=is_promotion,
                    raw_price=raw_price,
                    was_lowered=was_lowered,
                    string_without_discount=string_without_discount,
                    discount_percent=discount_percent,
                    badges=badges_list if badges_list else None
                ))

            return results
        except Exception as e:
            logger.debug(f"Ошибка парсинга SSR JSON: {e}")
            return None

    @classmethod
    def parse_items(cls, html: str) -> List[AvitoItem]:
        """
        Парсит органические объявления на странице выдачи:
        1. Сначала пробует мгновенный парсинг через встроенный SSR JSON (с точными timestamp).
        2. При отсутствии JSON в разметке использует надежный BeautifulSoup-парсер (фолбэк).
        """
        # ⚡️ 1. Мгновенное извлечение через чистый встроенный SSR JSON
        json_items = cls._parse_items_from_ssr_json(html)
        if json_items:
            return json_items

        # 2. Фолбэк на HTML-разметку (BeautifulSoup)
        soup = BeautifulSoup(html, "html.parser")
        
        # 1. Ищем ТОЛЬКО контейнер органической поисковой выдачи
        main_catalog = soup.find("div", {"data-marker": "catalog-serp"})
        
        if not main_catalog:
            # Фолбэк, если Авито отдало нестандартную разметку
            items_raw = soup.find_all(attrs={"data-marker": "item"}, limit=25)
        else:
            # 2. Удаляем из каталога карусели VIP/рекламы и врезки других регионов
            for junk in main_catalog.find_all(attrs={"data-marker": ["witcher/block", "items-vip"]}):
                junk.decompose()
            for junk in main_catalog.find_all(class_=lambda c: c and any(x in c for x in ["items-vip", "witcher"])):
                junk.decompose()

            # 3. Берем карточки только из очищенного контейнера (топ-25)
            items_raw = main_catalog.find_all(attrs={"data-marker": "item"}, limit=25)

        results: List[AvitoItem] = []

        for it in items_raw:
            try:
                # 1. ID объявления
                item_id = it.get("data-item-id") or it.get("id")
                if not item_id:
                    continue
                item_id = str(item_id)

                # 2. Название и ссылка
                title_elem = it.find(attrs={"data-marker": "item-title"})
                if not title_elem:
                    continue
                title = title_elem.text.replace("\xa0", " ").strip()
                href = title_elem.get("href", "")
                full_url = f"https://www.avito.ru{href}" if href.startswith("/") else href

                # 3. Цена
                price = "Цена не указана"
                price_meta = it.find(attrs={"itemprop": "price"})
                if price_meta and price_meta.get("content"):
                    price_val = price_meta.get("content")
                    try:
                        price = f"{int(price_val):,} ₽".replace(",", " ")
                    except Exception:
                        price = f"{price_val} ₽"
                else:
                    price_elem = it.find(attrs={"data-marker": "item-price"})
                    if price_elem:
                        price = price_elem.text.replace("\xa0", " ").strip()

                # 4. Фотография (100% извлечение через слайдер и lazy-load)
                image_url = cls._extract_image(it)

                # 5. Адрес / Город / Метро
                address = None
                geo_elem = it.find(attrs={"data-marker": "item-location"}) or it.find(attrs={"data-marker": "item-address"})
                if not geo_elem:
                    geo_elem = it.find(class_=lambda x: x and ("geo-root" in x or "item-location" in x or "geo-geo" in x))
                if geo_elem:
                    address = geo_elem.get_text(separator=" ", strip=True).replace("\xa0", " ")
                    # Убираем дублирование пробелов
                    address = re.sub(r"\s+", " ", address)

                # 6. Дата публикации
                date = None
                date_elem = it.find(attrs={"data-marker": "item-date"})
                if not date_elem:
                    date_elem = it.find(class_=lambda x: x and "date-root" in x)
                if date_elem:
                    date = date_elem.text.replace("\xa0", " ").strip()

                # 7. Описание (аккуратно очищенное от даты)
                desc_elem = (
                    it.find(attrs={"data-marker": "item-description"}) or
                    it.find(class_=lambda x: x and ("bottomBlock-" in x or "iva-item-description" in x))
                )
                description = cls._clean_description(desc_elem, date)

                # 8. Продавец и рейтинг
                seller_elem = (
                    it.find(class_=lambda x: x and ("sellerInfo-" in x or "iva-item-sellerInfo" in x or "userInfoStep-" in x)) or
                    it.find(attrs={"data-marker": "item-line/company"})
                )
                seller, seller_name = cls._clean_seller_info(seller_elem)

                # 9. Доставка и состояние
                delivery, condition = cls._extract_delivery_and_condition(it)

                is_reserved = False
                if it.find(string=re.compile(r"забронирован|в резерве", re.I)):
                    is_reserved = True

                is_promotion = False
                if it.find(class_=lambda c: c and any(x in c for x in ["items-vip", "witcher", "vas", "promotion", "highlight"])):
                    is_promotion = True

                results.append(AvitoItem(
                    item_id=item_id,
                    title=title,
                    price=price,
                    url=full_url,
                    image_url=image_url,
                    address=address,
                    date=date,
                    description=description,
                    seller=seller,
                    seller_name=seller_name,
                    delivery=delivery,
                    condition=condition,
                    is_reserved=is_reserved,
                    is_promotion=is_promotion
                ))

            except Exception as e:
                logger.debug(f"Ошибка при обработке элемента: {e}")
                continue

        return results

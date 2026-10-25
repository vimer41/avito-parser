import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests
from config import PROXY_URL, DATA_DIR

logger = logging.getLogger(__name__)

COOKIE_FILE = DATA_DIR / "avito_cookies.json"
WORKING_PROXY_FILE = DATA_DIR / "working_proxy.txt"

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


class AvitoParser:
    _WORKING_PROXY_OVERRIDE: Optional[str] = None

    def __init__(self, proxy: Optional[str] = PROXY_URL):
        if AvitoParser._WORKING_PROXY_OVERRIDE:
            self.proxy = AvitoParser._WORKING_PROXY_OVERRIDE
        elif WORKING_PROXY_FILE.exists():
            try:
                saved = WORKING_PROXY_FILE.read_text("utf-8").strip()
                if saved:
                    AvitoParser._WORKING_PROXY_OVERRIDE = saved
                    self.proxy = saved
                else:
                    self.proxy = proxy
            except Exception:
                self.proxy = proxy
        else:
            self.proxy = proxy

        self.current_proxy = self.proxy
        self.proxy_fail_count = 0
        self.session = self._create_session(self.current_proxy)
        self._load_cookies()

    def _create_session(self, proxy: Optional[str] = None) -> requests.Session:
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        session = requests.Session(
            impersonate="chrome120",
            proxies=proxies,
            headers=headers,
            timeout=20
        )
        return session

    def _save_cookies(self):
        """Сохраняет текущие валидные куки сессии на диск (исключая временный pow_challenge)."""
        try:
            cookie_dict = dict(self.session.cookies)
            cookie_dict.pop("pow_challenge", None)
            if cookie_dict:
                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
                logger.debug(f"Куки сохранены в {COOKIE_FILE.name} ({len(cookie_dict)} шт.)")
        except Exception as e:
            logger.debug(f"Не удалось сохранить куки: {e}")

    def _load_cookies(self):
        """Загружает сохраненные куки с диска в текущую сессию."""
        if not COOKIE_FILE.exists():
            return
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                cookie_dict = json.load(f)
            if isinstance(cookie_dict, dict) and cookie_dict:
                cookie_dict.pop("pow_challenge", None)
                for k, v in cookie_dict.items():
                    self.session.cookies.set(k, str(v), domain=".avito.ru", path="/")
                    self.session.cookies.set(k, str(v), domain="www.avito.ru", path="/")
                logger.info(f"🍪 Загружено {len(cookie_dict)} сохранённых кук из {COOKIE_FILE.name}")
        except Exception as e:
            logger.warning(f"Ошибка загрузки кук из файла: {e}")

    def _init_session(self, clear_cookies: bool = False):
        """Пересоздание сессии. Если clear_cookies=False, куки сохраняются/подгружаются."""
        try:
            self.session.close()
        except Exception:
            pass
        if clear_cookies and COOKIE_FILE.exists():
            try:
                COOKIE_FILE.unlink()
                logger.info("Файл кук удален для полной очистки сессии.")
            except Exception:
                pass

        self.session = self._create_session(self.current_proxy)
        if not clear_cookies:
            self._load_cookies()

    def _handle_proxy_error(self, err: Exception):
        """Обработка ошибок прокси: автоматический переход на socks5 или фолбэк на прямое соединение."""
        self.proxy_fail_count += 1
        err_str = str(err)
        logger.warning(f"⚠️ Ошибка прокси (сбой #{self.proxy_fail_count}): {err_str[:120]}")

        # Если HTTP-прокси упал с CONNECT tunnel failed (часто бывает на портах, где работает SOCKS5),
        # пробуем переключиться на socks5://
        if self.current_proxy and self.current_proxy.startswith("http://") and "CONNECT tunnel failed" in err_str:
            logger.info("🔄 Прокси вернул ошибку CONNECT tunnel на HTTP. Автоматически переключаем на протокол SOCKS5...")
            self.current_proxy = self.current_proxy.replace("http://", "socks5://", 1)
            self.proxy = self.current_proxy
            AvitoParser._WORKING_PROXY_OVERRIDE = self.current_proxy
            try:
                WORKING_PROXY_FILE.parent.mkdir(parents=True, exist_ok=True)
                WORKING_PROXY_FILE.write_text(self.current_proxy, "utf-8")
            except Exception:
                pass
            self.proxy_fail_count = 0
            self._init_session(clear_cookies=False)
            return

        # Если 2 сбоя подряд — временный фолбэк на прямое соединение
        if self.proxy_fail_count >= 2:
            logger.warning("🚨 Прокси заблокирован Авито или недоступен! Включаем фолбэк на прямое подключение (без прокси).")
            self.current_proxy = None
            self.proxy = None
            AvitoParser._WORKING_PROXY_OVERRIDE = None
            try:
                if WORKING_PROXY_FILE.exists():
                    WORKING_PROXY_FILE.unlink()
            except Exception:
                pass
            self._init_session(clear_cookies=False)



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
        if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
            raw_url = "https://" + raw_url

        parsed = urlparse(raw_url)
        if "avito.ru" not in parsed.netloc:
            raise ValueError("Ссылка должна быть с сайта avito.ru")

        # Парсим параметры запроса
        query_params = parse_qs(parsed.query)

        # Удаляем мусорные / сессионные параметры, меняющиеся между запросами
        junk_keys = {"context", "localPriority", "presentationType", "from", "searchRadius", "view"}
        for k in list(query_params.keys()):
            if k in junk_keys or k.startswith("utm_"):
                query_params.pop(k, None)

        # Устанавливаем сортировку по дате (сначала новые)
        query_params["s"] = ["104"]

        # Сортируем параметры для стабильного хеша URL при группировке
        sorted_params = sorted(query_params.items())
        new_query = urlencode(sorted_params, doseq=True)

        normalized = urlunparse((
            parsed.scheme or "https",
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            ""  # fragment убираем
        ))
        return normalized

    def _solve_pow_challenge(self, pow_challenge: str) -> bool:
        """
        Автоматическое решение криптографической капчи Proof-of-Work (PoW) Авито.
        """
        try:
            headers = {
                "Content-Type": "application/json",
                "Referer": "https://www.avito.ru/",
                "Origin": "https://www.avito.ru"
            }
            # 1. Получаем JWT с заданием
            get_resp = self.session.post(
                "https://www.avito.ru/web/3/firewallPow/get",
                json={"challenge": pow_challenge},
                headers=headers,
                timeout=10,
                allow_redirects=True
            )
            if get_resp.status_code != 200:
                logger.error(f"Ошибка получения pow challenge: {get_resp.status_code}")
                try:
                    self.session.cookies.pop("pow_challenge", None)
                except Exception:
                    pass
                return False

            get_data = get_resp.json()
            challenge_jwt = get_data.get("success", {}).get("result", {}).get("challenge_jwt")
            if not challenge_jwt:
                return False

            # 2. Декодируем полезную нагрузку JWT
            payload_b64 = challenge_jwt.split(".")[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())

            cid = payload.get("id")
            compl = payload.get("compl", 3)
            prefix = "0" * compl

            # 3. Решаем sha256 PoW
            nonce = 0
            while True:
                s = f"{cid}:{nonce}".encode()
                if hashlib.sha256(s).hexdigest().startswith(prefix):
                    break
                nonce += 1

            # 4. Отправляем решение на проверку
            verify_resp = self.session.post(
                "https://www.avito.ru/web/3/firewallPow/verify",
                json={"challenge": challenge_jwt, "nonce": nonce},
                headers=headers,
                timeout=10,
                allow_redirects=True
            )

            if verify_resp.status_code == 200:
                v_data = verify_resp.json()
                if v_data.get("success", {}).get("result", {}).get("verified"):
                    self.session.cookies.set("pow_solved", "1", domain=".avito.ru", path="/")
                    self.session.cookies.set("pow_solved", "1", domain="www.avito.ru", path="/")
                    self._save_cookies()
                    logger.info("✅ PoW успешно решен и кука pow_solved установлена!")
                    return True

            logger.warning(f"PoW не подтвержден: {verify_resp.text}")
            return False

        except Exception as e:
            logger.error(f"Исключение при решении PoW: {e}")
            return False

    def fetch_page(self, url: str) -> Optional[str]:
        """
        Загружает страницу Авито с автообходом защиты QRATOR / PoW, персистентными куками и фолбэком прокси.
        """
        for attempt in range(2):
            try:
                r = self.session.get(url, timeout=20, allow_redirects=True)

                # Проверяем, не попали ли мы на страницу блокировки /blocked
                if "/blocked" in str(r.url):
                    logger.warning(f"🚫 Редирект на страницу блокировки: {r.url}")

                # Если получили 403/429/439 или challenge
                pow_challenge = self.session.cookies.get("pow_challenge") or r.cookies.get("pow_challenge")
                if (r.status_code in (403, 429, 439) or "firewall-container" in r.text):
                    if not pow_challenge:
                        logger.info("Обновляем сессию и запрашиваем свежий PoW-челлендж через главную страницу avito.ru...")
                        r_main = self.session.get("https://www.avito.ru", timeout=15, allow_redirects=True)
                        pow_challenge = self.session.cookies.get("pow_challenge") or r_main.cookies.get("pow_challenge")

                    if pow_challenge:
                        logger.info("Обнаружена PoW-защита Авито, решаем...")
                        if self._solve_pow_challenge(pow_challenge):
                            time.sleep(1.0)
                            r = self.session.get(url, timeout=20, allow_redirects=True)
                            logger.info(f"Повторный запрос после PoW: статус {r.status_code}")

                if r.status_code == 200:
                    self.proxy_fail_count = 0  # Сбрасываем счетчик ошибок при успешном запросе
                    self._save_cookies()       # Сохраняем свежие куки сессии

                    # Проверяем, не является ли страница внутренним SSR-редиректом Авито
                    if '"isSuspenseRedirect":true' in r.text or ('"redirect":' in r.text and '"items"' not in r.text):
                        m_redir = re.search(r'"redirect"\s*:\s*"([^"]+)"', r.text)
                        if m_redir:
                            redir_path = m_redir.group(1).replace(r"\u0026", "&")
                            canonical_url = "https://www.avito.ru" + redir_path if redir_path.startswith("/") else redir_path
                            logger.info(f"🔄 [АВИТО 301] Переход на канонический URL: {self._human_url(canonical_url)}")
                            r_canon = self.session.get(canonical_url, timeout=20, allow_redirects=True)
                            if r_canon.status_code == 200:
                                return r_canon.text

                    return r.text
                else:
                    logger.warning(f"Не удалось получить {self._human_url(url)} (статус {r.status_code})")
                    if self.current_proxy and r.status_code in (403, 429):
                        self._handle_proxy_error(Exception(f"Avito статус {r.status_code} на прокси"))
                    if attempt == 0 and r.status_code in (403, 429):
                        time.sleep(2.0)
                        continue
                    return None

            except Exception as e:
                err_msg = str(e)
                if "curl: (28)" in err_msg or "Proxy" in type(e).__name__ or "CONNECT tunnel" in err_msg or "ConnectionError" in type(e).__name__:
                    self._handle_proxy_error(e)

                if attempt == 0:
                    logger.warning(f"Сбой подключения к {self._human_url(url)} ({e}), повтор через 2 сек...")
                    time.sleep(2.0)
                    self._init_session(clear_cookies=False)
                    continue
                logger.error(f"Ошибка запроса к {self._human_url(url)}: {e}")
                return None
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
        # 1. Слайдер изображений Авито
        li_img = it.find(attrs={"data-marker": lambda m: m and "slider-image/image-" in m})
        if li_img:
            marker = li_img.get("data-marker", "")
            url = marker.split("slider-image/image-")[-1].strip()
            if url.startswith("http"):
                return url

        # 2. Обычные теги img и srcset
        for img in it.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src and src.startswith("http"):
                return src
            srcset = img.get("srcset")
            if srcset:
                parts = srcset.split(",")
                if parts:
                    first = parts[0].strip().split(" ")[0]
                    if first.startswith("http"):
                        return first

        # 3. Блок item-photo
        photo_div = it.find(attrs={"data-marker": "item-photo"})
        if photo_div:
            for tag in photo_div.find_all(True):
                for attr_name, attr_val in tag.attrs.items():
                    if isinstance(attr_val, str) and "img.avito.st" in attr_val:
                        m = re.search(r"(https?://[^\s\"\']+\.img\.avito\.st/[^\s\"\']+)", attr_val)
                        if m:
                            return m.group(1)

        return None

    @classmethod
    def _parse_items_from_ssr_json(cls, html: str) -> Optional[List[AvitoItem]]:
        """
        Извлекает структурированный каталог объявлений из встроенного в страницу SSR JSON (loaderData -> catalog -> items).
        Работает в 100 раз быстрее BeautifulSoup и отдает точный timestamp публикации в миллисекундах.
        """
        try:
            scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
            catalog_items = None

            for s in scripts:
                if '"catalog"' in s and '"items"' in s and len(s) > 10000:
                    m = re.search(r'(\{[\s\S]*\})', s)
                    if m:
                        try:
                            data = json.loads(m.group(1))
                            items = data.get("loaderData", {}).get("data", {}).get("catalog", {}).get("items")
                            if items and isinstance(items, list):
                                catalog_items = items
                                break
                        except Exception:
                            continue

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
                price = price_det.get("fullString")
                if not price and price_det.get("value") is not None:
                    try:
                        price = f"{int(price_det.get('value')):,} ₽".replace(",", " ")
                    except Exception:
                        price = f"{price_det.get('value')} ₽"
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

                # Точный timestamp публикации (в секундах)
                sort_ts = it.get("sortTimeStamp") or it.get("allowTimeStamp")
                exact_ts = int(sort_ts / 1000) if sort_ts else None

                # Компоненты IVA (описание, продавец, доставка)
                iva = it.get("iva") or {}

                # Описание
                description = None
                desc_steps = iva.get("DescriptionStep") or []
                if desc_steps and isinstance(desc_steps, list):
                    description = desc_steps[0].get("payload", {}).get("description")

                # Продавец и рейтинг
                seller = None
                seller_name = None
                user_steps = iva.get("UserInfoStep") or []
                for step in user_steps:
                    payload = step.get("payload") or {}
                    if "profile" in payload:
                        prof_title = payload.get("profile", {}).get("title")
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
                    if step.get("componentData", {}).get("component") == "delivery":
                        deliv_text = step.get("payload", {}).get("text")
                        if deliv_text:
                            delivery = f"Авито {deliv_text}" if not deliv_text.lower().startswith("авито") else deliv_text
                        break
                if not delivery:
                    for step in iva.get("PriceStep") or []:
                        if step.get("componentData", {}).get("component") == "delivery":
                            delivery = "Авито Доставка"
                            break

                # Адрес
                address = None
                addr_det = it.get("addressDetailed") or {}
                if addr_det.get("locationName"):
                    address = addr_det["locationName"]
                elif it.get("location", {}).get("name"):
                    address = it["location"]["name"]
                elif it.get("gallery", {}).get("imageAlt"):
                    alt = it["gallery"]["imageAlt"]
                    if "," in alt:
                        address = alt.split(",")[-1].strip()

                # Дата в читаемом виде
                date_info = "Только что"
                date_steps = iva.get("DateInfoStep") or []
                if date_steps:
                    date_info = date_steps[0].get("payload", {}).get("relative") or "Только что"
                elif exact_ts:
                    diff_sec = int(time.time() - exact_ts)
                    if diff_sec < 60:
                        date_info = f"{diff_sec} сек. назад"
                    elif diff_sec < 3600:
                        date_info = f"{diff_sec // 60} мин. назад"
                    else:
                        date_info = f"{diff_sec // 3600} ч. назад"

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
                    timestamp=exact_ts
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
                    condition=condition
                ))

            except Exception as e:
                logger.debug(f"Ошибка при обработке элемента: {e}")
                continue

        return results

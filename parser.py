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
from config import PROXY_URL

logger = logging.getLogger(__name__)

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
    delivery: Optional[str] = None


class AvitoParser:
    def __init__(self, proxy: Optional[str] = PROXY_URL):
        self.proxy = proxy
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

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

    @staticmethod
    def normalize_url(raw_url: str) -> str:
        """
        Нормализует ссылку:
        - добавляет https:// если нет
        - проверяет, что это avito.ru
        - устанавливает s=104 (сортировка 'Сначала новые')
        """
        raw_url = raw_url.strip()
        if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
            raw_url = "https://" + raw_url

        parsed = urlparse(raw_url)
        if "avito.ru" not in parsed.netloc:
            raise ValueError("Ссылка должна быть с сайта avito.ru")

        # Парсим параметры запроса
        query_params = parse_qs(parsed.query)
        # Устанавливаем сортировку по дате (сначала новые)
        query_params["s"] = ["104"]

        # Собираем query обратно (doseq=True чтобы списки разворачивались корректно)
        new_query = urlencode(query_params, doseq=True)

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
            # 1. Получаем JWT с заданием
            get_resp = self.session.post(
                "https://www.avito.ru/web/3/firewallPow/get",
                json={"challenge": pow_challenge},
                timeout=10
            )
            if get_resp.status_code != 200:
                logger.error(f"Ошибка получения pow challenge: {get_resp.status_code}")
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
                timeout=10
            )

            if verify_resp.status_code == 200:
                v_data = verify_resp.json()
                if v_data.get("success", {}).get("result", {}).get("verified"):
                    self.session.cookies.set("pow_solved", "1", domain=".avito.ru")
                    return True

            logger.warning(f"PoW не подтвержден: {verify_resp.text}")
            return False

        except Exception as e:
            logger.error(f"Исключение при решении PoW: {e}")
            return False

    def fetch_page(self, url: str) -> Optional[str]:
        """
        Загружает страницу Авито с автообходом защиты QRATOR / PoW.
        """
        try:
            r = self.session.get(url, timeout=20)

            # Если получили 429/439 или challenge
            pow_challenge = self.session.cookies.get("pow_challenge")
            if (r.status_code in (429, 439) or "firewall-container" in r.text) and pow_challenge:
                logger.info("Обнаружена PoW-защита Авито, решаем...")
                if self._solve_pow_challenge(pow_challenge):
                    time.sleep(0.5)
                    r = self.session.get(url, timeout=20)
                    logger.info(f"Повторный запрос после PoW: статус {r.status_code}")

            if r.status_code == 200:
                return r.text
            else:
                logger.warning(f"Не удалось получить страницу {url}, статус {r.status_code}")
                return None

        except Exception as e:
            logger.error(f"Ошибка запроса к {url}: {e}")
            return None

    @staticmethod
    def extract_search_title(html: str, fallback_url: str) -> str:
        """Извлекает читаемое название поиска из HTML (например 'iPhone 15')."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.find("h1")
            if h1 and h1.text.strip():
                title = h1.text.strip()
                # Убираем лишние фразы вроде 'купить'
                return title

            title_tag = soup.find("title")
            if title_tag and title_tag.text.strip():
                t = title_tag.text.strip()
                # Отрезаем ' | Авито' или '— купить...'
                t = t.split("—")[0].split("|")[0].split("купить")[-1].strip()
                if t:
                    return t
        except Exception:
            pass

        # Fallback: берем из пути URL
        parsed = urlparse(fallback_url)
        parts = [p for p in parsed.path.split("/") if p and p != "all"]
        return parts[-1] if parts else "Поиск Авито"

    @staticmethod
    def parse_items(html: str) -> List[AvitoItem]:
        """
        Парсит все объявления на странице выдачи.
        """
        soup = BeautifulSoup(html, "html.parser")
        items_raw = soup.find_all(attrs={"data-marker": "item"})
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
                title = title_elem.text.strip()
                href = title_elem.get("href", "")
                if href.startswith("/"):
                    full_url = f"https://www.avito.ru{href}"
                else:
                    full_url = href

                # 3. Цена
                price = "Цена не указана"
                price_meta = it.find(attrs={"itemprop": "price"})
                if price_meta and price_meta.get("content"):
                    price_val = price_meta.get("content")
                    # Форматируем с пробелами (например 25 000)
                    try:
                        price = f"{int(price_val):,} ₽".replace(",", " ")
                    except Exception:
                        price = f"{price_val} ₽"
                else:
                    price_elem = it.find(attrs={"data-marker": "item-price"})
                    if price_elem:
                        price = price_elem.text.strip()

                # 4. Фотография
                image_url = None
                img_tag = it.find("img")
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src")
                    if src and src.startswith("http"):
                        image_url = src

                # 5. Адрес / Город
                address = None
                geo_elem = it.find(attrs={"data-marker": "item-location"}) or it.find(attrs={"data-marker": "item-address"})
                if not geo_elem:
                    geo_elem = it.find(class_=lambda x: x and ("geo-root" in x or "item-location" in x))
                if geo_elem:
                    address = geo_elem.text.strip()

                # 6. Дата публикации
                date = None
                date_elem = it.find(attrs={"data-marker": "item-date"})
                if not date_elem:
                    date_elem = it.find(class_=lambda x: x and "date-root" in x)
                if date_elem:
                    date = date_elem.text.strip()

                # 7. Описание
                description = None
                desc_elem = (
                    it.find(attrs={"data-marker": "item-description"}) or
                    it.find(class_=lambda x: x and ("bottomBlock-" in x or "iva-item-description" in x))
                )
                if desc_elem:
                    description = desc_elem.text.strip()

                # 8. Продавец и рейтинг
                seller = None
                seller_elem = (
                    it.find(class_=lambda x: x and ("sellerInfo-" in x or "iva-item-sellerInfo" in x or "userInfoStep-" in x)) or
                    it.find(attrs={"data-marker": "item-line/company"})
                )
                if seller_elem:
                    seller_text = seller_elem.text.strip()
                    # Убираем дублирование пробелов
                    seller = re.sub(r"\s+", " ", seller_text)

                # 9. Доставка
                delivery = None
                deliv_elem = it.find(attrs={"data-marker": lambda m: m and "delivery" in m})
                if not deliv_elem:
                    deliv_elem = it.find(class_=lambda x: x and "delivery" in x.lower())
                if deliv_elem:
                    d_txt = deliv_elem.text.strip()
                    if d_txt and len(d_txt) < 50:
                        delivery = d_txt
                if not delivery:
                    # Поиск фрагмента с доставкой в тексте
                    for p_tag in it.find_all(["div", "span", "p"]):
                        t_p = p_tag.text.strip()
                        if 3 < len(t_p) < 40 and "доставка" in t_p.lower():
                            delivery = t_p
                            break

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
                    delivery=delivery
                ))

            except Exception as e:
                logger.debug(f"Ошибка при обработке элемента: {e}")
                continue

        return results

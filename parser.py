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
    condition: Optional[str] = None


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

            # Если получили 403/429/439 или challenge
            pow_challenge = self.session.cookies.get("pow_challenge") or r.cookies.get("pow_challenge")
            if (r.status_code in (403, 429, 439) or "firewall-container" in r.text) and pow_challenge:
                logger.info("Обнаружена PoW-защита Авито, решаем...")
                if self._solve_pow_challenge(pow_challenge):
                    time.sleep(0.5)
                    r = self.session.get(url, timeout=20)
                    logger.info(f"Повторный запрос после PoW: статус {r.status_code}")
            elif r.status_code == 403:
                # Если 403 и кука устарела, сбрасываем сессию
                logger.info("Сброс сессии из-за 403...")
                self._init_session()
                time.sleep(1.0)
                r = self.session.get(url, timeout=20)
                pow_challenge = self.session.cookies.get("pow_challenge") or r.cookies.get("pow_challenge")
                if pow_challenge:
                    self._solve_pow_challenge(pow_challenge)
                    r = self.session.get(url, timeout=20)

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
    def _clean_seller_info(seller_elem) -> Optional[str]:
        if not seller_elem:
            return None
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

        rating_match = re.search(r"([1-5][,\.]\d)", full_text)
        rating = rating_match.group(1).replace(".", ",") if rating_match else None

        reviews_match = re.search(r"(\d+[\s\d]*\s*отзыв(?:а|ов)?)", full_text)
        reviews = reviews_match.group(1).strip() if reviews_match else None

        if rating and reviews:
            return f"{name} · ⭐ {rating} ({reviews})"
        elif rating:
            return f"{name} · ⭐ {rating}"
        return name

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
    def parse_items(cls, html: str) -> List[AvitoItem]:
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
                seller = cls._clean_seller_info(seller_elem)

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
                    delivery=delivery,
                    condition=condition
                ))

            except Exception as e:
                logger.debug(f"Ошибка при обработке элемента: {e}")
                continue

        return results

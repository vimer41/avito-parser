#!/usr/bin/env python3
"""
Менеджер обхода защит Авито (Anti-Bot Manager):
1. QRATOR Challenge (HTTP 302/429):
   - Загрузка fingerprint-скрипта (/[0-9a-f]{20}.js) и favicon.ico
   - Генерация XTEA-зашифрованных полезных нагрузок 'f' и 's'
   - Отправка multipart POST на /web/2/ft и установка кук 'f' и 'ft'
   - Запрос отслеживающего пикселя /web/1/u?{pixel_id}
2. Firewall PoW Challenge (HTTP 439 / 429 / Cookies):
   - Извлечение челленджа из тела ответа JSON (response.json()['pow_challenge']) или кук
   - Запрос challenge_jwt на /web/3/firewallPow/get
   - Локальный расчет SHA-256 PoW nonce заданной сложности
   - Отправка решения на /web/3/firewallPow/verify и получение unblock_ttl
3. GeeTest v4 Captcha (HTTP 429 / 403 show captcha):
   - Определение типа капчи
   - Подключение локального решателя GeekedTest (при наличии ML-зависимостей)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from anti_bot.firewallpow_payload_reference import (
    build_get_payload,
    build_verify_payload,
)
from anti_bot.generate_qrator_variants import (
    DEFAULT_BASE_F,
    DEFAULT_BASE_S,
    generate_variants,
    load_f_base,
    load_s_base,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.avito.ru"
POW_GET_URL = f"{BASE_URL}/web/3/firewallPow/get"
POW_VERIFY_URL = f"{BASE_URL}/web/3/firewallPow/verify"
FIREWALL_CAPTCHA_GET_URL = f"{BASE_URL}/web/5/firewallCaptcha/get"
FIREWALL_CAPTCHA_VERIFY_URL = f"{BASE_URL}/web/3/firewallCaptcha/verify"
QRATOR_FT_URL = f"{BASE_URL}/web/2/ft"
QRATOR_PIXEL_URL = f"{BASE_URL}/web/1/u"
QRATOR_FAVICON_URL = f"{BASE_URL}/favicon.ico"

GEETEST_LOAD_URL = "https://gcaptcha4.geevisit.com/load"
GEETEST_BASE_URL = "https://gcaptcha4.geevisit.com"
GEETEST_CAPTCHA_ID = "2d9c743cf7d63dbc9db578a608196bcd"

GEEKED_TEST_ROOT = Path(__file__).resolve().parent / "GeekedTest"
if str(GEEKED_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(GEEKED_TEST_ROOT))


def get_browser_user_agent() -> str:
    """Возвращает актуальный заголовок User-Agent для Firefox 152."""
    if sys.platform.startswith("win"):
        platform_token = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10.15"
    else:
        platform_token = "X11; Linux x86_64"
    return f"Mozilla/5.0 ({platform_token}; rv:152.0) Gecko/20100101 Firefox/152.0"


# ═══════════════════════════════════════════════════════════════════════════
# 1. QRATOR PROTECTION
# ═══════════════════════════════════════════════════════════════════════════

def is_qrator_challenge_response(response: Any) -> bool:
    """Определяет, выдал ли QRATOR страницу с проверкой браузера."""
    if not hasattr(response, "status_code"):
        return False

    status = response.status_code
    headers = getattr(response, "headers", {}) or {}
    server = headers.get("server", "").strip().upper()
    text = getattr(response, "text", "") or ""

    has_meta_refresh = bool(
        re.search(r"<meta\b[^>]*\bhttp-equiv\s*=\s*[\"']refresh[\"']", text, re.IGNORECASE)
    )
    has_fingerprint_script = bool(
        re.search(r"<script\b[^>]*\bsrc\s*=\s*[\"']/[0-9a-f]{20}\.js[\"']", text, re.IGNORECASE)
    )

    if status in (302, 429) and (server == "QRATOR" or "qrator" in server.lower()):
        if has_meta_refresh or has_fingerprint_script:
            return True

    # Дополнительная проверка на случай нестандартных кодов или проксирования
    if (server == "QRATOR" or "qrator" in server.lower()) and has_fingerprint_script:
        return True

    return False


def extract_qrator_script_url(qrator_html: str, page_url: str) -> Optional[str]:
    """Извлекает абсолютный URL скрипта снятия отпечатка пальца Qrator."""
    script_sources = re.findall(
        r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']",
        qrator_html,
        re.IGNORECASE,
    )
    for source in script_sources:
        absolute_url = urljoin(page_url, source)
        parsed = urlsplit(absolute_url)
        if (
            parsed.netloc == urlsplit(BASE_URL).netloc
            and re.fullmatch(r"/[0-9a-f]{20}\.js", parsed.path)
        ):
            return absolute_url
    return None


def build_qrator_multipart(f_value: str, s_value: str) -> tuple[str, bytes]:
    """Формирует multipart body для запроса /web/2/ft точь-в-точь как JS бандл Qrator."""
    boundary = f"{secrets.randbelow(10**16):016d}"
    body = (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="f"'
        f"\r\n\r\n{f_value}"
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="s"'
        f"\r\n\r\n{s_value}"
        f"\r\n--{boundary}--\r\n"
    ).encode("ascii")
    return f'multipart/form-data;boundary="{boundary}"', body


def solve_qrator_challenge(
    session: requests.Session,
    response: Any,
    page_url: str,
    timeout: int = 15,
) -> bool:
    """
    Выполняет полный цикл подтверждения браузера перед QRATOR:
    1. Загружает JS-скрипт отпечатка и favicon
    2. Генерирует XTEA-зашифрованные поля 'f' и 's' на базе эталонных отпечатков
    3. Отправляет /web/2/ft и сохраняет куки 'f' и 'ft'
    4. Запрашивает проверочный пиксель /web/1/u
    """
    qrator_html = getattr(response, "text", "") or ""
    logger.info("🛡 Обнаружен челлендж QRATOR. Запуск протокола обхода...")
    sequence_start = time.monotonic()

    # 1. Загрузка fingerprint-скрипта
    script_url = extract_qrator_script_url(qrator_html, page_url)
    if script_url:
        try:
            session.get(
                script_url,
                headers={"Referer": page_url, "Accept": "*/*"},
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception as e:
            logger.debug(f"Ошибка загрузки Qrator JS скрипта: {e}")

    # 2. Загрузка favicon
    try:
        session.get(
            QRATOR_FAVICON_URL,
            headers={"Referer": page_url, "Accept": "image/*,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as e:
        logger.debug(f"Ошибка загрузки Qrator favicon: {e}")

    # Имитация времени выполнения JS в браузере (1 сек)
    elapsed = time.monotonic() - sequence_start
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    # 3. Генерация валидных полей 'f' и 's'
    try:
        f_base = load_f_base(DEFAULT_BASE_F)
        s_base = load_s_base(DEFAULT_BASE_S)
        variants = generate_variants(
            f_base,
            s_base,
            count=1,
            timestamp=int(time.time()),
        )
        f_value = variants[0]["f"]
        s_value = variants[0]["s"]
    except Exception as e:
        logger.error(f"Ошибка генерации отпечатка Qrator: {e}")
        return False

    # Устанавливаем куку f
    session.cookies.set("f", f_value, domain=".avito.ru", path="/")
    session.cookies.set("f", f_value, domain="www.avito.ru", path="/")

    # 4. Отправка multipart в /web/2/ft
    content_type, multipart_body = build_qrator_multipart(f_value, s_value)
    ft_headers = {
        "Accept": "*/*",
        "Origin": BASE_URL,
        "Referer": page_url,
        "Content-Type": content_type,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        ft_resp = session.post(
            QRATOR_FT_URL,
            data=multipart_body,
            headers=ft_headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if ft_resp.status_code != 200:
            logger.warning(f"Qrator /web/2/ft вернул статус {ft_resp.status_code}")
            return False

        # Qrator возвращает строку токена в JSON
        token = ft_resp.json()
        if not isinstance(token, str) or not token:
            logger.warning("Qrator /web/2/ft вернул пустой токен")
            return False

        # Важно: JS Qrator помещает в куку токен с обрамляющими кавычками
        ft_cookie_val = json.dumps(token, ensure_ascii=False, separators=(",", ":"))
        session.cookies.set("ft", ft_cookie_val, domain=".avito.ru", path="/")
        session.cookies.set("ft", ft_cookie_val, domain="www.avito.ru", path="/")

    except Exception as e:
        logger.error(f"Исключение при запросе к Qrator /web/2/ft: {e}")
        return False

    # 5. Запрос проверочного пикселя /web/1/u?{pixel_id}
    pixel_id = secrets.randbelow(0xFFFFFFFF)
    pixel_headers = {
        "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5",
        "Referer": page_url,
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        session.get(
            f"{QRATOR_PIXEL_URL}?{pixel_id}",
            headers=pixel_headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as e:
        logger.debug(f"Ошибка запроса пикселя Qrator: {e}")

    # Задержка после пикселя для синхронизации кэша защиты
    time.sleep(1.5)
    logger.info("✅ Защита QRATOR успешно пройдена, куки f и ft установлены!")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 2. FIREWALL PROOF-OF-WORK (PoW)
# ═══════════════════════════════════════════════════════════════════════════

def extract_pow_challenge(response: Any, session: requests.Session) -> Optional[str]:
    """
    Извлекает pow_challenge из:
    1. Тела ответа JSON (при HTTP 439 / 429)
    2. Кук ответа
    3. Кук сессии (только если ответ вернул статус блокировки 403, 429 или 439)
    """
    # 1. Из тела ответа JSON
    try:
        data = response.json()
        if isinstance(data, dict):
            ch = data.get("pow_challenge")
            if isinstance(ch, str) and ch:
                return ch
    except Exception:
        pass

    # 2. Из кук ответа
    if hasattr(response, "cookies") and response.cookies.get("pow_challenge"):
        return response.cookies.get("pow_challenge")

    # 3. Из кук сессии — ТОЛЬКО если код ответа сигнализирует о блоке или челлендже
    status = getattr(response, "status_code", None)
    if status in (403, 429, 439) and hasattr(session, "cookies") and session.cookies.get("pow_challenge"):
        return session.cookies.get("pow_challenge")

    return None


def solve_pow_challenge(
    session: requests.Session,
    pow_challenge: str,
    referer: str = "https://www.avito.ru/",
    timeout: int = 15,
) -> bool:
    """
    Решает криптографический Proof-of-Work (PoW) Авито:
    1. Запрашивает JWT задания на /web/3/firewallPow/get
    2. Декодирует сложность (compl) и ID задания из полезной нагрузки JWT
    3. Подбирает nonce, при котором sha256(f"{cid}:{nonce}") начинается с N нулей
    4. Отправляет решение на /web/3/firewallPow/verify
    5. Проверяет подтверждение и устанавливает куку pow_solved
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "Referer": referer,
            "Origin": BASE_URL,
            "Accept": "application/json, text/plain, */*",
        }

        # 1. Запрос challenge_jwt
        get_payload = build_get_payload(pow_challenge)
        get_resp = session.post(
            POW_GET_URL,
            json=get_payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if get_resp.status_code != 200:
            logger.error(f"firewallPow/get ответил статусом {get_resp.status_code}")
            return False

        get_json = get_resp.json()
        challenge_jwt = get_json.get("success", {}).get("result", {}).get("challenge_jwt")
        if not challenge_jwt:
            logger.error("firewallPow/get: challenge_jwt не найден в ответе")
            return False

        # 2. Вычисление SHA-256 PoW
        logger.info("🧩 firewallPow/get получен, вычисляем SHA-256 PoW...")
        verify_payload = build_verify_payload(challenge_jwt)

        # 3. Отправка решения на проверку
        verify_resp = session.post(
            POW_VERIFY_URL,
            json=verify_payload,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if verify_resp.status_code != 200:
            logger.error(f"firewallPow/verify ответил статусом {verify_resp.status_code}")
            return False

        verify_json = verify_resp.json()
        result = verify_json.get("success", {}).get("result", {})
        if result.get("verified") is True:
            unblock_ttl = result.get("unblock_ttl")
            session.cookies.set("pow_solved", "1", domain=".avito.ru", path="/")
            session.cookies.set("pow_solved", "1", domain="www.avito.ru", path="/")
            # Очищаем временный вызов pow_challenge, чтобы исключить ложные срабатывания
            try:
                session.cookies.pop("pow_challenge", None)
            except Exception:
                pass
            logger.info(f"✅ PoW успешно решен! (unblock_ttl: {unblock_ttl} сек)")
            return True
        else:
            logger.warning(f"PoW отклонен сервером: {verify_resp.text[:200]}")
            return False

    except Exception as e:
        logger.error(f"Исключение при решении PoW: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 3. GEETEST v4 CAPTCHA
# ═══════════════════════════════════════════════════════════════════════════

def is_geetest_challenge(response: Any) -> bool:
    """Определяет, требует ли ответ прохождения визуальной капчи GeeTest."""
    text = getattr(response, "text", "") or ""
    if "initGeetest4" in text and "gt4.js" in text and "captchaId" in text:
        return True

    if getattr(response, "status_code", 0) in (403, 429):
        try:
            data = response.json()
            if isinstance(data, dict):
                tmr = data.get("too-many-requests", {})
                if isinstance(tmr, dict) and "/firewall/captcha/show" in str(tmr.get("link", "")):
                    return True
        except Exception:
            pass

    return False


def solve_geetest_if_available(
    session: requests.Session,
    response: Any,
    page_url: str,
) -> bool:
    """
    Пытается решить GeeTest v4 локально с помощью модуля GeekedTest.
    Если ML-библиотеки (opencv, ddddocr, pycryptodome) не установлены,
    выводит понятное предупреждение в лог без падения бота.
    """
    geeked_root_str = str(GEEKED_TEST_ROOT)
    if geeked_root_str not in sys.path:
        sys.path.insert(0, geeked_root_str)

    try:
        from geeked import Geeked, CaptchaSolveRejected
    except ImportError as e:
        logger.warning(
            "⚠️ Авито выдал визуальную капчу GeeTest v4! "
            "Для автономного решения установите зависимости: "
            "pip install opencv-python numpy pycryptodome requests ddddocr. "
            f"(Причина отсутствия модуля: {e})"
        )
        return False

    try:
        text = getattr(response, "text", "") or ""
        m = re.search(r"const\s+captchaId\s*=\s*['\"]([^'\"]+)['\"]", text)
        captcha_id = m.group(1) if m else GEETEST_CAPTCHA_ID

        logger.info(f"🤖 Запуск автономного решателя GeeTest v4 (id: {captcha_id})...")

        # Получаем задачу от GeeTest
        challenge = str(secrets.token_hex(16))
        callback = f"geetest_{int(time.time() * 1000) + secrets.randbelow(10000)}"

        load_resp = session.get(
            GEETEST_LOAD_URL,
            params={
                "callback": callback,
                "captcha_id": captcha_id,
                "challenge": challenge,
                "client_type": "web",
                "lang": "rus",
            },
            headers={"Referer": BASE_URL + "/"},
            timeout=15,
        )

        match = re.fullmatch(rf"\s*{re.escape(callback)}\((.*)\)\s*;?\s*", load_resp.text, re.DOTALL)
        if not match:
            logger.warning("GeeTest load: некорректный JSONP ответ")
            return False

        load_data = json.loads(match.group(1))
        if load_data.get("status") != "success":
            return False

        task_data = load_data.get("data", {})
        solver = Geeked(
            captcha_id,
            lang="rus",
            session=session,
            request_headers={"Referer": BASE_URL + "/"},
        )
        solver.lot_number = task_data.get("lot_number")
        solver.base_url = GEETEST_BASE_URL

        seccode = solver.submit_captcha(task_data)
        if not isinstance(seccode, dict) or "lot_number" not in seccode:
            return False

        # Отправляем подтверждение на Авито
        cube_val = str(10 + secrets.randbelow(90))
        verify_payload = {
            "captcha": "",
            "hCaptchaResponse": "",
            **seccode,
        }
        verify_resp = session.post(
            FIREWALL_CAPTCHA_VERIFY_URL,
            json=verify_payload,
            headers={
                "Referer": page_url,
                "Origin": BASE_URL,
                "X-Cube": cube_val,
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        v_body = verify_resp.json()
        if v_body.get("success", {}).get("result", {}).get("verified") is True:
            logger.info("🎉 GeeTest v4 капча успешно решена и подтверждена Авито!")
            return True

        logger.warning("GeeTest: Авито отклонил решение капчи")
        return False

    except Exception as e:
        logger.error(f"Исключение при решении GeeTest: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 4. ОБЩИЙ ДИСПЕТЧЕР ЗАЩИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def handle_anti_bot_challenge(
    session: requests.Session,
    response: Any,
    page_url: str,
) -> bool:
    """
    Универсальный обработчик защиты Авито.
    Проверяет тип защиты (QRATOR, PoW 439, GeeTest) и решает её.
    Возвращает True, если защита успешно пройдена и можно повторить запрос.
    """
    # 1. QRATOR 302/429
    if is_qrator_challenge_response(response):
        return solve_qrator_challenge(session, response, page_url)

    # 2. Firewall PoW (439, 429 или в куках)
    pow_challenge = extract_pow_challenge(response, session)
    if pow_challenge:
        logger.info("🧩 Обнаружен челлендж Firewall PoW, запускаем решение...")
        return solve_pow_challenge(session, pow_challenge, referer=page_url)

    # 3. GeeTest v4 Captcha
    if is_geetest_challenge(response):
        return solve_geetest_if_available(session, response, page_url)

    return False

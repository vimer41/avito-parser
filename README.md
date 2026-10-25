# ⚡️ Avito Radar Bot — Высокоскоростной мониторинг объявлений Авито 24/7

**Avito Radar Bot** — профессиональный асинхронный Telegram-бот для отслеживания новых объявлений на Авито в реальном времени.

Построен на микросекундном парсинге встроенного SSR JSON, многоуровневой очереди доставки уведомлений (Outbox Pattern) и автономном комплексе обхода анти-бот систем (QRATOR, Firewall PoW, GeeTest v4).

---

## 🚀 Ключевые архитектурные особенности

* **⚡️ Сверхбыстрый SSR JSON движок:** извлекает структурированный каталог объявлений напрямую из состояния страницы Авито за < 5 миллисекунд без тяжелого разбора DOM-дерева BeautifulSoup.
* **🛡 Автономный модуль [anti_bot](file:///c:/Users/zahar/Рабочий%20стол/avito%20parser%20tg%20bot/anti_bot):**
  * **QRATOR Bypass**: чистая реализация блочного XTEA-шифра (32 раунда) для полей `f` и `s`, синтез реалистичных Canvas/Audio отпечатков браузера, handshake через `/web/2/ft` и пиксель `/web/1/u`.
  * **Firewall PoW Solver**: перехват челленджей из JSON-ответов (HTTP 439 / 429) и кук, моментальный подбор SHA-256 `nonce` на процессоре.
  * **GeeTest v4 Solver**: локальное распознавание координат слайдера и иконок капчи на базе встроенной ONNX-нейросети без внешних платных сервисов.
  * **TLS & HTTP/2 Impersonation**: согласованная работа под профилем `Firefox 147` в `curl_cffi` с аутентичным сетевым стеком.
* **📦 Умные статусы и мобильный API:**
  * Индикаторы: `🔒 В РЕЗЕРВЕ` (оформлена Авито Доставка) и `⚡️ ПРОДВИНУТО` (платное поднятие в поиске).
  * Интеграция с Mobile API `m.avito.ru/api/1/card/items/{id}` для мгновенной подгрузки просмотров и полного текста.
* **📬 Очередь доставки (Outbox Worker):** цикл проверки радаров и отправка сообщений в Telegram полностью изолированы через `asyncio.Queue`. Задержки Telegram API не замедляют сканирование.
* **🖼 Кэширование Telegram `file_id`:** при дублировании объявлений фото скачивается ровно 1 раз, а остальным пользователям рассылается мгновенно через кэшированный идентификатор Telegram.
* **🛡 Гибкие минус-слова и бан продавцов:** под каждым уведомлением есть кнопки быстрого добавления стоп-слов и отправки перекупов в бан в 1 клик.
* **👥 Многопользовательский режим:** система вайтлиста, заявки на доступ, тарифные планы (`standart` / `pro`) и панель администратора.

---

## 📋 Формат карточки в Telegram

```text
⚡️ НОВЫЙ ЛОТ НА АВИТО [🔒 В РЕЗЕРВЕ · ⚡️ ПРОДВИНУТО]
🏷 85 000 ₽ · Apple iPhone 15 Pro Max 256GB
────────────────────
📍 Локация: Санкт-Петербург, Невский пр-т
📦 Детали: ✨ Идеальное · 🚚 Авито Доставка
👁 Просмотры: 1 420 (+45 сегодня)
👤 Продавец: Иван · ⭐ 4.9 (48 отзывов)
🕒 Опубликовано: 2 мин. назад

📝 «Идеальное состояние, полный комплект, чек, на гарантии...»
────────────────────
🎯 Радар: «iPhone 15 Pro Max»

[ ↗️ Открыть на Авито ]
[ 🛡 Стоп-слово ]  [ 🚷 Забанить продавца ]
```

---

## 🛠 Технологический стек

* **Язык**: Python 3.11+
* **Telegram Framework**: [aiogram 3.x](https://github.com/aiogram/aiogram) (asyncio, роутеры, FSM)
* **HTTP Client**: [curl_cffi](https://github.com/lexiforest/curl_cffi) (TLS/JA3/HTTP2 фингерпринт браузера Firefox 147)
* **База данных**: SQLite через [aiosqlite](https://github.com/omnilib/aiosqlite) (WAL-режим, лимиты памяти в 2 МБ)
* **Machine Learning & CV**: `onnxruntime`, `opencv-python`, `ddddocr` (решение интерактивной капчи GeeTest)
* **Развертывание**: Docker, Docker Compose, systemd Linux

---

## 🚀 Быстрый старт

### 1. Клонирование и виртуальное окружение
```bash
git clone <URL_РЕПОЗИТОРИЯ>
cd "avito parser tg bot"

# Создание и активация venv
python -m venv venv

# Windows
.\venv\Scripts\activate

# Linux / macOS
source venv/bin/activate
```

### 2. Установка зависимостей
```bash
pip install -r requirements.txt

# Дополнительно для локального решения капчи GeeTest v4 (при необходимости):
pip install opencv-python numpy pycryptodome requests ddddocr
```

### 3. Конфигурация `.env`
Скопируйте пример настроек:
```bash
cp .env.example .env
```
Заполните переменные окружения:
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ  # Токен от @BotFather
ADMIN_ID=123456789                              # Ваш Telegram ID
CHECK_INTERVAL=60                               # Пауза между кругами сканирования (сек)
MAX_SEARCHES_PER_USER=10                        # Макс. радаров на пользователя
```

### 4. Запуск бота
```bash
python bot.py
```

---

## 🌐 Развертывание 24/7 (Docker / VPS)

### Через Docker Compose (Рекомендуется)
```bash
docker-compose up -d --build
docker-compose logs -f
```

### Через systemd сервис (Ubuntu / Debian)
Файл юнита уже подготовлен: [avito-bot.service](file:///c:/Users/zahar/Рабочий%20стол/avito%20parser%20tg%20bot/avito-bot.service).
```bash
sudo cp avito-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now avito-bot
sudo journalctl -u avito-bot -f
```

---

## 📁 Архитектура проекта

```text
├── bot.py                     # Точка входа, запуск бота, диспетчера и фоновых служб
├── config.py                  # Конфигурация проекта из .env
├── database.py                # Асинхронный слой SQLite (пользователи, радары, история, бан-листы)
├── parser.py                  # Парсер Авито (SSR JSON, mobile API, интеграция с anti_bot)
├── scheduler.py               # Супервизор мониторинга радаров и Outbox Queue воркер
├── middlewares.py             # Контроль доступа (вайтлист) и аудит действий пользователей
│
├── anti_bot/                  # Автономный комплекс обхода защит Авито
│   ├── manager.py             # Главный диспетчер (маршрутизация QRATOR, PoW, GeeTest)
│   ├── qrator_payload_codec.py# Алгоритм блочного шифрования XTEA (32 раунда)
│   ├── generate_qrator_variants.py # Генератор синтетических браузерных отпечатков
│   ├── firewallpow_payload_reference.py # Решатель Proof-of-Work (SHA-256 + JWT)
│   ├── base.txt / base-s.json # Эталонные матрицы характеристик устройств
│   └── GeekedTest/            # Локальный ONNX-решатель GeeTest v4 (слайдер/иконки)
│
├── handlers/                  # Telegram роутеры
│   ├── common.py              # Старт, помощь, регистрация и заявки в вайтлист
│   ├── searches.py            # Создание радаров, управление стоп-словами, фильтрами
│   ├── profile.py             # Личный кабинет, статус подписки
│   └── admin.py               # Админ-панель (одобрение пользователей, статистика)
│
├── Dockerfile                 # Контейнеризация приложения
├── docker-compose.yml         # Сервис Docker Compose
└── requirements.txt           # Зависимости Python
```

---

## ⚖️ Дисклеймер
Проект создан исключительно в ознакомительных и исследовательских целях. Разработчик не несет ответственности за любое использование скрипта, нарушающее правила и условия использования сторонних сервисов.


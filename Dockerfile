FROM python:3.11-slim

# Установка зависимостей ОС для curl_cffi и сборки
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Установка python-зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# Создание папки для базы данных и логов
RUN mkdir -p data

# Запуск бота
CMD ["python", "bot.py"]

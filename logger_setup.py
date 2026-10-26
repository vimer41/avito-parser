import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Базовая директория логов
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(Path(__file__).resolve().parent / "logs")))

USER_ACTIONS_LOG = LOGS_DIR / "user_actions.log"
PARSER_LOG = LOGS_DIR / "parser.log"
ERRORS_LOG = LOGS_DIR / "errors.log"
COMBINED_LOG = Path(os.getenv("COMBINED_LOG", str(Path(__file__).resolve().parent / "bot.log")))

# Логгер для действий пользователей
user_logger = logging.getLogger("user_action")


def setup_logging():
    """
    Инициализирует раздельное многофайловое логирование:
    1. logs/user_actions.log — каждое действие пользователя (кнопки, ссылки, FSM, ошибки пользователя)
    2. logs/parser.log — сетевая активность парсера, мониторинг, обход защит
    3. logs/errors.log — все ошибки и предупреждения (WARNING, ERROR, CRITICAL) со всего проекта
    4. bot.log — общий консольный лог (для обратной совместимости и docker logs)
    5. sys.stdout — вывод в терминал
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    COMBINED_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Повторная инициализация не должна оставлять открытые файлы и дублировать записи.
    for name in ("", "user_action", "parser", "scheduler", "anti_bot", "anti_bot.manager"):
        current_logger = logging.getLogger(name)
        for handler in list(current_logger.handlers):
            current_logger.removeHandler(handler)
            handler.close()

    # Общий форматтер
    std_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    # Форматтер для логов пользователей (читаемый таймлайн)
    user_formatter = logging.Formatter(
        fmt="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Очищаем существующие обработчики
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # Проверяем, не перенаправлен ли stdout напрямую в bot.log (например, systemd StandardOutput=append:.../bot.log)
    is_stdout_same_as_combined = False
    try:
        if COMBINED_LOG.exists() and hasattr(sys.stdout, "fileno"):
            stdout_stat = os.fstat(sys.stdout.fileno())
            log_stat = COMBINED_LOG.stat()
            if stdout_stat.st_ino == log_stat.st_ino and stdout_stat.st_dev == log_stat.st_dev:
                is_stdout_same_as_combined = True
    except Exception:
        pass

    # 1. Вывод в консоль (stdout) — только если stdout не дублирует bot.log
    if not is_stdout_same_as_combined:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(std_formatter)
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)

    # 2. Общий файл bot.log (все события)
    combined_handler = RotatingFileHandler(
        COMBINED_LOG,
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8"
    )
    combined_handler.setFormatter(std_formatter)
    combined_handler.setLevel(logging.INFO)
    root_logger.addHandler(combined_handler)

    # 3. Файл только для ошибок (logs/errors.log)
    errors_handler = RotatingFileHandler(
        ERRORS_LOG,
        maxBytes=3 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8"
    )
    errors_handler.setFormatter(std_formatter)
    errors_handler.setLevel(logging.WARNING)
    root_logger.addHandler(errors_handler)

    # 4. Файл действий пользователей (logs/user_actions.log)
    user_actions_handler = RotatingFileHandler(
        USER_ACTIONS_LOG,
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8"
    )
    user_actions_handler.setFormatter(user_formatter)
    user_actions_handler.setLevel(logging.INFO)

    user_logger.setLevel(logging.INFO)
    # Не дублируем логи юзеров в общий root_logger дважды, но отправляем в user_actions.log
    user_logger.propagate = True
    user_logger.addHandler(user_actions_handler)

    # 5. Файл мониторинга и парсера (logs/parser.log)
    parser_handler = RotatingFileHandler(
        PARSER_LOG,
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8"
    )
    parser_handler.setFormatter(std_formatter)
    parser_handler.setLevel(logging.INFO)

    for logger_name in ("parser", "scheduler", "anti_bot"):
        pl = logging.getLogger(logger_name)
        pl.addHandler(parser_handler)

    # Подавляем шумные технические логи aiogram
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)


def log_user_event(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    action: str,
    details: str = "",
    status: str = "OK",
    fsm_state: Optional[str] = None
):
    """
    Записывает структурированное событие пользователя в logs/user_actions.log.
    Удобно фильтровать по user_id при обращении в поддержку.
    """
    uname = f"@{username}" if username else "no_username"
    fname = first_name or ""
    fsm_info = f" | [FSM: {fsm_state}]" if fsm_state else ""
    det_info = f" ── {details}" if details else ""

    # Символы статусов для быстрой визуальной навигации
    icons = {
        "OK": "✅",
        "WARN": "⚠️",
        "ERROR": "❌",
        "CLICK": "🔘",
        "MSG": "💬",
        "INFO": "ℹ️"
    }
    icon = icons.get(status, "🔹")

    log_line = (
        f"[{icon} {status}] [ID:{user_id} {uname} ({fname})]{fsm_info} "
        f"{action}{det_info}"
    )

    if status in ("ERROR", "FAIL"):
        user_logger.error(log_line)
    elif status == "WARN":
        user_logger.warning(log_line)
    else:
        user_logger.info(log_line)


def sanitize_logs_for_user(pattern: str):
    """
    Удаляет строки с упоминанием пользователя из всех файлов логов.
    """
    clean = pattern.strip().lstrip("@").lower()
    if not clean or len(clean) < 3:
        return
    log_files = [
        USER_ACTIONS_LOG,
        ERRORS_LOG,
        PARSER_LOG,
        COMBINED_LOG,
    ]
    for lf in log_files:
        if lf.exists() and lf.is_file():
            try:
                with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                filtered = [l for l in lines if clean not in l.lower()]
                if len(filtered) != len(lines):
                    with open(lf, "w", encoding="utf-8") as f:
                        f.writelines(filtered)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Не удалось очистить лог {lf}: {e}")


def cleanup_logs(max_total_mb: int = 40, keep_days: int = 3) -> int:
    """
    Автоматическая очистка старых ротированных логов (*.log.1, *.log.2).
    Удаляет файлы старше keep_days или самые старые ротации при превышении max_total_mb.
    """
    import time
    removed_count = 0
    now = time.time()
    cutoff = now - (keep_days * 86400)

    target_dirs = {LOGS_DIR, COMBINED_LOG.parent}
    log_files: list[Path] = []

    for d in target_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.log.*"):
            if p.is_file():
                log_files.append(p)

    # 1. Удаление ротированных архивов логов старше keep_days
    for p in list(log_files):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                log_files.remove(p)
                removed_count += 1
        except Exception:
            pass

    # 2. Если общий объем оставшихся ротаций превышает max_total_mb — удаляем самые старые
    try:
        total_bytes = sum(p.stat().st_size for p in log_files if p.is_file())
        max_bytes = max_total_mb * 1024 * 1024
        if total_bytes > max_bytes:
            log_files.sort(key=lambda f: f.stat().st_mtime)
            for p in log_files:
                if total_bytes <= max_bytes:
                    break
                try:
                    sz = p.stat().st_size
                    p.unlink(missing_ok=True)
                    total_bytes -= sz
                    removed_count += 1
                except Exception:
                    pass
    except Exception:
        pass

    return removed_count


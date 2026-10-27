"""Publication-age parsing without treating an unknown date as either new or old."""

import datetime
import math
import re
import time
from typing import Optional

MSK_TZ = datetime.timezone(datetime.timedelta(hours=3))


def normalize_timestamp(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        if number >= 1_000_000_000_000:
            number /= 1000
        return int(number)
    except (ValueError, TypeError, OverflowError):
        return None


def publication_age_seconds(date_str: Optional[str], timestamp=None, *, now: Optional[float] = None) -> Optional[float]:
    now = time.time() if now is None else now
    published = normalize_timestamp(timestamp)
    if published is not None:
        # A large future timestamp is bad metadata, not proof of freshness.
        return max(0.0, now - published) if published <= now + 90 else None
    text = re.sub(r"\s+", " ", (date_str if isinstance(date_str, str) else "").lower().replace("\xa0", " ")).strip()
    if text in {"только что", "сейчас", "несколько секунд назад"}:
        return 0.0
    singular = {"секунду назад": 1, "минуту назад": 60, "час назад": 3600, "полчаса назад": 1800}
    if text in singular:
        return float(singular[text])
    relative = re.fullmatch(
        r"(\d+)\s*(сек(?:унд(?:а|ы|у)?)?\.?|с\.?|мин(?:ут(?:а|ы|у)?)?\.?|"
        r"час(?:а|ов)?|ч\.?|день|дня|дней|дн\.?|д\.?)\s*(?:назад)?", text,
    )
    if relative:
        count, unit = int(relative[1]), relative[2]
        multiplier = 1 if unit.startswith(("сек", "с")) else 60 if unit.startswith("мин") else 3600 if unit.startswith(("час", "ч")) else 86400
        return float(count * multiplier)
    clock = re.fullmatch(r"(сегодня|вчера)\s*(?:,\s*|в\s*)?(\d{1,2}):(\d{2})", text)
    if clock:
        local_now = datetime.datetime.fromtimestamp(now, MSK_TZ)
        try:
            published_dt = local_now.replace(hour=int(clock[2]), minute=int(clock[3]), second=0, microsecond=0)
        except ValueError:
            return None
        if clock[1] == "вчера":
            published_dt -= datetime.timedelta(days=1)
        age = now - published_dt.timestamp()
        return max(0.0, age) if age >= -90 else None
    if text == "вчера":
        return 86400.0
    # Full absolute dates do not depend on the server's local timezone.
    for date_format in ("%d.%m.%Y %H:%M", "%d.%m.%Y в %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            published_dt = datetime.datetime.strptime(text, date_format).replace(tzinfo=MSK_TZ)
        except ValueError:
            continue
        age = now - published_dt.timestamp()
        return max(0.0, age) if age >= -90 else None
    return None


def freshness_status(date_str: Optional[str], timestamp=None, max_age_minutes: Optional[int] = 5, *, now: Optional[float] = None) -> Optional[bool]:
    """True = fresh, False = known old, None = not enough publication metadata."""
    age = publication_age_seconds(date_str, timestamp, now=now)
    if age is None:
        return None
    limit = int(max_age_minutes or 5)
    limit = min(30, limit if limit > 0 else 5)
    return age <= limit * 60
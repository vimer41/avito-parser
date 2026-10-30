"""
Debug report from logs/events.jsonl and logs/metrics.jsonl: one compact text file that holds
what is needed to diagnose the bot without reading raw logs.

    python log_report.py [--minutes 60]     the report (the same as the button in the admin panel)
    python log_report.py --ready 24         can the bot be sold: a verdict with its evidence
    python log_report.py --trace a1f3       everything that happened to one scan
    python log_report.py --radar 12         recent events of a radar
    python log_report.py --chat 986832959   recent deliveries to a chat
    python log_report.py --grep "catalog"   any text in the events
"""

import argparse
import gzip
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Optional

from logger_setup import CRASH_LOG, EVENTS_LOG, METRICS_LOG

MAX_LINES = 400000
ERROR_GROUPS = 15
SAMPLE_TRACEBACK_CHARS = 1500
TAIL_EVENTS = 25
BUCKET_MINUTES = 5
# Above this age of the worst link, or at this share of failed scans, a minute counts as a work outage.
OUTAGE_COVERAGE_S = 180
OUTAGE_FAIL_SHARE = 0.1
COVERED_S = 120  # a link is covered when its last good scan is younger than this
LONG_STALL_MS = 5000
SAFE_RPS_PER_IP = 0.5
GOOD_CHANNEL_OK = 0.7
ENOUGH_LOTS = 20


def _files_oldest_first(path: Path, since: float) -> list[Path]:
    rotated = sorted(path.parent.glob(f"{path.name}.*"), key=lambda p: p.stat().st_mtime)
    files = [p for p in rotated if p.stat().st_mtime >= since]
    return files + ([path] if path.is_file() else [])


def _read_lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as source:
        yield from source


def _epoch_prefix(line: str) -> Optional[float]:
    # Rows start with {"t":<epoch>, so old lines are skipped without parsing the JSON.
    try:
        return float(line[5 : line.index(",", 5, 24)])
    except ValueError:
        return None


def read_rows(path: Path, since: float) -> list[dict]:
    rows: list[dict] = []
    for file in _files_oldest_first(path, since):
        for index, line in enumerate(_read_lines(file)):
            if index % 2000 == 0:
                time.sleep(0)  # gives the event loop the GIL when run from the bot
            stamp = _epoch_prefix(line)
            if stamp is None or stamp < since:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a line cut by a crash
            if len(rows) >= MAX_LINES:
                return rows
    return rows


def _sum(rows: Iterable[dict], *path: str) -> int:
    total = 0
    for row in rows:
        value = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        total += value or 0
    return total


def _pct(part: float, total: float) -> str:
    return f"{100 * part / total:.0f}%" if total else "—"


def _top(counter: dict, limit: int = 4) -> str:
    return ", ".join(f"{key}×{count}" for key, count in Counter(counter).most_common(limit)) or "—"


def _merge(rows: Iterable[dict], key: str) -> Counter:
    merged: Counter = Counter()
    for row in rows:
        merged.update(row.get(key) or {})
    return merged


def _values(rows: Iterable[dict], *path: str) -> list[float]:
    found = []
    for row in rows:
        value = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            found.append(value)
    return found


def _worst(rows: list[dict], *path: str) -> str:
    values = _values(rows, *path)
    return f"{max(values):g}" if values else "—"


def _median(rows: list[dict], *path: str) -> str:
    values = sorted(_values(rows, *path))
    return f"{values[len(values) // 2]:g}" if values else "—"


def _channels(metrics: list[dict]) -> dict[str, dict]:
    merged: dict[str, dict] = defaultdict(lambda: {"fail": Counter()})
    for row in metrics:
        for name, stats in (row.get("ch") or {}).items():
            target = merged[name]
            for key in ("http", "ok", "403", "429", "5xx", "terr", "coll", "coll_ok"):
                target[key] = target.get(key, 0) + (stats.get(key) or 0)
            target["fail"].update(stats.get("fail") or {})
            target["state"], target["rate"] = stats.get("state"), stats.get("rate")
            target.setdefault("ms", []).extend([stats["ms_p50"]] if stats.get("ms_p50") is not None else [])
    return merged


def channel_history(name: str, hours: int = 24) -> Optional[dict]:
    """One channel's totals from the minute rows; they survive restarts, unlike METRICS."""
    rows = read_rows(METRICS_LOG, time.time() - hours * 3600)
    stats = _channels(rows).get(name)
    if stats is None:
        return None
    minutes = [row["ch"][name] for row in rows if name in (row.get("ch") or {})]
    stats["minutes"] = len(minutes)
    stats["paused"] = sum(1 for minute in minutes if minute.get("state") == "quarantined")
    return stats


def _outages(metrics: list[dict]) -> list[list[dict]]:
    """Runs of 2+ minutes when scans failed or some link went unchecked; an hourly average hides them."""
    runs: list[list[dict]] = []
    current: list[dict] = []
    for row in metrics:
        ok, failed = _sum([row], "scans", "ok"), _sum([row], "scans", "fail")
        if (ok + failed and failed / (ok + failed) >= OUTAGE_FAIL_SHARE) or (row.get("coverage_s") or 0) > OUTAGE_COVERAGE_S:
            current.append(row)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return [run for run in runs if len(run) >= 2]


def findings(metrics: list[dict], events: list[dict]) -> list[str]:
    """What stands out, so the reader starts with the likely cause and not with the tables."""
    if not metrics:
        return ["Нет минутных метрик за период: бот только что запущен или мониторинг не работал."]
    notes = []
    ok, failed = _sum(metrics, "scans", "ok"), _sum(metrics, "scans", "fail")
    if ok + failed == 0:
        notes.append("Сканов нет вообще: проверьте режим сети и список каналов.")
    elif failed / (ok + failed) >= 0.5:
        notes.append(f"Падает больше половины сканов ({_pct(failed, ok + failed)}), главные причины: {_top(_merge(metrics, 'fail'), 3)}.")
    for name, stats in _channels(metrics).items():
        if stats.get("http", 0) >= 20:
            raw_ok, coll = stats["ok"] / stats["http"], stats.get("coll_ok", 0) / max(stats.get("coll", 0), 1)
            if raw_ok - coll >= 0.3:
                notes.append(f"{name}: HTTP ok {raw_ok:.0%}, но каталог получен лишь в {coll:.0%} сканов: ответы приходят без каталога.")
            if stats["403"] / stats["http"] >= 0.5:
                notes.append(f"{name}: {_pct(stats['403'], stats['http'])} ответов 403, канал почти бесполезен.")
    stalls = _sum(metrics, "stalls")
    if stalls:
        notes.append(f"Event loop зависал {stalls} раз, максимум {_worst(metrics, 'loop_max_ms')} мс: что-то блокирует все корутины.")
    flood = _sum(metrics, "flood")
    if flood:
        notes.append(f"Telegram flood-wait {flood} раз, максимум {_worst(metrics, 'flood_max_s')} с.")
    deficits = [row for row in metrics if (row.get("rps_need") or 0) > (row.get("rps_have") or 0)]
    if len(deficits) >= len(metrics) / 2:
        notes.append(f"Мощности не хватает {len(deficits)} мин из {len(metrics)}: нужно {_median(metrics, 'rps_need')} req/s, есть {_median(metrics, 'rps_have')}.")
    drops = max(_values(metrics, "log_drops") or [0])
    if drops:
        notes.append(f"Потеряно {drops} записей лога: очередь логов переполнялась.")
    starts = [row for row in events if row.get("ev") == "app.start"]
    if len(starts) > 1:
        notes.append(f"Бот перезапускался {len(starts)} раз за период.")
    outages = _outages(metrics)
    if outages:
        worst = max(outages, key=len)
        notes.append(f"Провалов работы: {len(outages)}, всего {sum(map(len, outages))} мин, самый долгий {len(worst)} мин с {worst[0].get('ts')}.")
    if not notes:
        notes.append("Явных проблем не видно.")
    return notes


def _restarts(events: list[dict]) -> list[str]:
    lines = []
    previous_stop = True
    for row in events:
        if row.get("ev") == "app.stop":
            previous_stop = True
        elif row.get("ev") == "app.start":
            fields = row.get("f") or {}
            state = "" if previous_stop else " · ПРЕДЫДУЩИЙ ЗАПУСК УПАЛ (нет app.stop)"
            lines.append(f"{row.get('ts')} версия {fields.get('version')} · сеть {fields.get('net_mode')}{state}")
            previous_stop = False
    return lines


def _timeline(metrics: list[dict], bucket: int = BUCKET_MINUTES) -> list[str]:
    lines = ["время  сканы ok/всего  причины  лоты→очередь  отпр  pub→sent p50  опозд p95  rps нужно/есть  outbox  loop мс  rss"]
    for start in range(0, len(metrics), bucket):
        chunk = metrics[start : start + bucket]
        ok, failed = _sum(chunk, "scans", "ok"), _sum(chunk, "scans", "fail")
        lines.append(
            f"{chunk[0].get('ts', '')[-5:]}  {ok}/{ok + failed} ({_pct(ok, ok + failed)})  {_top(_merge(chunk, 'fail'), 3)}  "
            f"{_sum(chunk, 'lots', 'new')}→{_sum(chunk, 'lots', 'queued')}  {_sum(chunk, 'sent')}  "
            f"{_median(chunk, 'pub_sent_s', 'p50')}  {_worst(chunk, 'due_s', 'p95')}  "
            f"{_median(chunk, 'rps_need')}/{_median(chunk, 'rps_have')}  {_worst(chunk, 'outbox')}  "
            f"{_worst(chunk, 'loop_max_ms')}  {_worst(chunk, 'rss_mb')}"
        )
    return lines


def _channel_table(metrics: list[dict]) -> list[str]:
    lines = ["канал: http · ok · 403 · 429 · 5xx · транспорт · каталог ok/сканы · латентность p50 мс · состояние · rate · причины"]
    for name, stats in sorted(_channels(metrics).items()):
        http = stats.get("http", 0)
        latency = sorted(stats.get("ms") or [0])
        lines.append(
            f"{name}: {http} · {_pct(stats.get('ok', 0), http)} · {_pct(stats.get('403', 0), http)} · {stats.get('429', 0)} · "
            f"{stats.get('5xx', 0)} · {stats.get('terr', 0)} · {stats.get('coll_ok', 0)}/{stats.get('coll', 0)} "
            f"({_pct(stats.get('coll_ok', 0), stats.get('coll', 0))}) · {latency[len(latency) // 2]} · {stats.get('state')} · "
            f"{stats.get('rate')} · {_top(stats['fail'], 3)}"
        )
    return lines


def _failure_details(events: list[dict]) -> list[str]:
    details: Counter = Counter()
    for row in events:
        if row.get("ev") == "scan.fail":
            fields = row.get("f") or {}
            details[f"{fields.get('kind')}: {re.sub(r'[0-9]+ КБ', 'N КБ', fields.get('detail') or 'без деталей')}"] += 1
    return [f"{count}× {text}" for text, count in details.most_common(12)]


def _worst_sources(events: list[dict]) -> list[str]:
    failures: dict[str, Counter] = defaultdict(Counter)
    for row in events:
        if row.get("ev") == "scan.fail":
            fields = row.get("f") or {}
            failures[str(fields.get("src"))][fields.get("kind")] += 1
    ranked = sorted(failures.items(), key=lambda pair: -sum(pair[1].values()))[:10]
    return [f"{sum(kinds.values())}× «{source}»: {_top(kinds, 3)}" for source, kinds in ranked]


def _challenges(metrics: list[dict]) -> list[str]:
    merged: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
    for row in metrics:
        for kind, stats in (row.get("challenge") or {}).items():
            attempts = stats["ok"] + stats["fail"]
            merged[kind][0] += stats["ok"]
            merged[kind][1] += stats["fail"]
            merged[kind][2] += stats["avg_s"] * attempts
    return [
        f"{kind}: решено {int(ok)}/{int(ok + failed)} ({_pct(ok, ok + failed)}), среднее {seconds / max(ok + failed, 1):.1f} с"
        for kind, (ok, failed, seconds) in sorted(merged.items())
    ]


def _funnel(metrics: list[dict]) -> str:
    lots = _merge((row for row in metrics), "lots")
    return (
        f"новых {lots['new']} → в очередь {lots['queued']} · отсеяно: возраст {lots['old']}, стоп-слова {lots['stop']}, "
        f"продавцы {lots['seller']}, название {lots['title']} · отправлено {_sum(metrics, 'sent')}"
    )


def _error_groups(events: list[dict]) -> list[str]:
    groups: dict[str, dict] = {}
    for row in events:
        if row.get("l") not in ("W", "E", "C"):
            continue
        text = str(row.get("msg") or row.get("ev") or "")
        key = re.sub(r"\d+", "N", text)[:90]
        group = groups.setdefault(key, {"count": 0, "first": row.get("ts"), "level": row.get("l"), "row": row})
        group["count"] += 1
        group["last"] = row.get("ts")
        if row.get("exc"):
            group["row"] = row
    ranked = sorted(groups.values(), key=lambda g: ("CEW".index(g["level"]), -g["count"]))[:ERROR_GROUPS]
    lines = []
    for group in ranked:
        row = group["row"]
        lines.append(f"[{group['level']}] {group['count']}× {group['first']} … {group['last']} · {row.get('c')} · {str(row.get('msg') or row.get('ev'))[:300]}")
        if row.get("exc"):
            lines.append("    " + row["exc"][-SAMPLE_TRACEBACK_CHARS:].replace("\n", "\n    "))
    return lines


def _compact(row: dict) -> str:
    fields = row.get("f")
    text = row.get("msg") or ""
    tail = json.dumps(fields, ensure_ascii=False) if fields else ""
    return f"{row.get('ts')} {row.get('l')} {row.get('c')} {row.get('id', '····')} {row.get('ev', '')} {text} {tail}".rstrip()


def _crash_tail() -> list[str]:
    if not CRASH_LOG.is_file() or CRASH_LOG.stat().st_size == 0:
        return []
    return CRASH_LOG.read_text(encoding="utf-8", errors="replace")[-6000:].splitlines()


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", *(lines or ["нет данных"]), ""]


def build_report(minutes: int = 60, live_state: Optional[dict] = None) -> str:
    now = time.time()
    since = now - minutes * 60
    events = read_rows(EVENTS_LOG, since)
    metrics = read_rows(METRICS_LOG, since)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3 * 3600))
    lines = [f"# Avito Radar: отчёт за {minutes} мин, {stamp} МСК", f"Минутных строк: {len(metrics)}, событий: {len(events)}.", ""]
    lines += _section("Главное", [f"- {note}" for note in findings(metrics, events)])
    lines += _section("Запуски", _restarts(events))
    if live_state:
        lines += _section("Состояние сейчас", [json.dumps(live_state, ensure_ascii=False, default=str)])
    lines += _section(f"Динамика по {BUCKET_MINUTES} мин", _timeline(metrics))
    lines += _section("Каналы", _channel_table(metrics))
    lines += _section("Причины провалов сканов", _failure_details(events))
    lines += _section("Худшие источники", _worst_sources(events))
    lines += _section("Антибот", _challenges(metrics))
    lines += _section("Воронка лотов", [_funnel(metrics)])
    lines += _section("Ошибки и предупреждения", _error_groups(events))
    crash = _crash_tail()
    if crash:
        lines += _section("crash.log", crash)
    lines += _section(
        f"Последние {TAIL_EVENTS} важных событий",
        [_compact(row) for row in [r for r in events if r.get("l") in ("W", "E", "C")][-TAIL_EVENTS:]],
    )
    return "\n".join(lines)


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share))] if ordered else 0.0


def _grade(value: float, good: float, fair: float) -> str:
    """Higher is better when good > fair, lower is better otherwise."""
    if good >= fair:
        return "✅" if value >= good else "⚠️" if value >= fair else "❌"
    return "✅" if value <= good else "⚠️" if value <= fair else "❌"


def _lots(events: list[dict]) -> list[dict]:
    return [row.get("f") or {} for row in events if row.get("ev") == "lot.new"]


def _bot_waits(lots: list[dict]) -> list[float]:
    """How long a lot already visible on Avito waited for the bot's next look."""
    return [min(lot["gap"], lot["age"]) for lot in lots if lot.get("fresh") and not lot.get("promo")]


def _safe_capacity_share(metrics: list[dict]) -> float:
    """Share of minutes when proven proxies alone cover the load at the safe per-IP rate, with a 20 % reserve."""
    covered = total = 0
    for row in metrics:
        need = row.get("rps_need")
        if not need:
            continue
        total += 1
        good = sum(
            1 for name, stats in (row.get("ch") or {}).items()
            if name != "direct" and stats.get("http", 0) >= 3 and stats.get("ok", 0) / stats["http"] >= GOOD_CHANNEL_OK
        )
        covered += good * SAFE_RPS_PER_IP >= need * 1.2
    return covered / total if total else 0.0


def readiness_checks(metrics: list[dict], events: list[dict], hours: int) -> list[str]:
    """Each line starts with ✅ ⚠️ ❌ or ⏳ (not enough data). Avito's own delay is not graded: the bot cannot change it."""
    checks = []

    def add(mark: str, name: str, value: str, norm: str) -> None:
        checks.append(f"{mark} {name}: {value} (норма: {norm})")

    seen = len(metrics) / (hours * 60)
    crashes = sum("УПАЛ" in line for line in _restarts(events))
    mark = "⏳" if seen < 0.8 else "❌" if crashes else _grade(seen, 0.99, 0.95)
    add(mark, "Работа без перерывов", f"{seen:.1%} времени, падений {crashes}", "99 % и 0 падений")

    coverage = [row["coverage_s"] for row in metrics if row.get("coverage_s") is not None]
    if coverage:
        share, worst = sum(1 for age in coverage if age <= COVERED_S) / len(coverage), max(coverage)
        mark = _grade(share, 0.99, 0.95)
        add("⚠️" if mark == "✅" and worst > 300 else mark, "Все ссылки под присмотром",
            f"{share:.1%} минут, худшая слепая зона {worst:.0f} с", f"99 % минут за {COVERED_S} с, зона до 300 с")
    else:
        add("⏳", "Все ссылки под присмотром", "нет данных", "99 % минут")

    ok, failed = _sum(metrics, "scans", "ok"), _sum(metrics, "scans", "fail")
    add(_grade(ok / (ok + failed), 0.97, 0.93) if ok + failed else "⏳", "Успешные сканы", f"{_pct(ok, ok + failed)} из {ok + failed}", "97 %")

    waits = _bot_waits(_lots(events))
    value = f"p95 {_percentile(waits, 0.95):.0f} с по {len(waits)} лотам"
    add(_grade(_percentile(waits, 0.95), 45, 90) if len(waits) >= ENOUGH_LOTS else "⏳", "Бот замечает лот", value, f"p95 до 45 с, от {ENOUGH_LOTS} лотов")

    sent, lost = _sum(metrics, "sent"), _sum(metrics, "lost", "expired") + _sum(metrics, "lost", "failed")
    flood = _sum(metrics, "flood")
    mark = _grade(lost / (sent + lost), 0.005, 0.02) if sent + lost else "⏳"
    add("⚠️" if mark == "✅" and flood else mark, "Доставка", f"отправлено {sent}, потеряно {lost}, flood-wait {flood}", "потери до 0,5 %, без flood-wait")

    long_stalls = sum(1 for row in metrics if (row.get("loop_max_ms") or 0) >= LONG_STALL_MS)
    add(_grade(long_stalls, 0, 2), "Зависания бота", f"{long_stalls} минут с паузой от {LONG_STALL_MS // 1000} с", "0")

    capacity = _safe_capacity_share(metrics)
    add(_grade(capacity, 0.95, 0.8) if metrics else "⏳", "Запас хороших прокси", f"хватает {capacity:.0%} минут",
        f"95 % минут при {SAFE_RPS_PER_IP} запр/с на IP и запасе 20 %")
    return checks


def readiness_verdict(checks: list[str]) -> str:
    marks = {line.split(" ", 1)[0] for line in checks}
    if "❌" in marks:
        return "🔴 Продавать рано: есть проваленные пункты."
    if "⏳" in marks:
        return "🟡 Данных мало: вердикт будет точным после суток работы и 20+ лотов."
    if "⚠️" in marks:
        return "🟡 Можно продавать осторожно: есть пункты на грани."
    return "🟢 Можно продавать: все пункты в норме."


def _lot_timing(events: list[dict]) -> list[str]:
    lots = _lots(events)
    if not lots:
        return []
    organic = [lot for lot in lots if not lot.get("promo")]
    fresh = [lot for lot in organic if lot.get("fresh")]
    old = [lot for lot in organic if not lot.get("fresh")]

    def split(group: list[dict], title: str) -> list[str]:
        if not group:
            return []
        ages = [lot["age"] for lot in group]
        bot = [min(lot["gap"], lot["age"]) for lot in group]
        hidden = [lot["age"] - min(lot["gap"], lot["age"]) for lot in group]
        return [
            f"{title} ({len(group)}): от публикации до бота p50 {_percentile(ages, 0.5):.0f} с, p95 {_percentile(ages, 0.95):.0f} с",
            f"  Авито не показывал: p50 {_percentile(hidden, 0.5):.0f} с, p95 {_percentile(hidden, 0.95):.0f} с",
            f"  бот не смотрел: p50 {_percentile(bot, 0.5):.0f} с, p95 {_percentile(bot, 0.95):.0f} с",
        ]

    by_radar = Counter(lot.get("rid") for lot in old)
    return [
        f"Новых лотов с точным временем: {len(lots)}, реклама {len(lots) - len(organic)}.",
        *split(fresh, "Свежие"),
        *split(old, "Старше лимита радара"),
        *([f"Старые по радарам: {_top(by_radar, 5)}"] if by_radar else []),
    ]


def _channel_load(metrics: list[dict]) -> list[str]:
    lines = ["канал: запр/мин · ok · 403/429 · обрывы · каталог ok · на паузе"]
    for name, stats in sorted(_channels(metrics).items()):
        minutes = [row["ch"][name] for row in metrics if name in (row.get("ch") or {})]
        http = stats.get("http", 0)
        blocks = stats.get("403", 0) + stats.get("429", 0)
        paused = sum(1 for minute in minutes if minute.get("state") == "quarantined")
        lines.append(
            f"{name}: {http / max(len(minutes), 1):.1f} · {_pct(stats.get('ok', 0), http)} · {_pct(blocks, http)} · "
            f"{stats.get('terr', 0)} · {_pct(stats.get('coll_ok', 0), stats.get('coll', 0))} · {paused} мин из {len(minutes)}"
        )
    return lines


def _source_table(metrics: list[dict]) -> list[str]:
    merged: dict[str, dict] = {}
    for row in metrics:
        for key, stats in (row.get("src") or {}).items():
            target = merged.setdefault(key, Counter())
            target.update({field: stats.get(field, 0) for field in ("ok", "fail", "retry", "blocked")})
            target["q"], target["to"] = stats.get("q"), stats.get("to")
    if not merged:
        return []
    lines = ["радар «запрос» → адрес на Авито: сканов · провалов · повторов · блоков"]
    for key, stats in sorted(merged.items(), key=lambda pair: -pair[1]["fail"]):
        scans = stats["ok"] + stats["fail"]
        lines.append(f"#{key} «{stats['q']}» → {stats['to']}: {scans} · {_pct(stats['fail'], scans)} · {stats['retry']} · {stats['blocked']}")
    return lines


def _outage_lines(metrics: list[dict]) -> list[str]:
    lines = []
    for run in _outages(metrics):
        ok, failed = _sum(run, "scans", "ok"), _sum(run, "scans", "fail")
        dead = [
            name for name, stats in sorted(_channels(run).items())
            if stats.get("http", 0) >= 5 and stats.get("ok", 0) / stats["http"] < 0.1
        ]
        lines.append(
            f"{run[0].get('ts')} … {str(run[-1].get('ts'))[-5:]} · {len(run)} мин · провалов {failed} из {ok + failed} · "
            f"слепая зона до {_worst(run, 'coverage_s')} с · {_top(_merge(run, 'fail'), 3)}"
            + (f" · не отвечали: {', '.join(dead)}" if dead else "")
        )
    return lines


def build_readiness(hours: int = 24, live_state: Optional[dict] = None) -> str:
    """The sale decision on one page: verdict first, then the numbers behind each point."""
    now = time.time()
    events = read_rows(EVENTS_LOG, now - hours * 3600)
    metrics = read_rows(METRICS_LOG, now - hours * 3600)
    checks = readiness_checks(metrics, events, hours)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now + 3 * 3600))
    lines = [f"# Avito Radar: готовность к продаже за {hours} ч, {stamp} МСК", f"Минутных строк: {len(metrics)}, событий: {len(events)}.", ""]
    lines += _section("Вердикт", [readiness_verdict(checks)])
    lines += _section("Критерии", checks)
    lines += _section("Скорость лота", _lot_timing(events))
    lines += _section("Провалы работы", _outage_lines(metrics))
    lines += _section("По часам", _timeline(metrics, 60))
    lines += _section("Каналы", _channel_load(metrics))
    lines += _section("Ссылки", _source_table(metrics))
    lines += _section("Воронка лотов", [_funnel(metrics)])
    lines += _section("Антибот", _challenges(metrics))
    lines += _section("Запуски", _restarts(events))
    if live_state:
        lines += _section("Состояние сейчас", [json.dumps(live_state, ensure_ascii=False, default=str)])
    lines += _section("Ошибки и предупреждения", _error_groups(events))
    crash = _crash_tail()
    if crash:
        lines += _section("crash.log", crash)
    return "\n".join(lines)


def select(rows: list[dict], trace: str = "", radar: int = 0, chat: int = 0, text: str = "") -> list[dict]:
    def matches(row: dict) -> bool:
        fields = row.get("f") or {}
        if trace and row.get("id") != trace:
            return False
        if radar and radar != fields.get("radar") and radar not in (fields.get("rids") or ()):
            return False
        if chat and chat != fields.get("chat"):
            return False
        return not text or text.lower() in json.dumps(row, ensure_ascii=False).lower()

    return [row for row in rows if matches(row)]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    args.add_argument("--minutes", type=int, default=60)
    args.add_argument("--ready", type=int, default=0)
    args.add_argument("--trace", default="")
    args.add_argument("--radar", type=int, default=0)
    args.add_argument("--chat", type=int, default=0)
    args.add_argument("--grep", default="")
    args.add_argument("--limit", type=int, default=200)
    options = args.parse_args()
    if options.ready:
        print(build_readiness(options.ready))
        return
    if not (options.trace or options.radar or options.chat or options.grep):
        print(build_report(options.minutes))
        return
    rows = read_rows(EVENTS_LOG, time.time() - options.minutes * 60)
    for row in select(rows, options.trace, options.radar, options.chat, options.grep)[-options.limit:]:
        print(_compact(row))


if __name__ == "__main__":
    main()

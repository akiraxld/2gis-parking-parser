"""
parser.py — преобразует сырой JSON-элемент из API 2ГИС в ParkingRecord.

Поддерживает три формата ответа:
  "items"     — /3.0/items?rubric_id=60340  (самый богатый: адрес, capacity, etc.)
  "byid"      — /3.0/items/byid             (те же поля, что items)
  "clustered" — /3.0/markers/clustered      (только координаты, имя, тариф, рейтинг)
"""

import re
from datetime import datetime, timezone
from typing import Optional

from models import ParkingRecord, Tristate

# ── Публичный API ──────────────────────────────────────────────────────────────

def parse_parking_item(raw: dict, *, source: str = "items") -> ParkingRecord:
    """
    Единственная точка входа для парсинга.

    raw    — один элемент из response.result.items[]
    source — "items" | "byid" | "clustered"
    """
    id_2gis = _clean_id(raw.get("id", ""))
    lat, lon = _extract_coords(raw, source=source)

    tariff = _extract_tariff(raw, source=source)
    parking_type, target_object = _extract_type_and_target(raw)

    return ParkingRecord(
        id_2gis=id_2gis,
        name=raw.get("name") or raw.get("name_ex", {}).get("primary") or "",
        url=_build_url(id_2gis),
        address=_extract_address(raw, source=source),
        latitude=lat,
        longitude=lon,
        district=_extract_district(raw),
        type=parking_type,
        target_object=target_object,
        is_paid=tariff["is_paid"],
        price_per_hour=tariff["price_per_hour"],
        raw_tariff=tariff["raw_tariff"],
        capacity=_extract_capacity(raw),
        working_hours=_extract_working_hours(raw),
        for_trucks=_extract_for_trucks(raw),
        paving_type=raw.get("paving_type"),
        has_disabled_parking=_extract_disabled_parking(raw),
        has_ev_charging=_extract_ev_charging(raw),
        rating=_extract_rating(raw),
        reviews_count=_extract_reviews_count(raw),
        parsed_at=datetime.now(timezone.utc),
    )


# ── Приватные хелперы ──────────────────────────────────────────────────────────

def _clean_id(raw_id: str) -> str:
    """
    В clustered-формате id выглядит как "70000001054874402_f4x4qe7DdBdB...".
    Числовая часть до первого "_" — это настоящий id объекта в 2ГИС.
    В items/byid id уже чистый: "70000001076150305".
    """
    return raw_id.split("_")[0] if "_" in raw_id else raw_id


def _build_url(id_2gis: str) -> str:
    """Каноническая ссылка на объект в 2ГИС Алматы."""
    return f"https://2gis.kz/almaty/parking/{id_2gis}"


def _extract_coords(raw: dict, *, source: str) -> tuple[float, float]:
    """
    clustered → lat/lon на верхнем уровне.
    items/byid → вложены в point: {"lat": ..., "lon": ...}.
    """
    if source == "clustered":
        return float(raw["lat"]), float(raw["lon"])
    point = raw.get("point") or {}
    return float(point.get("lat", 0.0)), float(point.get("lon", 0.0))


def _extract_address(raw: dict, *, source: str) -> Optional[str]:
    """
    clustered не содержит адреса вообще — возвращаем None.
    items/byid хранят готовую строку в address_name.
    """
    if source == "clustered":
        return None
    return raw.get("address_name")


def _extract_district(raw: dict) -> Optional[str]:
    """
    Район берём из adm_div[] с type == "district".
    Присутствует только в items/byid; в clustered — отсутствует.
    """
    for div in raw.get("adm_div") or []:
        if div.get("type") == "district":
            return div.get("name")
    return None


def _extract_tariff(raw: dict, *, source: str) -> dict:
    """
    Два источника тарифа, в зависимости от формата:

    clustered/items — context.stop_factors[] с тегами:
        parking_cost_parking_hour  → цена за час
        parking_cost_parking_day   → цена за сутки (в raw_tariff)
        parking_free_parking       → признак бесплатной парковки

    items/byid — поле is_paid (bool) как прямой ответ API.
    Stop_factors приоритетнее, потому что несут числовые значения.
    """
    result = {"is_paid": None, "price_per_hour": None, "raw_tariff": None}

    # Прямой флаг из items/byid
    if source in ("items", "byid") and "is_paid" in raw:
        result["is_paid"] = bool(raw["is_paid"])

    stop_factors = (raw.get("context") or {}).get("stop_factors") or []
    tariff_parts: list[str] = []

    for sf in stop_factors:
        tag = sf.get("tag", "")
        name = sf.get("name", "").replace("\xa0", " ").strip()

        if tag == "parking_cost_parking_hour":
            tariff_parts.append(name)
            result["is_paid"] = True
            # Извлекаем число: "400 тнг./час" → 400.0
            m = re.search(r"(\d[\d\s]*)", name)
            if m:
                result["price_per_hour"] = float(m.group(1).replace(" ", ""))

        elif tag == "parking_cost_parking_day":
            tariff_parts.append(name)
            # Не перезаписываем is_paid: суточная цена ≠ бесплатно

        elif tag == "parking_free_parking":
            tariff_parts.append(name)
            # Бесплатный период не означает полностью бесплатную парковку,
            # но если hour-тарифа нет — считаем бесплатной.
            if result["is_paid"] is None:
                result["is_paid"] = False

    if tariff_parts:
        result["raw_tariff"] = " | ".join(tariff_parts)

    return result


def _extract_capacity(raw: dict) -> Optional[int]:
    """
    Поле capacity присутствует только в items/byid.
    Может прийти как int или строка — приводим к int.
    """
    val = raw.get("capacity")
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _extract_working_hours(raw: dict) -> Optional[str]:
    """
    schedule — словарь вида {"Mon": {"working_hours": [{"from": "10:00", "to": "24:00"}]}, ...}.
    Если все дни одинаковые — сворачиваем в "Пн-Вс: 10:00–24:00".
    Иначе — перечисляем уникальные интервалы.
    """
    schedule = raw.get("schedule")
    if not schedule:
        return None

    day_map = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
               "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}

    def hours_str(day_data: dict) -> str:
        slots = day_data.get("working_hours") or []
        return ", ".join(f"{s['from']}–{s['to']}" for s in slots)

    # is_24x7 и подобные флаги — не дни недели, пропускаем нединарные значения
    intervals = {
        day: hours_str(data)
        for day, data in schedule.items()
        if isinstance(data, dict)
    }

    unique = set(intervals.values())
    if len(unique) == 1 and len(intervals) == 7:
        return f"Пн-Вс: {unique.pop()}"

    # Группируем дни с одинаковым расписанием
    groups: dict[str, list[str]] = {}
    for eng_day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        if eng_day in intervals:
            slot = intervals[eng_day]
            groups.setdefault(slot, []).append(day_map[eng_day])

    parts = [f"{', '.join(days)}: {slot}" for slot, days in groups.items()]
    return " | ".join(parts)


def _extract_for_trucks(raw: dict) -> Tristate:
    """
    for_trucks присутствует только в items/byid.
    API возвращает булево значение.
    """
    val = raw.get("for_trucks")
    if val is None:
        return Tristate.UNKNOWN
    return Tristate.YES if val else Tristate.NO


def _extract_disabled_parking(raw: dict) -> Tristate:
    """
    В items/byid ищем в attribute_groups атрибут с текстом про инвалидов.
    В clustered эта информация не приходит.
    """
    return _search_attribute_groups(
        raw,
        keywords=["инвалид", "колясочник", "маломобил"],
    )


def _extract_ev_charging(raw: dict) -> Tristate:
    """
    В items/byid ищем атрибут про зарядку для электромобилей.
    В clustered эта информация не приходит.
    """
    return _search_attribute_groups(
        raw,
        keywords=["электро", "зарядк", "ev ", "ev-"],
    )


def _extract_type_and_target(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Тип и целевой объект выводим из названия парковки эвристически,
    потому что API не возвращает явного поля "тип парковки".

    Паттерны (порядок важен — от специфичных к общим):
        ТРК / ТРЦ / ТД / Mall / Mega → ТЦ
        БЦ / бизнес-центр            → БЦ
        ЖК / жилой комплекс          → ЖК
        автостоянка / стоянка        → городская
        по умолчанию                 → неизвестно

    target_object — часть имени после ключевого слова (например "ТРК АДК" → "АДК").
    """
    name = (raw.get("name") or "").strip()
    name_lower = name.lower()

    # Паттерны: (ключевые слова в имени, тип парковки, regex для извлечения объекта)
    rules = [
        (r"\bтр[кц]\b|\bтд\b|\bmall\b|\bmega\b",       "ТЦ",          r"(?:ТРК|ТРЦ|ТД|Mall|Mega)\s+(.+)"),
        (r"\bбц\b|\bбизнес.центр\b",                    "БЦ",          r"(?:БЦ)\s+(.+)"),
        (r"\bжк\b|\bжилой\s+комплекс\b",                "ЖК",          r"(?:ЖК)\s+(.+)"),
        (r"автостоянк|стоянк",                          "городская",   None),
        (r"parking|паркинг",                            "паркинг",     None),
    ]

    for pattern, ptype, target_pattern in rules:
        if re.search(pattern, name_lower):
            target = None
            if target_pattern:
                m = re.search(target_pattern, name, re.IGNORECASE)
                if m:
                    target = m.group(1).strip(" ,–-")
            return ptype, target

    return "неизвестно", None


# ── Вспомогательная функция для attribute_groups ──────────────────────────────

def _extract_rating(raw: dict) -> Optional[float]:
    reviews = raw.get("reviews") or {}
    val = reviews.get("general_rating") or reviews.get("org_rating")
    return float(val) if val is not None else None


def _extract_reviews_count(raw: dict) -> Optional[int]:
    reviews = raw.get("reviews") or {}
    val = reviews.get("general_review_count") or reviews.get("org_review_count")
    return int(val) if val is not None else None


def _search_attribute_groups(raw: dict, *, keywords: list[str]) -> Tristate:
    """
    Проходит по attribute_groups и ищет атрибуты по ключевым словам.
    Если ни одной группы нет — UNKNOWN; если нашли — YES.

    Возвращает NO только если явно указано обратное
    (2ГИС не предоставляет явного "нет" — только наличие фичи).
    """
    groups = raw.get("attribute_groups")
    if not groups:
        return Tristate.UNKNOWN

    for group in groups:
        for attr in group.get("attributes") or []:
            attr_name = (attr.get("name") or "").lower()
            if any(kw in attr_name for kw in keywords):
                return Tristate.YES

    # Группы есть, но нужного атрибута нет — скорее всего нет фичи
    return Tristate.NO

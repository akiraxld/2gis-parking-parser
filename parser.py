"""
parser.py — маппинг сырого JSON-элемента 2ГИС → ParkingRecord.

Поддерживаемые форматы:
  "items"     — /3.0/items?rubric_id=60340  (богатый: адрес, capacity, attribute_groups)
  "byid"      — /3.0/items/byid             (те же поля, что items)
  "clustered" — /3.0/markers/clustered      (координаты, имя, тариф из context, рейтинг)

Честная граница возможностей:
  - price_per_hour и type — эвристики; могут быть None / "неизвестно" — это ожидаемо.
  - capacity, has_disabled_parking, has_ev_charging — только в items/byid.
  - address — только в items/byid; в clustered всегда None.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from models import ParkingRecord, Tristate
from utils import (
    build_address,
    format_schedule,
    get_attribute_by_tag,
    parse_price_from_text,
)

# ── Публичный API ──────────────────────────────────────────────────────────────

def parse_parking_item(raw: dict, *, source: str = "items") -> ParkingRecord:
    """
    Единственная точка входа.

    raw    — один элемент из response.result.items[]
    source — "items" | "byid" | "clustered"
    """
    id_2gis = _clean_id(raw.get("id", ""))
    lat, lon = _extract_coords(raw, source=source)
    tariff = _extract_tariff(raw, source=source)

    # price_per_hour — эвристика; None ожидаем, если тариф не распарсился
    parking_type, target_object = _extract_type_and_target(raw)
    # type — эвристика по названию; "неизвестно" — штатный результат

    return ParkingRecord(
        id_2gis=id_2gis,
        name=_extract_name(raw),
        url=_build_url(id_2gis),
        address=build_address(raw) if source != "clustered" else None,
        latitude=lat,
        longitude=lon,
        district=_extract_district(raw),
        type=parking_type,
        target_object=target_object,
        is_paid=tariff["is_paid"],
        price_per_hour=tariff["price_per_hour"],
        raw_tariff=tariff["raw_tariff"],
        capacity=_extract_capacity(raw),
        working_hours=format_schedule(raw.get("schedule")),
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
    В clustered id выглядит как "70000001054874402_f4x4qe7...".
    Числовая часть до "_" — настоящий id объекта 2ГИС.
    В items/byid id уже чистый.
    """
    return raw_id.split("_")[0] if "_" in raw_id else raw_id


def _build_url(id_2gis: str) -> str:
    """
    /firm/ — универсальный маршрут 2ГИС для любого объекта.
    /parking/ переадресует, но /firm/ надёжнее.
    """
    return f"https://2gis.kz/almaty/firm/{id_2gis}"


def _extract_name(raw: dict) -> str:
    return raw.get("name") or (raw.get("name_ex") or {}).get("primary") or ""


def _extract_coords(raw: dict, *, source: str) -> tuple[float, float]:
    """
    clustered → lat/lon на верхнем уровне объекта.
    items/byid → вложены в point: {"lat": ..., "lon": ...}.
    """
    if source == "clustered":
        return float(raw["lat"]), float(raw["lon"])
    point = raw.get("point") or {}
    return float(point.get("lat", 0.0)), float(point.get("lon", 0.0))


def _extract_district(raw: dict) -> Optional[str]:
    """Район из adm_div[type == "district"]. Только items/byid."""
    for div in raw.get("adm_div") or []:
        if div.get("type") == "district":
            return div.get("name")
    return None


def _extract_tariff(raw: dict, *, source: str) -> dict:
    """
    Источники тарифа по убыванию надёжности:

    1. attribute_groups, тег parking_price_comment — готовый текст тарифа (items/byid)
    2. context.stop_factors — теги parking_cost_parking_hour / parking_cost_parking_day
       (есть и в clustered, и в items)
    3. raw["is_paid"] — прямой bool-флаг (только items/byid)

    price_per_hour — эвристика: None ожидаем если тарифа нет или он не числовой.
    """
    result: dict = {"is_paid": None, "price_per_hour": None, "raw_tariff": None}

    # 1. Готовый текст тарифа из attribute_groups
    tariff_comment = get_attribute_by_tag(raw, "parking_price_comment")

    # 2. stop_factors — работают для обоих форматов
    stop_factors = (raw.get("context") or {}).get("stop_factors") or []
    sf_parts: list[str] = []

    for sf in stop_factors:
        tag = sf.get("tag", "")
        name = (sf.get("name") or "").replace("\xa0", " ").strip()

        if tag == "parking_cost_parking_hour":
            sf_parts.append(name)
            result["is_paid"] = True
            # price_per_hour — эвристика; None если число не найдено
            result["price_per_hour"] = parse_price_from_text(name)

        elif tag == "parking_cost_parking_day":
            sf_parts.append(name)

        elif tag == "parking_free_parking":
            sf_parts.append(name)
            # Бесплатный период ≠ полностью бесплатная парковка,
            # но если hour-цены нет — считаем бесплатной
            if result["is_paid"] is None:
                result["is_paid"] = False

    # raw_tariff: приоритет у явного комментария из attribute_groups
    if tariff_comment:
        result["raw_tariff"] = tariff_comment
    elif sf_parts:
        result["raw_tariff"] = " | ".join(sf_parts)

    # 3. Прямой флаг из items/byid (не перезаписываем если уже определили)
    if result["is_paid"] is None and source in ("items", "byid"):
        is_paid_raw = raw.get("is_paid")
        if is_paid_raw is not None:
            result["is_paid"] = bool(is_paid_raw)

    return result


def _extract_capacity(raw: dict) -> Optional[int]:
    """
    capacity в items/byid может быть:
      - int (старый формат)
      - {"total": N, "special_spaces": [...]} (новый формат)
    В clustered отсутствует.
    """
    val = raw.get("capacity")
    if val is None:
        return None
    if isinstance(val, dict):
        total = val.get("total")
        return int(total) if total is not None else None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _extract_for_trucks(raw: dict) -> Tristate:
    """Поле for_trucks — только items/byid, значение bool."""
    val = raw.get("for_trucks")
    if val is None:
        return Tristate.UNKNOWN
    return Tristate.YES if val else Tristate.NO


def _extract_disabled_parking(raw: dict) -> Tristate:
    """
    Источник 1: capacity.special_spaces[type == "handicapped"].count > 0
    Источник 2: attribute_groups (тег с "handicapped" / "инвалид")
    В clustered — всегда UNKNOWN (данных нет).
    """
    capacity_raw = raw.get("capacity")
    if isinstance(capacity_raw, dict):
        for space in capacity_raw.get("special_spaces") or []:
            if space.get("type") == "handicapped":
                return Tristate.YES if (space.get("count") or 0) > 0 else Tristate.NO

    # Fallback через attribute_groups
    groups = raw.get("attribute_groups")
    if not groups:
        return Tristate.UNKNOWN
    for group in groups:
        for attr in group.get("attributes") or []:
            name = (attr.get("name") or "").lower()
            tag = (attr.get("tag") or "").lower()
            if any(kw in name or kw in tag for kw in ["инвалид", "handicap", "маломобил"]):
                return Tristate.YES
    return Tristate.NO


def _extract_ev_charging(raw: dict) -> Tristate:
    """
    Ищем теги parking_ev / parking_ev_charging или слова «зарядка»/«электро».
    Данных почти никогда нет — UNKNOWN штатный результат.
    """
    ev_attr = get_attribute_by_tag(raw, "parking_ev") or get_attribute_by_tag(raw, "parking_ev_charging")
    if ev_attr is not None:
        return Tristate.YES

    groups = raw.get("attribute_groups")
    if not groups:
        return Tristate.UNKNOWN
    for group in groups:
        for attr in group.get("attributes") or []:
            name = (attr.get("name") or "").lower()
            tag = (attr.get("tag") or "").lower()
            if any(kw in name or kw in tag for kw in ["электро", "зарядк", "ev_", "ev-", "parking_ev"]):
                return Tristate.YES
    return Tristate.NO


def _extract_rating(raw: dict) -> Optional[float]:
    reviews = raw.get("reviews") or {}
    val = reviews.get("general_rating") or reviews.get("org_rating")
    return float(val) if val is not None else None


def _extract_reviews_count(raw: dict) -> Optional[int]:
    reviews = raw.get("reviews") or {}
    val = reviews.get("general_review_count") or reviews.get("org_review_count")
    return int(val) if val is not None else None


def _extract_type_and_target(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Тип и целевой объект — ЭВРИСТИКА, штатный результат ("неизвестно", None).

    Порядок источников:
    1. purpose_name — явное поле от 2ГИС (редко приходит для парковок)
    2. group[].type == "building" с именем — наиболее точно
    3. Паттерны в названии объекта
    """
    # 1. purpose_name (только items/byid, обычно None для парковок)
    purpose = (raw.get("purpose_name") or "").strip()
    if purpose:
        return purpose, None

    # 2. group с типом building даёт имя здания — это и есть target_object
    for g in raw.get("group") or []:
        if g.get("type") == "building" and g.get("name"):
            building_name = g["name"]
            ptype = _type_from_name(building_name) or "неизвестно"
            return ptype, building_name

    # 3. Эвристика по названию парковки
    name = (raw.get("name") or "").strip()
    ptype = _type_from_name(name)
    target = _target_from_name(name) if ptype not in (None, "городская", "паркинг") else None

    return ptype or "неизвестно", target


def _type_from_name(name: str) -> Optional[str]:
    """
    Определяет тип по ключевым словам в строке.
    Возвращает None если ничего не подошло — caller решает что делать дальше.
    """
    n = name.lower()
    if re.search(r"\bтр[кц]\b|\bтд\b|\bmall\b|\bmega\b|\bплаза\b|\baport\b", n):
        return "ТЦ"
    if re.search(r"\bбц\b|\bбизнес.центр\b|\boffice\b", n):
        return "БЦ"
    if re.search(r"\bжк\b|\bжилой\s+комплекс\b|\bresidential\b", n):
        return "ЖК"
    if re.search(r"автостоянк|стоянк", n):
        return "городская"
    if re.search(r"parking|паркинг", n):
        return "паркинг"
    return None


def _target_from_name(name: str) -> Optional[str]:
    """
    Из "Парковка ТРК АДК" → "ТРК АДК".
    Срезает префиксные слова "парковка", "стоянка", "паркинг" и возвращает остаток.
    """
    cleaned = re.sub(
        r"^(парковка|автостоянка|стоянка|паркинг|parking)[,\s]+",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    ).strip(" ,–-")
    return cleaned if cleaned and cleaned.lower() != name.lower() else None
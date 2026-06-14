"""
utils.py — четыре чистые функции без side-эффектов.
Каждая принимает только то, что ей нужно, и не знает о ParkingRecord.
"""

import re
from typing import Optional


def parse_price_from_text(text: str) -> Optional[float]:
    """
    Извлекает ЦЕНУ (не единицы времени) из строки тарифа.

    Стратегия: ищем число перед валютой (тнг/тг/тенге).
    Это защищает от "1 час - 400 тнг" → не хватаем "1".

    "400 тнг./час"       → 400.0
    "1 500 тнг./час"     → 1500.0
    "1 час - 400 тнг"    → 400.0  (берём число перед тнг, не "1")
    "бесплатно"          → None
    """
    cleaned = text.replace("\xa0", " ")

    # Ищем число непосредственно перед валютой: "400 тнг", "1 500тнг"
    m = re.search(r"([\d][\d\s]{0,6})\s*(?:тнг|тг|тенге)", cleaned, re.IGNORECASE)
    if m:
        digits = m.group(1).replace(" ", "")
        try:
            val = float(digits)
            # Защита от "1 тнг" — скорее всего это единица времени "1 час"
            if val >= 10:
                return val
        except ValueError:
            pass

    # Fallback: ищем число после слова "час" или после тире/двоеточия
    m = re.search(r"(?:час|:|-)\s*([\d][\d\s]{0,6})", cleaned, re.IGNORECASE)
    if m:
        digits = m.group(1).replace(" ", "")
        try:
            val = float(digits)
            if val >= 10:
                return val
        except ValueError:
            pass

    return None


def format_schedule(schedule: dict) -> Optional[str]:
    """
    Превращает словарь расписания 2ГИС в читаемую строку.

    Если есть ключ is_24x7 == True → "круглосуточно".
    Если все 7 дней с одинаковым интервалом → "Пн-Вс: HH:MM–HH:MM".
    Иначе → "Пн, Вт: 09:00–18:00 | Сб, Вс: 10:00–17:00".
    """
    if not schedule:
        return None

    # 2ГИС иногда кладёт is_24x7 как отдельный ключ рядом с днями
    if schedule.get("is_24x7"):
        return "круглосуточно"

    day_map = {
        "Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
        "Fri": "Пт", "Sat": "Сб", "Sun": "Вс",
    }

    def _slots_str(day_data: dict) -> str:
        slots = day_data.get("working_hours") or []
        if not slots:
            return ""
        return ", ".join(f"{s['from']}–{s['to']}" for s in slots)

    # Пропускаем нединарные значения (is_24x7, is_always_open и т.п.)
    intervals = {
        day: _slots_str(data)
        for day, data in schedule.items()
        if isinstance(data, dict)
    }
    if not intervals:
        return None

    # Проверяем is_24x7 внутри первого дня (альтернативная схема)
    first_day = next(iter(schedule.values()))
    if isinstance(first_day, dict) and first_day.get("is_24x7"):
        return "круглосуточно"

    unique_slots = set(intervals.values())
    if len(unique_slots) == 1 and len(intervals) == 7:
        return f"Пн-Вс: {unique_slots.pop()}"

    # Группируем дни с одинаковым расписанием, сохраняя порядок нед. дней
    groups: dict[str, list[str]] = {}
    for eng in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        if eng in intervals:
            slot = intervals[eng]
            groups.setdefault(slot, []).append(day_map[eng])

    return " | ".join(
        f"{', '.join(days)}: {slot}" for slot, days in groups.items()
    )


def build_address(raw: dict) -> Optional[str]:
    """
    Строит строку адреса по приоритету:
      1. address_name — готовая строка от 2ГИС (items/byid)
      2. address.components[] — собираем "улица, номер"
      3. None — для clustered, где адреса нет

    Район (adm_div district) не добавляем сюда: он идёт в отдельное поле district.
    """
    # Готовая строка — самый надёжный вариант
    if raw.get("address_name"):
        return raw["address_name"]

    components = (raw.get("address") or {}).get("components") or []
    if not components:
        return None

    # Собираем "улица номер" из компонентов типа street_number / street
    parts = []
    for c in components:
        ctype = c.get("type", "")
        if ctype == "street_number":
            # street + number дают полный адрес
            street = c.get("street", "")
            number = c.get("number", "")
            parts.append(f"{street}, {number}".strip(", "))
        elif ctype == "street" and not parts:
            parts.append(c.get("name", ""))

    return ", ".join(parts) if parts else None


def get_attribute_by_tag(raw: dict, tag: str) -> Optional[str]:
    """
    Ищет атрибут с точным совпадением тега в attribute_groups.
    Возвращает поле name первого найденного атрибута или None.

    Пример тегов для парковок:
      "parking_price_comment"       — текст тарифа
      "parking_cost_parking_hour"   — цена за час (в clustered это в context.stop_factors)
      "parking_ev"                  — зарядка EV
    """
    for group in raw.get("attribute_groups") or []:
        for attr in group.get("attributes") or []:
            if attr.get("tag") == tag:
                return attr.get("name")
    return None
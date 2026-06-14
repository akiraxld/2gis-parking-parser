"""
export.py — выгрузка всех парковок из SQLite в Google Sheets.

Требования:
  pip install gspread google-auth

Использует:
  - service_account.json  (файл сервисного аккаунта Google)
  - parkings.db           (SQLite с raw_items)
  - parser.py / models.py (маппинг JSON → ParkingRecord)

Запуск:
  python export.py
"""

import json
import sqlite3
from parser import parse_parking_item
from models import ParkingRecord, Tristate

import gspread
from google.oauth2.service_account import Credentials

# ── Настройки ─────────────────────────────────────────────────────────────────

SPREADSHEET_ID = "1Hjb0XcDKIqNgTikKE9Ok74ev0xFajF0QiPdhiavlNzQ"
SHEET_NAME = "Парковки"
SERVICE_ACCOUNT_FILE = "service_account.json"
DB_PATH = "parkings.db"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Порядок и заголовки столбцов
COLUMNS = [
    ("id_2gis",               "ID 2ГИС"),
    ("name",                  "Название"),
    ("url",                   "Ссылка"),
    ("address",               "Адрес"),
    ("district",              "Район"),
    ("latitude",              "Широта"),
    ("longitude",             "Долгота"),
    ("type",                  "Тип"),
    ("target_object",         "Объект"),
    ("is_paid",               "Платная"),
    ("price_per_hour",        "Цена/час (тнг)"),
    ("raw_tariff",            "Тариф (текст)"),
    ("capacity",              "Мест"),
    ("working_hours",         "Часы работы"),
    ("for_trucks",            "Для грузовых"),
    ("paving_type",           "Покрытие"),
    ("has_disabled_parking",  "Для инвалидов"),
    ("has_ev_charging",       "Зарядка EV"),
    ("rating",                "Рейтинг"),
    ("reviews_count",         "Отзывов"),
    ("parsed_at",             "Дата парсинга"),
]


def record_to_row(r: ParkingRecord) -> list:
    """Преобразует ParkingRecord в список значений для Google Sheets."""
    def fmt_bool(v):
        if v is None:
            return ""
        return "Да" if v else "Нет"

    def fmt_tristate(v: Tristate):
        return v.value  # "Да" / "Нет" / "Нет данных"

    return [
        r.id_2gis,
        r.name,
        r.url,
        r.address or "",
        r.district or "",
        r.latitude,
        r.longitude,
        r.type or "",
        r.target_object or "",
        fmt_bool(r.is_paid),
        r.price_per_hour if r.price_per_hour is not None else "",
        r.raw_tariff or "",
        r.capacity if r.capacity is not None else "",
        r.working_hours or "",
        fmt_tristate(r.for_trucks),
        r.paving_type or "",
        fmt_tristate(r.has_disabled_parking),
        fmt_tristate(r.has_ev_charging),
        r.rating if r.rating is not None else "",
        r.reviews_count if r.reviews_count is not None else "",
        r.parsed_at.strftime("%Y-%m-%d %H:%M") if r.parsed_at else "",
    ]


def load_records_from_db(db_path: str) -> list[ParkingRecord]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Берём rich_json если есть, иначе raw_json (clustered данные)
    rows = conn.execute("""
        SELECT
            id_2gis,
            CASE
                WHEN rich_json IS NOT NULL AND rich_json != '{}'
                THEN rich_json
                ELSE raw_json
            END AS best_json,
            CASE
                WHEN rich_json IS NOT NULL AND rich_json != '{}'
                THEN 'byid'
                ELSE 'clustered'
            END AS source
        FROM raw_items
        ORDER BY id_2gis
    """).fetchall()
    conn.close()

    records = []
    errors = 0
    for row in rows:
        try:
            item = json.loads(row["best_json"])
            record = parse_parking_item(item, source=row["source"])
            records.append(record)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  [warn] id={row['id_2gis']}: {e}")
    if errors:
        print(f"  [warn] Всего ошибок парсинга: {errors}")
    return records


def get_or_create_sheet(gc: gspread.Client, spreadsheet_id: str, sheet_name: str):
    """Возвращает лист с нужным именем, создаёт если нет."""
    spreadsheet = gc.open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows=2000, cols=len(COLUMNS))


def main():
    print("[1/4] Загружаем данные из SQLite...")
    records = load_records_from_db(DB_PATH)
    print(f"      Загружено: {len(records)} парковок")

    print("[2/4] Подключаемся к Google Sheets...")
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = get_or_create_sheet(gc, SPREADSHEET_ID, SHEET_NAME)
    print(f"      Лист: «{SHEET_NAME}»")

    print("[3/4] Формируем данные...")
    headers = [col[1] for col in COLUMNS]
    rows = [record_to_row(r) for r in records]

    print(f"[4/4] Записываем {len(rows)} строк в Google Sheets...")
    # Очищаем лист и пишем всё с нуля
    sheet.clear()

    all_data = [headers] + rows
    sheet.update(
        range_name="A1",
        values=all_data,
    )

    # Жирный заголовок
    sheet.format("A1:U1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.9},
    })

    # Закрепляем первую строку
    sheet.freeze(rows=1)

    print(f"\nГотово! Записано {len(rows)} парковок.")
    print(f"Открыть: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")


if __name__ == "__main__":
    main()

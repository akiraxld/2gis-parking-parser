import sqlite3, json
from parser import parse_parking_item

conn = sqlite3.connect("parkings.db")
rows = conn.execute(
    "SELECT rich_json FROM raw_items WHERE rich_json IS NOT NULL AND rich_json != '{}' LIMIT 5"
).fetchall()

for row in rows:
    item = json.loads(row[0])
    try:
        record = parse_parking_item(item, source="byid")
        print(f"OK: {record.name} | {record.address} | paid={record.is_paid} | {record.working_hours}")
    except Exception as e:
        print(f"ОШИБКА: {e} | item keys: {list(item.keys())}")


from parser import _type_from_name, _target_from_name

tests = [
    "Парковка ТЦ Colibri",
    "Парковка ТРК Aport Mall",
    "Паркинг БЦ Нурлы Тау",
    "Платная парковка",
    "Автостоянка №5",
    "Парковка ЖК Алма-Ата",
    "Парковка отель Rixos",
]
for t in tests:
    print(f"{t!r:40} → тип={_type_from_name(t)!r:15} target={_target_from_name(t)!r}")
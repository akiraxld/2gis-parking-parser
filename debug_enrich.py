"""Диагностика: проверяем что возвращает byid API для наших ID."""
import sqlite3, json, httpx

conn = sqlite3.connect("parkings.db")
conn.row_factory = sqlite3.Row

# Смотрим первые 5 записей (у всех сейчас rich_json='{}')
rows = conn.execute(
    "SELECT id_2gis, raw_json FROM raw_items LIMIT 5"
).fetchall()

print(f"""Записей без rich_json: {conn.execute('SELECT COUNT(*) FROM raw_items WHERE rich_json IS NULL').fetchone()[0]}""")
print(f"""Записей с rich_json='{{}}': {conn.execute("SELECT COUNT(*) FROM raw_items WHERE rich_json = '{}'").fetchone()[0]}""")
print(f"""Записей с rich_json не null и не '{{}}': {conn.execute("SELECT COUNT(*) FROM raw_items WHERE rich_json IS NOT NULL AND rich_json != '{}'").fetchone()[0]}""")
print()

print("=== Первые 5 ID из БД ===")
for row in rows:
    raw = json.loads(row["raw_json"])
    print(f"  id_2gis={row['id_2gis']}  raw_id={raw.get('id','?')[:60]}")

# Пробуем один запрос к byid
print(f"\nПервый raw_json:")
print(json.dumps(json.loads(rows[0]["raw_json"]), ensure_ascii=False, indent=2)[:600])

if rows:
    test_ids = [row["id_2gis"] for row in rows[:3]]
    print(f"\n=== Тестовый byid запрос для IDs: {test_ids} ===")

    API_KEY = "c7f1a769-c8a5-4636-b14d-d8c987808a12"
    FIELDS = "items.name_ex,items.point,items.address,items.adm_div,items.reviews,items.is_paid,items.capacity,items.schedule,items.attribute_groups"

    resp = httpx.get(
        "https://catalog.api.2gis.ru/3.0/items/byid",
        params={
            "id": ",".join(test_ids),
            "key": API_KEY,
            "locale": "ru_KZ",
            "fields": FIELDS,
        },
        timeout=15,
    )
    print(f"HTTP статус: {resp.status_code}")
    data = resp.json()
    print(f"meta: {data.get('meta')}")
    result = data.get("result") or {}
    items = result.get("items") or []
    print(f"items в ответе: {len(items)}")
    if items:
        print(f"Первый item id: {items[0].get('id')}")
        print(json.dumps(items[0], ensure_ascii=False, indent=2)[:800])
    else:
        print("Полный ответ:")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1000])

conn.close()

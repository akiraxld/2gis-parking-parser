import sqlite3, json

conn = sqlite3.connect("parkings.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id_2gis, rich_json FROM raw_items
    WHERE rich_json IS NOT NULL AND rich_json != '{}'
    LIMIT 200
""").fetchall()

seen = set()
for row in rows:
    item = json.loads(row["rich_json"])
    stop_factors = (item.get("context") or {}).get("stop_factors") or []
    for sf in stop_factors:
        tag = sf.get("tag", "")
        name = (sf.get("name") or "").replace("\xa0", " ").strip()
        key = (tag, name)
        if key not in seen:
            seen.add(key)
            print(f"  tag={tag!r:45} name={name!r}")

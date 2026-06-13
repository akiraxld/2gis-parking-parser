import sqlite3, json
conn = sqlite3.connect('parkings.db')
item = json.loads(conn.execute('SELECT raw_json FROM raw_items LIMIT 1').fetchone()[0])
print(json.dumps(item, ensure_ascii=False, indent=2))
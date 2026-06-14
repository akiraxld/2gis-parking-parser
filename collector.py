"""
collector.py — двухфазный сборщик парковок Алматы с 2ГИС.

Фаза 1 (playwright_phase): перехватываем clustered API через браузер.
  Проблема zoom=13: при мелком зуме 2ГИС кластеризует объекты — один маркер
  представляет группу из 4-20 парковок. Решение: zoom=15 (~250м вьюпорт)
  + сетка 12×12 = 144 ячейки. Цель — собрать максимум уникальных ID.

Фаза 2 (enrich_phase): обогащаем каждый ID через /3.0/items/byid напрямую.
  byid возвращает адрес, capacity, attribute_groups, adm_div, schedule и т.д.
  API-ключ уже известен из перехваченных запросов. Батчи по 50 ID.

Итог: богатые записи сохраняются в raw_items.rich_json.
Запуск обеих фаз: python collector.py
Только обогащение: python collector.py --enrich-only
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv
from playwright.async_api import Response, async_playwright

load_dotenv()

# ── Настройки ─────────────────────────────────────────────────────────────────

ALMATY_BBOX = (76.724, 43.112, 77.062, 43.324)  # lon_min, lat_min, lon_max, lat_max
GRID_N = 12   # 12×12 = 144 ячейки; при zoom=15 покрывает весь Алматы с перекрытием
ZOOM = 15     # zoom=15 → ~250м вьюпорт, минимум кластеризации

DB_PATH = "parkings.db"

API_KEY = os.getenv("DGIS_API_KEY", "")
BYID_URL = "https://catalog.api.2gis.com/3.0/items/byid"  # .com — официальный эндпоинт
BYID_BATCH = 50  # максимум ID за один запрос

# Поля для byid — всё что нужно для ParkingRecord
BYID_FIELDS = (
    "items.name_ex,items.point,items.address,items.adm_div,"
    "items.reviews,items.is_paid,items.capacity,items.schedule,"
    "items.for_trucks,items.paving_type,items.attribute_groups,"
    "items.stop_factors,items.context,items.group,items.purpose,"
    "items.purpose_code,items.rubrics,items.flags"
)

SEARCH_BASE = (
    "https://2gis.kz/almaty/search/"
    "%D0%BF%D0%B0%D1%80%D0%BA%D0%BE%D0%B2%D0%BA%D0%B0"
    "/rubricId/60340"
)

API_RE = re.compile(r"https://catalog\.api\.2gis\.(ru|com|kz)/3\.0/(items|markers)")

WAIT_AFTER_GOTO_MS = 7000
WAIT_BETWEEN_CELLS_MS = 1200


# ── Ячейка сетки ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BBox:
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    @property
    def cell_id(self) -> str:
        return f"{self.lon_min:.5f},{self.lat_min:.5f},{self.lon_max:.5f},{self.lat_max:.5f}"

    @property
    def center_lon(self) -> float:
        return (self.lon_min + self.lon_max) / 2

    @property
    def center_lat(self) -> float:
        return (self.lat_min + self.lat_max) / 2

    def map_url(self, zoom: int) -> str:
        return f"{SEARCH_BASE}?m={self.center_lon:.5f},{self.center_lat:.5f}/{zoom}"


def generate_grid(bbox: tuple, n: int) -> list[BBox]:
    lon_min, lat_min, lon_max, lat_max = bbox
    lon_step = (lon_max - lon_min) / n
    lat_step = (lat_max - lat_min) / n
    cells = []
    for row in range(n):
        for col in range(n):
            cells.append(BBox(
                lon_min=lon_min + col * lon_step,
                lat_min=lat_min + row * lat_step,
                lon_max=lon_min + (col + 1) * lon_step,
                lat_max=lat_min + (row + 1) * lat_step,
            ))
    return cells


# ── SQLite ─────────────────────────────────────────────────────────────────────

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_items (
            id_2gis     TEXT PRIMARY KEY,
            raw_json    TEXT NOT NULL,
            rich_json   TEXT,
            source_cell TEXT,
            fetched_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS grid_cells (
            cell_id      TEXT PRIMARY KEY,
            status       TEXT NOT NULL DEFAULT 'pending',
            items_found  INTEGER,
            processed_at TEXT
        );
    """)
    # Добавляем rich_json если таблица уже существовала без него
    try:
        conn.execute("ALTER TABLE raw_items ADD COLUMN rich_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    return conn


def _clean_id(raw_id: str) -> str:
    return raw_id.split("_")[0] if "_" in raw_id else raw_id


def save_items(conn: sqlite3.Connection, items: list[dict], cell_id: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    for item in items:
        raw_id = item.get("id", "")
        id_2gis = _clean_id(raw_id)
        if not id_2gis or not id_2gis.isdigit():
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_items (id_2gis, raw_json, source_cell, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (id_2gis, json.dumps(item, ensure_ascii=False), cell_id, now),
        )
        new_count += cur.rowcount
    conn.commit()
    return new_count


def mark_cell(conn: sqlite3.Connection, cell_id: str, status: str, items_found: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO grid_cells (cell_id, status, items_found, processed_at) "
        "VALUES (?, ?, ?, ?)",
        (cell_id, status, items_found, now),
    )
    conn.commit()


# ── Фаза 1: Playwright ────────────────────────────────────────────────────────

async def playwright_phase(db_path: str = DB_PATH) -> None:
    conn = init_db(db_path)
    grid = generate_grid(ALMATY_BBOX, GRID_N)

    done_cells = {
        row["cell_id"]
        for row in conn.execute("SELECT cell_id FROM grid_cells WHERE status = 'done'")
    }

    remaining = len(grid) - len(done_cells)
    print(f"Сетка {GRID_N}×{GRID_N} = {len(grid)} ячеек, zoom={ZOOM}")
    print(f"Уже обработано: {len(done_cells)}, осталось: {remaining}")

    if remaining == 0:
        print("Все ячейки уже обработаны. Переходим к обогащению.")
        conn.close()
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context(
            locale="ru-KZ",
            viewport={"width": 1400, "height": 900},
            service_workers="block",
        )
        page = await context.new_page()
        cell_items: list[dict] = []

        async def on_response(response: Response) -> None:
            url = response.url
            if not API_RE.search(url):
                return
            if response.status != 200:
                return
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            try:
                data = await response.json()
            except Exception:
                return

            items = (data.get("result") or {}).get("items") or []
            if not items:
                return

            # Пропускаем кластеры (cluster.count > 1 без реального id)
            real_items = [
                it for it in items
                if not it.get("cluster") and it.get("id")
            ]
            endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
            print(f"    [{endpoint}] {len(items)} объектов, реальных: {len(real_items)}")
            cell_items.extend(real_items)

        page.on("response", on_response)

        print("\n[init] Открываем 2ГИС...")
        await page.goto("https://2gis.kz/almaty", wait_until="domcontentloaded", timeout=60_000)

        for sel in ("button:has-text('Принять')", "button:has-text('Согласен')", "[data-qa='cookie-agree']"):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    print("  [init] cookie-баннер закрыт")
                    break
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        for i, cell in enumerate(grid, start=1):
            if cell.cell_id in done_cells:
                print(f"[{i:03d}/{len(grid)}] SKIP")
                continue

            print(f"\n[{i:03d}/{len(grid)}] {cell.cell_id}")
            cell_items.clear()

            await page.goto(
                cell.map_url(ZOOM),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(WAIT_AFTER_GOTO_MS)

            new_items = save_items(conn, cell_items, cell.cell_id)
            total_in_db = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
            print(f"  Поймано: {len(cell_items)}, новых: {new_items}, всего в БД: {total_in_db}")

            mark_cell(conn, cell.cell_id, "done", items_found=new_items)
            await page.wait_for_timeout(WAIT_BETWEEN_CELLS_MS)

        await browser.close()

    total = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    print(f"\n{'─' * 40}")
    print(f"Уникальных парковок в БД: {total}")
    conn.close()


# ── Фаза 2: Обогащение через byid (официальный ключ, прямые HTTP запросы) ──────

def enrich_phase(db_path: str = DB_PATH) -> None:
    """
    Использует официальный демо-ключ 2ГИС из .env (DGIS_API_KEY).
    Официальный ключ работает для прямых HTTP запросов без сессионных параметров.
    Батчи по 50 ID через /3.0/items/byid.
    """
    if not API_KEY:
        print("[enrich] ОШИБКА: переменная DGIS_API_KEY не задана в .env")
        return

    conn = init_db(db_path)

    # Сбрасываем '{}' (старые неудачные попытки) обратно в NULL
    reset = conn.execute(
        "UPDATE raw_items SET rich_json = NULL WHERE rich_json = '{}'"
    ).rowcount
    conn.commit()
    if reset:
        print(f"[enrich] Сброшено {reset} старых пустых записей для повторной попытки")

    rows = conn.execute(
        "SELECT id_2gis FROM raw_items WHERE rich_json IS NULL"
    ).fetchall()
    ids = [row["id_2gis"] for row in rows]

    if not ids:
        print("[enrich] Все записи уже обогащены.")
        conn.close()
        return

    total_batches = (len(ids) + BYID_BATCH - 1) // BYID_BATCH
    print(f"\n[enrich] Обогащаем {len(ids)} записей, батчами по {BYID_BATCH} ({total_batches} батчей)...")

    enriched = 0
    not_found = 0
    errors = 0

    with httpx.Client(timeout=30, headers={"Accept": "application/json"}) as client:
        for batch_num, batch_start in enumerate(range(0, len(ids), BYID_BATCH), start=1):
            batch = ids[batch_start : batch_start + BYID_BATCH]

            try:
                resp = client.get(BYID_URL, params={
                    "id": ",".join(batch),
                    "key": API_KEY,
                    "locale": "ru_KZ",
                    "fields": BYID_FIELDS,
                })
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  [batch {batch_num}/{total_batches}] ОШИБКА запроса: {e}")
                errors += 1
                time.sleep(3)
                continue

            meta = data.get("meta") or {}
            meta_code = meta.get("code")
            if meta_code == 403:
                err_msg = (meta.get("error") or {}).get("message", "")
                print(f"  [batch {batch_num}/{total_batches}] 403: {err_msg}")
                print("  Проверь что DGIS_API_KEY в .env правильный и активный")
                errors += 1
                break  # нет смысла продолжать если ключ не работает

            items = (data.get("result") or {}).get("items") or []
            item_by_id = {_clean_id(it.get("id", "")): it for it in items}

            for id_2gis in batch:
                rich = item_by_id.get(id_2gis)
                if rich:
                    conn.execute(
                        "UPDATE raw_items SET rich_json = ? WHERE id_2gis = ?",
                        (json.dumps(rich, ensure_ascii=False), id_2gis),
                    )
                    enriched += 1
                else:
                    # Объект удалён из 2ГИС или не найден — помечаем чтобы не повторять
                    conn.execute(
                        "UPDATE raw_items SET rich_json = '{}' WHERE id_2gis = ?",
                        (id_2gis,),
                    )
                    not_found += 1

            conn.commit()
            print(
                f"  [batch {batch_num}/{total_batches}] "
                f"+{len(item_by_id)} богатых | не найдено: {not_found} | всего: {enriched}/{len(ids)}"
            )

            time.sleep(0.3)  # вежливая пауза

    conn.close()
    print(f"\n[enrich] Готово: {enriched} обогащено, {not_found} не найдено, {errors} ошибок")


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Сборщик парковок 2ГИС Алматы")
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="Пропустить фазу Playwright, только обогатить существующие ID из БД",
    )
    parser.add_argument("--db", default=DB_PATH, help=f"Путь к SQLite (default: {DB_PATH})")
    args = parser.parse_args()

    if not args.enrich_only:
        asyncio.run(playwright_phase(args.db))

    enrich_phase(args.db)
    print("\nВсё готово. Запусти export.py для выгрузки в Google Sheets.")

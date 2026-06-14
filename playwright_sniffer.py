"""
playwright_sniffer.py (v3) — активное управление вьюпортом карты.

Стратегия:
  1. Открыть 2gis.kz/almaty — получить куки и сессию
  2. Для каждой ячейки 4×4 сетки: полный goto на URL с центром ячейки
  3. Перехватить /3.0/markers/clustered ответ — он летит автоматически
  4. Сохранить уникальные объекты в SQLite, дедупликация по id_2gis

Почему clustered, а не items:
  /3.0/items требует сессионных параметров (stat[sid], search_user_hash)
  которые 2ГИС генерирует динамически. clustered принимает только
  ключ + вьюпорт и работает стабильно из браузерного контекста.

Почему goto на каждую ячейку, а не pushState:
  2ГИС — SPA на собственном роутере, он не реагирует на нативный popstate.
  Только полный переход гарантирует новый сетевой запрос к clustered.

Zoom=15: мелкий вьюпорт → меньше кластеризации → точнее координаты.
"""

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Response, async_playwright

# ── Настройки ─────────────────────────────────────────────────────────────────

ALMATY_BBOX = (76.724, 43.112, 77.062, 43.324)  # lon_min, lat_min, lon_max, lat_max
GRID_N = 12   # 12×12 = 144 ячейки; перекрытие гарантирует что ни один объект не пропущен
ZOOM = 15     # zoom=15 → ~250м вьюпорт, минимум кластеризации

DB_PATH = "parkings.db"

SEARCH_BASE = (
    "https://2gis.kz/almaty/search/"
    "%D0%BF%D0%B0%D1%80%D0%BA%D0%BE%D0%B2%D0%BA%D0%B0"  # "парковка"
    "/rubricId/60340"
)

# Ловим /3.0/items и /3.0/markers — оба могут дать парковки
API_RE = re.compile(r"https://catalog\.api\.2gis\.(ru|com|kz)/3\.0/(items|markers)")

WAIT_AFTER_GOTO_MS = 6000     # ждём XHR после перехода
WAIT_BETWEEN_CELLS_MS = 1500  # пауза между ячейками


# ── Модель ячейки ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BBox:
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    @property
    def cell_id(self) -> str:
        return f"{self.lon_min:.6f},{self.lat_min:.6f},{self.lon_max:.6f},{self.lat_max:.6f}"

    @property
    def center_lon(self) -> float:
        return (self.lon_min + self.lon_max) / 2

    @property
    def center_lat(self) -> float:
        return (self.lat_min + self.lat_max) / 2

    def map_url(self, zoom: int) -> str:
        """URL с центром ячейки — при открытии браузер сам запросит clustered."""
        return f"{SEARCH_BASE}?m={self.center_lon:.5f},{self.center_lat:.5f}/{zoom}"


def generate_grid(bbox: tuple, n: int) -> list[BBox]:
    """Делит bbox на n×n равных ячеек."""
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
    conn.commit()
    return conn


def _clean_id(raw_id: str) -> str:
    """clustered возвращает id с суффиксом типа "70000001_abc" — берём только число."""
    return raw_id.split("_")[0] if "_" in raw_id else raw_id


def save_items(conn: sqlite3.Connection, items: list[dict], cell_id: str) -> int:
    """Сохраняет items в БД, пропуская дубликаты. Возвращает число новых записей."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    for item in items:
        id_2gis = _clean_id(item.get("id", ""))
        if not id_2gis:
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


# ── Playwright ─────────────────────────────────────────────────────────────────

async def run() -> None:
    conn = init_db(DB_PATH)
    grid = generate_grid(ALMATY_BBOX, GRID_N)

    # Ячейки которые уже успешно обработаны — пропускаем при перезапуске
    done_cells = {
        row["cell_id"]
        for row in conn.execute("SELECT cell_id FROM grid_cells WHERE status = 'done'")
    }

    remaining = len(grid) - len(done_cells)
    print(f"Сетка {GRID_N}×{GRID_N} = {len(grid)} ячеек, zoom={ZOOM}")
    print(f"Уже обработано: {len(done_cells)}, осталось: {remaining}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context(
            locale="ru-KZ",
            viewport={"width": 1400, "height": 900},
            service_workers="block",  # service worker может перехватывать запросы раньше нас
        )
        page = await context.new_page()

        # Буфер items текущей ячейки — очищается перед каждым goto
        cell_items: list[dict] = []

        async def on_response(response: Response) -> None:
            """Перехватчик всех сетевых ответов."""
            url = response.url

            # DEBUG: показываем все запросы к API 2ГИС
            if "catalog.api.2gis" in url:
                print(f"  [api] {response.status} {url[:120]}")

            if not API_RE.search(url):
                return
            if response.status != 200:
                return

            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return

            try:
                data = await response.json()
            except Exception as e:
                print(f"  [error] json parse: {e}")
                return

            items = (data.get("result") or {}).get("items") or []
            if not items:
                return

            endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
            print(f"    [{endpoint}] +{len(items)} объектов")
            cell_items.extend(items)

        page.on("response", on_response)

        # ── Первичная загрузка: получаем куки и сессию ────────────────────────
        print("\n[init] Открываем 2ГИС...")
        await page.goto("https://2gis.kz/almaty", wait_until="domcontentloaded", timeout=60_000)

        # Закрываем cookie-баннер если появился
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

        # ── Основной цикл: обходим ячейки ─────────────────────────────────────
        for i, cell in enumerate(grid, start=1):
            if cell.cell_id in done_cells:
                print(f"[{i:02d}/{len(grid)}] SKIP: {cell.cell_id}")
                continue

            print(f"\n[{i:02d}/{len(grid)}] Ячейка: {cell.cell_id}")

            # Очищаем буфер перед каждым переходом
            cell_items.clear()

            # Полный goto — единственный способ гарантировать новый clustered запрос
            await page.goto(
                cell.map_url(ZOOM),
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            # Ждём пока браузер получит и отдаст нам XHR
            await page.wait_for_timeout(WAIT_AFTER_GOTO_MS)

            # Сохраняем что поймали
            new_items = save_items(conn, cell_items, cell.cell_id)
            total_in_db = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
            print(f"  Поймано: {len(cell_items)}, новых: {new_items}, всего в БД: {total_in_db}")

            mark_cell(conn, cell.cell_id, "done", items_found=new_items)

            # Пауза между ячейками
            await page.wait_for_timeout(WAIT_BETWEEN_CELLS_MS)

        await browser.close()

    # ── Итог ──────────────────────────────────────────────────────────────────
    total = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM grid_cells WHERE status='done'").fetchone()[0]
    print(f"\n{'─' * 40}")
    print(f"Ячеек пройдено: {done}/{len(grid)}")
    print(f"Уникальных парковок в БД: {total}")
    print(f"База: {Path(DB_PATH).resolve()}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(run())
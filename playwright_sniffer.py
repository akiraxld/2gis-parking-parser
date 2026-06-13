"""
Playwright network sniffer для 2ГИС — парковки Алматы.

Стратегия:
1. Открыть базовый Алматы с картой (m=), без page/rubric в URL.
2. Перейти на поиск парковок с rubricId=60340 — тоже без page.
3. Скроллить панель результатов — infinite scroll триггерит новые /3.0/items.
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Response, async_playwright

# ── Настройки ──────────────────────────────────────────────────────────────

# Шаг 1: только Алматы + карта. БЕЗ page, БЕЗ rubricId.
BASE_ALMATY_URL = "https://2gis.kz/almaty?m=76.893,43.218/12"

# Шаг 2: поиск парковок в рубрике 60340, с тем же центром карты. БЕЗ /page/N.
SEARCH_URL = (
    "https://2gis.kz/almaty/search/"
    "%D0%BF%D0%B0%D1%80%D0%BA%D0%BE%D0%B2%D0%BA%D0%B0"
    "/rubricId/60340"
    "?m=76.893,43.218/12"
)

SAMPLES_DIR = Path("samples")

# Ловим все варианты API-хостов 2ГИС
API_URL_RE = re.compile(r"https://catalog\.api\.2gis\.(ru|com|kz)/3\.0/(items|markers)")

SCROLL_ROUNDS = 12       # сколько раз прокрутить вниз
SCROLL_PAUSE_MS = 2500   # пауза между скроллами — ждём XHR
WAIT_AFTER_SEARCH_MS = 6000

# Печатать ВСЕ API-запросы в консоль (для отладки)
DEBUG_LOG_ALL_API = True


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _extract_url_params(url: str) -> dict[str, str]:
    parsed = parse_qs(urlparse(url).query)
    return {k: v[0] for k, v in parsed.items() if v}


def _build_filename(response: Response) -> str:
    path = urlparse(response.url).path
    endpoint = path.rstrip("/").split("/")[-1]
    params = _extract_url_params(response.url)
    page = params.get("page", "nopage")
    rubric = params.get("rubric_id", "no_rubric")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}__{endpoint}__page{page}__rubric{rubric}.json"


# ── Скролл панели результатов ────────────────────────────────────────────────

async def _scroll_results_panel(page) -> bool:
    """
    2ГИС: скроллится не вся страница, а боковая панель со списком.
    Ищем scrollable-элемент и крутим его вниз.
    """
    scrolled = await page.evaluate(
        """() => {
            const candidates = [...document.querySelectorAll('div')].filter(el => {
                const s = getComputedStyle(el);
                const scrollable = s.overflowY === 'auto' || s.overflowY === 'scroll';
                return scrollable && el.scrollHeight > el.clientHeight + 200;
            });

            // Берём самый высокий скроллируемый блок (обычно панель результатов)
            const panel = candidates.sort(
                (a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth)
            )[0];

            if (!panel) return false;

            const before = panel.scrollTop;
            panel.scrollTop += 700;
            return panel.scrollTop > before;
        }"""
    )
    return bool(scrolled)


async def _fallback_wheel_scroll(page) -> None:
    """Запасной вариант: колесо мыши над левой частью экрана (панель результатов)."""
    await page.mouse.move(350, 500)
    await page.mouse.wheel(0, 900)


# ── Main ─────────────────────────────────────────────────────────────────────

async def run() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    seen_urls: set[str] = set()

    async def on_response(response: Response) -> None:
        nonlocal saved_count

        url = response.url

        if DEBUG_LOG_ALL_API and "catalog.api.2gis" in url:
            print(f"[api] {response.status} {url[:160]}")

        if not API_URL_RE.search(url):
            return
        if response.status != 200:
            return

        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            print(f"[skip] не JSON: {content_type} | {url[:120]}")
            return

        if url in seen_urls:
            return
        seen_urls.add(url)

        try:
            data = await response.json()
        except Exception as exc:
            print(f"[skip] json error: {exc}")
            return

        # Сохраняем только ответы, где есть result.items (список парковок)
        items = (data.get("result") or {}).get("items")
        if not items:
            print(f"[skip] пустой items: {url[:120]}")
            return

        filepath = SAMPLES_DIR / _build_filename(response)
        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "request_url": url,
            "request_params": _extract_url_params(url),
            "items_count": len(items),
            "response": data,
        }
        filepath.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved_count += 1
        print(f"[saved] {filepath.name} ({len(items)} items)")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        context = await browser.new_context(
            locale="ru-KZ",
            viewport={"width": 1400, "height": 900},
            # Важно: service worker может «съедать» сетевые события
            service_workers="block",
        )
        page = await context.new_page()
        page.on("response", on_response)

        # ── Шаг 1: базовый Алматы ──
        print(f"1) Открываю базовый Алматы: {BASE_ALMATY_URL}")
        await page.goto(BASE_ALMATY_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3000)

        # Cookie-баннер (если есть)
        for sel in ("button:has-text('Принять')", "button:has-text('Согласен')"):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    print("   Cookie-баннер закрыт")
                    break
            except Exception:
                pass

        # ── Шаг 2: поиск парковок (без page в URL) ──
        print(f"2) Перехожу на поиск: {SEARCH_URL}")
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"   Жду первичные XHR ({WAIT_AFTER_SEARCH_MS} мс)...")
        await page.wait_for_timeout(WAIT_AFTER_SEARCH_MS)

        # ── Шаг 3: infinite scroll ──
        print(f"3) Скроллю панель результатов ({SCROLL_ROUNDS} раз)...")
        for i in range(1, SCROLL_ROUNDS + 1):
            ok = await _scroll_results_panel(page)
            if not ok:
                print(f"   [{i}] панель не найдена, пробую wheel fallback")
                await _fallback_wheel_scroll(page)
            else:
                print(f"   [{i}] scroll ok")

            await page.wait_for_timeout(SCROLL_PAUSE_MS)

        await page.wait_for_timeout(2000)
        await browser.close()

    print(f"\nГотово. Сохранено: {saved_count} файлов")
    print(f"Папка: {SAMPLES_DIR.resolve()}")

    if saved_count == 0:
        print(
            "\nЕсли снова 0 — пришлите строки [api] из консоли.\n"
            "По ним видно: запросы не идут / блокируются / другой endpoint."
        )


if __name__ == "__main__":
    asyncio.run(run())
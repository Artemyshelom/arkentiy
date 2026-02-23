"""
Скрапер меню конкурентов.

Стратегии (применяются последовательно, остановка при результате):
  1. requests + BeautifulSoup (поиск встроенного JSON в <script>)
  2. Playwright headless (для SPA/JS-сайтов)
     2a. JS-экстракция структурированных данных из window.*
     2b. DOM-обход: поиск карточек товаров по CSS-паттернам
     2c. Текстовый fallback: поиск пар имя+цена по тексту страницы

Добавить новый сайт = добавить блок в competitors.json (код не менять).
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Таймауты
HTTP_TIMEOUT = 30
PW_GOTO_TIMEOUT = 60_000   # ms
PW_IDLE_TIMEOUT = 15_000   # ms
PW_EXTRA_WAIT = 2.5        # секунд ожидания после networkidle

# Границы разумных цен в рублях
PRICE_MIN = 50
PRICE_MAX = 50_000

# Паттерны имён которые не являются блюдами
_BAD_NAME_RE = re.compile(
    r"^[-\d]*%"                                          # "-50%", "50%", "%"
    r"|^(new\b|новинк|акци|хит|от$|до$|или$|цена|бесплатн)",
    re.IGNORECASE,
)


def _is_valid_name(name: str) -> bool:
    """Проверяет что строка — реальное название блюда, а не промо-бейдж или служебное слово."""
    if len(name) < 3:          # "от", "до" — 2 символа
        return False
    if _BAD_NAME_RE.match(name):
        return False
    return True


@dataclass
class MenuItem:
    name: str
    price: float
    category: str | None = None
    price_old: float | None = None
    portion: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "price": self.price,
            "category": self.category,
            "price_old": self.price_old,
            "portion": self.portion,
        }


async def scrape_competitor(competitor: dict) -> list[MenuItem]:
    """
    Главная точка входа. Возвращает список позиций меню.
    competitor = {"name": "...", "url": "...", "parser": "playwright"|"css", ...}
    """
    url = competitor["url"]
    name = competitor["name"]
    parser = competitor.get("parser", "playwright")

    logger.info(f"[Конкуренты] Парсим {name} ({url}), режим={parser}")

    items: list[MenuItem] = []

    # Сначала пробуем лёгкий requests-парсер (ищет JSON в HTML)
    if parser == "css":
        selectors = competitor.get("selectors", {})
        items = await _scrape_requests(url, selectors)

    # Playwright с секционным извлечением (section → категория → карточки)
    if not items and competitor.get("section_selector"):
        items = await _scrape_playwright_sections(url, competitor)

    # Playwright с точными селекторами карточек (без категорий по секциям)
    if not items and competitor.get("card_selector"):
        items = await _scrape_playwright_selectors(url, competitor)

    # Playwright generic — для всего что не распарсилось выше
    if not items:
        items = await _scrape_playwright(url)

    logger.info(f"[Конкуренты] {name}: найдено {len(items)} позиций")
    return items


# ---------------------------------------------------------------------------
# Strategy 1: requests + BeautifulSoup
# ---------------------------------------------------------------------------

async def _scrape_requests(url: str, selectors: dict) -> list[MenuItem]:
    """Лёгкий парсер для статических/SSR сайтов."""
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug(f"requests: статус {resp.status_code} для {url}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")

            # Приоритет: встроенный JSON > CSS-селекторы
            items = _extract_embedded_json(soup)
            if items:
                return items

            if selectors:
                items = _extract_with_selectors(soup, selectors)

            return items
    except Exception as e:
        logger.debug(f"requests scrape failed для {url}: {e}")
        return []


def _extract_embedded_json(soup: BeautifulSoup) -> list[MenuItem]:
    """Ищет данные меню в <script> тегах (window.__NUXT__, window.__DATA__ и т.д.)."""
    patterns = [
        r'"products"\s*:\s*(\[.{20,}?\])',
        r'"items"\s*:\s*(\[.{20,}?\])',
        r'"catalog"\s*:\s*(\[.{20,}?\])',
        r'"goods"\s*:\s*(\[.{20,}?\])',
        r'"menu"\s*:\s*(\[.{20,}?\])',
        r'"dishes"\s*:\s*(\[.{20,}?\])',
    ]
    for script in soup.find_all("script"):
        text = script.string or ""
        if len(text) < 50 or len(text) > 2_000_000:
            continue
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    items = _parse_json_array(data)
                    if len(items) >= 3:
                        return items
                except (json.JSONDecodeError, Exception):
                    pass
    return []


def _parse_json_array(data: list) -> list[MenuItem]:
    """Пробует распарсить JSON-массив как список товаров меню."""
    if not isinstance(data, list):
        return []
    items = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        name = (
            obj.get("name") or obj.get("title") or obj.get("label")
            or obj.get("product_name") or obj.get("itemName")
        )
        price_raw = (
            obj.get("price") or obj.get("price_rub") or obj.get("cost")
            or obj.get("priceRub") or obj.get("amount")
        )
        if not name or price_raw is None:
            continue
        name = str(name).strip()
        if not _is_valid_name(name):
            continue
        try:
            price = float(str(price_raw).replace(" ", "").replace(",", ".").replace("₽", ""))
        except (ValueError, TypeError):
            continue
        if not (PRICE_MIN <= price <= PRICE_MAX):
            continue

        price_old_raw = obj.get("price_old") or obj.get("oldPrice") or obj.get("priceOld")
        price_old = None
        if price_old_raw:
            try:
                price_old = float(str(price_old_raw).replace(" ", "").replace(",", "."))
                if not (PRICE_MIN <= price_old <= PRICE_MAX):
                    price_old = None
            except (ValueError, TypeError):
                pass

        items.append(MenuItem(
            name=str(name).strip()[:200],
            price=price,
            price_old=price_old,
            category=str(obj.get("category") or obj.get("group") or ""),
            portion=str(obj.get("weight") or obj.get("portion") or obj.get("size") or ""),
        ))
    return items


def _extract_with_selectors(soup: BeautifulSoup, selectors: dict) -> list[MenuItem]:
    """Извлекает позиции по CSS-селекторам из конфига."""
    items = []
    container_sel = selectors.get("container")
    name_sel = selectors.get("item_name")
    price_sel = selectors.get("price")
    category_sel = selectors.get("category")
    portion_sel = selectors.get("portion")

    if not container_sel or not name_sel or not price_sel:
        return []

    containers = soup.select(container_sel)
    for container in containers:
        name_el = container.select_one(name_sel)
        price_el = container.select_one(price_sel)
        if not name_el or not price_el:
            continue
        name = name_el.get_text(strip=True)
        price_text = price_el.get_text(strip=True)
        price = _parse_price_text(price_text)
        if not name or price is None:
            continue

        category = None
        if category_sel:
            cat_el = soup.select_one(category_sel)
            category = cat_el.get_text(strip=True) if cat_el else None

        portion = None
        if portion_sel:
            por_el = container.select_one(portion_sel)
            portion = por_el.get_text(strip=True) if por_el else None

        items.append(MenuItem(name=name[:200], price=price, category=category, portion=portion))
    return items


def _parse_price_text(text: str) -> float | None:
    """Извлекает цену из строки типа '490 ₽' или '490 руб.'."""
    m = re.search(r"(\d[\d\s]*)\s*[₽р]", text, re.IGNORECASE)
    if not m:
        return None
    try:
        price = float(m.group(1).replace("\xa0", "").replace(" ", ""))
        return price if PRICE_MIN <= price <= PRICE_MAX else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Strategy 2a: Playwright + точные CSS-селекторы из конфига
# ---------------------------------------------------------------------------

async def _scrape_playwright_selectors(url: str, competitor: dict) -> list[MenuItem]:
    """
    Playwright с конкретными CSS-селекторами карточек из competitors.json.
    Используется когда в конфиге задан card_selector (напр. Суши Даром).
    """
    card_sel = competitor.get("card_selector", "")
    name_sel = competitor.get("name_selector", "")
    price_sel = competitor.get("price_selector", "")
    old_price_sel = competitor.get("old_price_selector", "")
    portion_sel = competitor.get("portion_selector", "")

    if not card_sel or not name_sel or not price_sel:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright не установлен.")
        return []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
            )
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}", lambda r: r.abort())

            try:
                await page.goto(url, timeout=PW_GOTO_TIMEOUT, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=PW_IDLE_TIMEOUT)
                except Exception:
                    pass
                await asyncio.sleep(PW_EXTRA_WAIT)
                await _scroll_page(page)

                js_code = f"""
                () => {{
                    const PRICE_MIN = {PRICE_MIN}, PRICE_MAX = {PRICE_MAX};
                    const results = [];
                    const seen = new Set();

                    function parsePrice(text) {{
                        if (!text) return null;
                        const m = text.match(/(\\d[\\d\\s\\u00a0]*)[\\u20bd\\u0440]/);
                        if (!m) return null;
                        const p = parseFloat(m[1].replace(/[\\s\\u00a0]/g, ''));
                        return (p >= PRICE_MIN && p <= PRICE_MAX) ? p : null;
                    }}

                    const cards = document.querySelectorAll({repr(card_sel)});
                    for (const card of cards) {{
                        const nameEl = card.querySelector({repr(name_sel)});
                        const priceEl = card.querySelector({repr(price_sel)});
                        if (!nameEl || !priceEl) continue;

                        const name = nameEl.innerText?.trim() || '';
                        const price = parsePrice(priceEl.innerText);
                        if (!name || !price) continue;
                        if (name.length < 3) continue;
                        if (/^[-\\d]*%/.test(name)) continue;
                        if (/^(new\\b|новинк|акци|хит|от$|до$)/i.test(name)) continue;

                        let priceOld = null;
                        {'const oldEl = card.querySelector(' + repr(old_price_sel) + '); if (oldEl) { priceOld = parsePrice(oldEl.innerText); }' if old_price_sel else ''}

                        let portion = null;
                        {'const portionEl = card.querySelector(' + repr(portion_sel) + '); if (portionEl) { const pt = portionEl.innerText?.trim(); if (pt && pt !== name) portion = pt; }' if portion_sel else ''}

                        const key = name + '|' + price;
                        if (!seen.has(key)) {{
                            seen.add(key);
                            results.push({{ name, price, priceOld, portion }});
                        }}
                    }}
                    return results;
                }}
                """
                data = await page.evaluate(js_code)
                items = _js_result_to_items(data)
                logger.info(f"[playwright_selectors] {competitor.get('name')}: {len(items)} позиций")
                return items

            finally:
                await browser.close()

    except Exception as e:
        logger.error(f"[playwright_selectors] Ошибка для {url}: {e}", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Strategy 2b: Playwright + секционное извлечение (section → категория → карточки)
# ---------------------------------------------------------------------------

async def _scrape_playwright_sections(url: str, competitor: dict) -> list[MenuItem]:
    """
    Playwright с секционным извлечением: итерирует section-элементы,
    извлекает категорию из заголовка секции и блюда из карточек внутри.
    Используется когда в конфиге задан section_selector.
    Селекторы инлайнятся прямо в JS чтобы избежать ограничений page.evaluate().
    """
    section_sel = competitor.get("section_selector", "section")
    cat_sel = competitor.get("category_selector", "")
    card_sel = competitor.get("card_selector", "")
    name_sel = competitor.get("name_selector", "")
    price_sel = competitor.get("price_selector", "")
    old_price_sel = competitor.get("old_price_selector", "")
    portion_sel = competitor.get("portion_selector", "")

    if not card_sel or not name_sel or not price_sel:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright не установлен.")
        return []

    # Строим JS с инлайн-подстановкой селекторов
    js_code = f"""
    () => {{
        var PRICE_MIN = {PRICE_MIN}, PRICE_MAX = {PRICE_MAX};
        var results = [];
        var seen = {{}};

        function getPrice(el) {{
            if (!el) return null;
            if (el.tagName === 'META') {{
                var cv = parseFloat(el.getAttribute('content') || '');
                return (cv >= PRICE_MIN && cv <= PRICE_MAX) ? cv : null;
            }}
            var text = el.innerText || el.textContent || '';
            var m = text.replace(/[\\s\\u00a0]/g, '').match(/([\\d]+(?:[.,]\\d+)?)[\\u20bd\\u0440]/);
            if (!m) return null;
            var p = parseFloat(m[1].replace(',', '.'));
            return (p >= PRICE_MIN && p <= PRICE_MAX) ? p : null;
        }}

        var sections = document.querySelectorAll({repr(section_sel)});
        for (var si = 0; si < sections.length; si++) {{
            var section = sections[si];
            var catEl = {repr(cat_sel)} ? section.querySelector({repr(cat_sel)}) : null;
            var category = catEl ? (catEl.innerText || catEl.textContent || '').trim() : '';

            var cards = section.querySelectorAll({repr(card_sel)});
            for (var ci = 0; ci < cards.length; ci++) {{
                var card = cards[ci];
                var nameEl = card.querySelector({repr(name_sel)});
                var priceEl = card.querySelector({repr(price_sel)});
                if (!nameEl || !priceEl) continue;

                var name = (nameEl.innerText || nameEl.textContent || '').trim();
                if (!name || name.length < 3) continue;
                if (/^[-\\d]*%/.test(name)) continue;
                if (/^(new\\b|новинк|акци|хит|от$|до$|цена|бесплатн)/i.test(name)) continue;
                if (name === category) continue;

                var price = getPrice(priceEl);
                if (!price) continue;

                var priceOld = null;
                {'var oldEl = card.querySelector(' + repr(old_price_sel) + '); if (oldEl) priceOld = getPrice(oldEl);' if old_price_sel else ''}

                var portion = null;
                {'var portEl = card.querySelector(' + repr(portion_sel) + '); if (portEl) {{ var pt = (portEl.innerText || portEl.textContent || "").trim(); if (pt && pt !== name) portion = pt; }}' if portion_sel else ''}

                var key = name + '|' + price;
                if (!seen[key]) {{
                    seen[key] = true;
                    results.push({{ name: name, price: price, priceOld: priceOld, portion: portion, category: category }});
                }}
            }}
        }}
        return results;
    }}
    """

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
            )
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}", lambda r: r.abort())

            try:
                await page.goto(url, timeout=PW_GOTO_TIMEOUT, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=PW_IDLE_TIMEOUT)
                except Exception:
                    pass
                await asyncio.sleep(PW_EXTRA_WAIT)
                await _scroll_page(page)

                data = await page.evaluate(js_code)
                items = _js_result_to_items(data)
                logger.info(
                    f"[playwright_sections] {competitor.get('name')}: "
                    f"{len(items)} позиций, "
                    f"{len(set(i.category for i in items if i.category))} категорий"
                )
                return items

            finally:
                await browser.close()

    except Exception as e:
        logger.error(f"[playwright_sections] Ошибка для {url}: {e}", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Strategy 2c: Playwright generic (DOM-обход + текстовый fallback)
# ---------------------------------------------------------------------------

async def _scrape_playwright(url: str) -> list[MenuItem]:
    """Playwright headless Chromium — для SPA/JS-сайтов."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright не установлен. Добавь в requirements.txt и пересобери контейнер.")
        return []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ru-RU",
            )
            page = await context.new_page()
            # Не грузим картинки — ускоряет парсинг
            await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}", lambda r: r.abort())

            try:
                await page.goto(url, timeout=PW_GOTO_TIMEOUT, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=PW_IDLE_TIMEOUT)
                except Exception:
                    pass
                await asyncio.sleep(PW_EXTRA_WAIT)

                # Прокрутка для lazy-loaded контента
                await _scroll_page(page)

                # Попытка 1: структурированные JS-данные
                items = await _pw_extract_js_data(page)
                if items:
                    return items

                # Попытка 2: DOM-обход карточек
                items = await _pw_extract_dom(page)
                if items:
                    return items

                # Попытка 3: текстовый fallback
                items = await _pw_extract_text(page)
                return items

            finally:
                await browser.close()

    except Exception as e:
        logger.error(f"[Playwright] Ошибка для {url}: {e}", exc_info=True)
        return []


async def _scroll_page(page) -> None:
    """Прокручивает страницу вниз для загрузки lazy-content."""
    try:
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.5)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def _pw_extract_js_data(page) -> list[MenuItem]:
    """Ищет данные меню в глобальных JS-переменных (window.__NUXT__ и т.д.)."""
    try:
        raw = await page.evaluate("""
        () => {
            const candidates = [
                window.__NUXT__,
                window.__INITIAL_STATE__,
                window.__DATA__,
                window.APP_STATE,
                window.__STORE__,
                window.initialData,
            ];
            for (const c of candidates) {
                if (!c) continue;
                try {
                    const s = JSON.stringify(c);
                    if ((s.includes('"price"') || s.includes('"cost"')) && s.includes('"name"')) {
                        return s;
                    }
                } catch {}
            }
            // Также смотрим <script> теги без src
            for (const script of document.querySelectorAll('script:not([src])')) {
                const t = script.textContent || '';
                if (t.length < 100 || t.length > 2_000_000) continue;
                if ((t.includes('"price"') || t.includes('"cost"')) && t.includes('"name"')) {
                    return t;
                }
            }
            return null;
        }
        """)
        if not raw:
            return []

        # Ищем JSON-массивы с товарами
        for pattern in [
            r'"products"\s*:\s*(\[.{20,}?\])',
            r'"items"\s*:\s*(\[.{20,}?\])',
            r'"catalog"\s*:\s*(\[.{20,}?\])',
            r'"goods"\s*:\s*(\[.{20,}?\])',
            r'"dishes"\s*:\s*(\[.{20,}?\])',
        ]:
            for match in re.finditer(pattern, raw, re.DOTALL):
                try:
                    data = json.loads(match.group(1))
                    items = _parse_json_array(data)
                    if len(items) >= 3:
                        return items
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"JS-data extraction failed: {e}")
    return []


# JS для DOM-обхода (выполняется в браузере)
_DOM_EXTRACTOR_JS = """
() => {
    const PRICE_MIN = 50, PRICE_MAX = 50000;
    const results = [];
    const seen = new Set();

    function parsePrice(text) {
        if (!text) return null;
        const m = text.match(/(\\d[\\d\\s\\u00a0]*)[\\u20bd\\u0440]/);
        if (!m) return null;
        const p = parseFloat(m[1].replace(/[\\s\\u00a0]/g, ''));
        return (p >= PRICE_MIN && p <= PRICE_MAX) ? p : null;
    }

    function cleanName(text) {
        if (!text || text.length < 2 || text.length > 200) return null;
        const cleaned = text
            .replace(/(\\d[\\d\\s\\u00a0]*)[\\u20bd\\u0440][\\u0431]?/g, '')
            .replace(/\\d+\\s*(г|гр|мл|шт|кг|л)\\b/gi, '')
            .replace(/\\s+/g, ' ')
            .trim();
        return cleaned.length > 1 ? cleaned : null;
    }

    // Карточки товаров по типичным CSS-классам
    const cardPatterns = [
        '[class*="product"]', '[class*="menu-item"]', '[class*="catalog-item"]',
        '[class*="dish"]', '[class*="-card"]', '[class*="food-item"]',
        '[class*="item-card"]', '[class*="goods-item"]', '[class*="menu__item"]',
        '[class*="catalogue__item"]', '[class*="product__item"]',
    ];

    let containers = [];
    for (const sel of cardPatterns) {
        const els = document.querySelectorAll(sel);
        if (els.length >= 3) { containers = Array.from(els); break; }
    }

    for (const card of containers) {
        const fullText = card.innerText?.trim() || '';
        if (!fullText || fullText.length > 800) continue;

        const price = parsePrice(fullText);
        if (!price) continue;

        // Имя: ищем h1-h6 или элементы с 'title'/'name' в классе
        let name = null;
        const nameEls = card.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="title"],[class*="name"],[class*="label"]');
        for (const el of nameEls) {
            const t = cleanName(el.innerText?.trim());
            if (t && parsePrice(t) === null) { name = t; break; }
        }
        if (!name) name = cleanName(fullText);

        // Старая цена (зачёркнутая)
        let priceOld = null;
        const prices = (fullText.match(/(\\d[\\d\\s\\u00a0]*)[\\u20bd\\u0440]/g) || [])
            .map(t => parseFloat(t.replace(/[^\\d]/g, '')))
            .filter(p => p >= PRICE_MIN && p <= PRICE_MAX);
        if (prices.length >= 2) {
            const higher = Math.max(...prices);
            if (higher !== price) priceOld = higher;
        }

        // Граммовка
        const portionM = fullText.match(/(\\d+\\s*(г|гр|мл|шт|кг|л)\\b)/i);
        const portion = portionM ? portionM[1] : null;

        if (name) {
            if (name.length < 3) continue;
            if (/^[-\\d]*%/.test(name)) continue;
            if (/^(new\\b|новинк|акци|хит|от$|до$|цена|бесплатн)/i.test(name)) continue;
            const key = name + '|' + price;
            if (!seen.has(key)) { seen.add(key); results.push({ name, price, priceOld, portion }); }
        }
    }
    return results;
}
"""

# JS для текстового fallback
_TEXT_EXTRACTOR_JS = """
() => {
    const PRICE_MIN = 50, PRICE_MAX = 50000;
    const results = [];
    const seen = new Set();

    function parsePrice(text) {
        const m = text?.match(/(\\d[\\d\\s\\u00a0]*)[\\u20bd\\u0440]/);
        if (!m) return null;
        const p = parseFloat(m[1].replace(/[\\s\\u00a0]/g, ''));
        return (p >= PRICE_MIN && p <= PRICE_MAX) ? p : null;
    }

    const lines = (document.body?.innerText || '')
        .split('\\n')
        .map(l => l.trim())
        .filter(l => l.length > 0 && l.length < 250);

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        // Строка цены: короткая и содержит цену
        if (line.length > 30) continue;
        const price = parsePrice(line);
        if (!price) continue;

        // Ищем имя в предыдущих строках
        for (let j = i - 1; j >= Math.max(0, i - 6); j--) {
            const cand = lines[j];
            if (cand.length < 2 || cand.length > 150) continue;
            if (parsePrice(cand) !== null) continue;
            if (/^\\d+\\s*(г|гр|мл|шт|кг|л)\\b/i.test(cand)) continue;
            if (/^[-\\d]*%/.test(cand)) continue;
            if (/^(new\\b|новинк|акци|хит|от$|до$|цена|бесплатн)/i.test(cand)) continue;
            if (cand.length < 3) continue;

            const key = cand + '|' + price;
            if (!seen.has(key)) {
                seen.add(key);
                results.push({ name: cand, price, priceOld: null, portion: null });
            }
            break;
        }
    }
    return results;
}
"""


async def _pw_extract_dom(page) -> list[MenuItem]:
    """DOM-обход: ищет карточки товаров по CSS-паттернам."""
    try:
        data = await page.evaluate(_DOM_EXTRACTOR_JS)
        return _js_result_to_items(data)
    except Exception as e:
        logger.debug(f"DOM extraction failed: {e}")
        return []


async def _pw_extract_text(page) -> list[MenuItem]:
    """Текстовый fallback: цена + ближайший текст выше."""
    try:
        data = await page.evaluate(_TEXT_EXTRACTOR_JS)
        return _js_result_to_items(data)
    except Exception as e:
        logger.debug(f"Text extraction failed: {e}")
        return []


def _js_result_to_items(data: list | None) -> list[MenuItem]:
    if not data:
        return []
    items = []
    for obj in data:
        try:
            name = str(obj.get("name", "")).strip()[:200]
            price = float(obj["price"])
            price_old_raw = obj.get("priceOld")
            price_old = float(price_old_raw) if price_old_raw else None
            portion = str(obj["portion"]).strip() if obj.get("portion") else None
            category = str(obj["category"]).strip() if obj.get("category") else None
            if name and _is_valid_name(name) and PRICE_MIN <= price <= PRICE_MAX:
                items.append(MenuItem(
                    name=name, price=price, price_old=price_old,
                    portion=portion, category=category,
                ))
        except (KeyError, TypeError, ValueError):
            pass
    return items

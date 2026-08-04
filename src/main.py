from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from apify import Actor
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


MONEY_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*"
    r"(?:₽|руб(?:\.|лей|ля)?|р\.)",
    re.I,
)
AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2)\b", re.I)
VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*м(?:³|3)\b", re.I)
PIECES_RE = re.compile(r"(\d+)\s*(?:шт\.?|плит(?:а|ы)?|лист(?:а|ов)?)", re.I)
TRIPLE_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:x|х|×|\*|kh|h)\s*"
    r"(\d{1,4})\s*(?:x|х|×|\*|kh|h)\s*(\d{1,4})(?!\d)",
    re.I,
)
SKU_RE = re.compile(r"(?:артикул|код\s*товара|sku)\s*[:№]?\s*([\w-]{4,})", re.I)
CAPTCHA_RE = re.compile(
    r"\bcaptcha\b|капч|я\s+не\s+робот|подтвердите,\s*что\s*вы\s+человек|"
    r"проверка\s+безопасности|cloudflare|challenge-platform",
    re.I,
)
BLOCKED_RE = re.compile(
    r"403\s+forbidden|access\s+denied|доступ\s+запрещ[её]н|доступ\s+ограничен",
    re.I,
)
HYPERLINK_FORMULA_RE = re.compile(
    r'^=HYPERLINK\(\s*["\']([^"\']+)["\']',
    re.I,
)


@dataclass
class ProductInput:
    name: str
    product_url: str
    thickness_mm: int | None
    quantity: float | None
    unit: str
    length_mm: float | None = None
    width_mm: float | None = None
    pieces_per_package: int | None = None
    price_unit: str = "package"


@dataclass
class StoreInput:
    name: str
    url: str
    city: str | None
    products: list[ProductInput]
    use_proxy: bool = False


@dataclass
class ProductResult:
    store_name: str
    store_url: str
    city: str | None
    requested_name: str
    requested_thickness_mm: int | None
    requested_quantity: float | None
    requested_unit: str
    product_url: str
    status: str
    found_name: str | None = None
    sku: str | None = None
    seller: str | None = None
    availability: str | None = None
    package_price: float | None = None
    currency: str = "RUB"
    price_unit: str = "package"
    pieces_per_package: int | None = None
    length_mm: float | None = None
    width_mm: float | None = None
    thickness_mm: float | None = None
    area_per_package_m2: float | None = None
    volume_per_package_m3: float | None = None
    price_per_piece: float | None = None
    price_per_m2: float | None = None
    price_per_m3: float | None = None
    required_packages: int | None = None
    total_quantity: float | None = None
    total_cost: float | None = None
    checked_at: str = ""
    price_source: str | None = None
    city_selected: bool = False
    http_status: int | None = None
    error: str | None = None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?", str(value))
    if not match:
        return None
    return float(match.group(0).replace(" ", "").replace("\u00a0", "").replace(",", "."))


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_label(value: Any) -> str:
    return normalize_text(value).lower().replace("ё", "е")


def safe_key(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return normalized[:45] or "ITEM"


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


def extract_spreadsheet_id(url_or_id: str) -> str:
    value = normalize_text(url_or_id)
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise ValueError("Не удалось определить ID Google Таблицы из googleSheetUrl")


def _download_google_sheet_json(
    spreadsheet_id: str,
    api_key: str,
    sheet_name: str | None,
) -> dict[str, Any]:
    fields = (
        "sheets(properties(title,gridProperties(rowCount,columnCount)),"
        "data(startRow,startColumn,rowData(values(formattedValue,effectiveValue,"
        "userEnteredValue,hyperlink,textFormatRuns))))"
    )
    params: list[tuple[str, str]] = [
        ("includeGridData", "true"),
        ("fields", fields),
        ("key", api_key),
    ]
    if sheet_name:
        params.append(("ranges", f"'{sheet_name.replace(chr(39), chr(39) * 2)}'"))

    endpoint = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id)}?"
        + urlencode(params)
    )
    request = Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "User-Agent": "Apify-price-monitor/1.0",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Google Sheets API вернул HTTP {exc.code}: {body[:500]}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Не удалось подключиться к Google Sheets API: {exc}") from exc


async def download_google_sheet_json(
    spreadsheet_id: str,
    api_key: str,
    sheet_name: str | None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _download_google_sheet_json,
        spreadsheet_id,
        api_key,
        sheet_name,
    )


def cell_text(cell: dict[str, Any] | None) -> str:
    if not cell:
        return ""
    formatted = cell.get("formattedValue")
    if formatted is not None:
        return normalize_text(formatted)
    effective = cell.get("effectiveValue") or {}
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in effective:
            return normalize_text(effective[key])
    return ""


def cell_url(cell: dict[str, Any] | None) -> str | None:
    if not cell:
        return None

    hyperlink = normalize_text(cell.get("hyperlink"))
    if hyperlink.startswith(("http://", "https://")):
        return hyperlink

    for run in cell.get("textFormatRuns") or []:
        uri = normalize_text((((run or {}).get("format") or {}).get("link") or {}).get("uri"))
        if uri.startswith(("http://", "https://")):
            return uri

    entered = cell.get("userEnteredValue") or {}
    formula = normalize_text(entered.get("formulaValue"))
    formula_match = HYPERLINK_FORMULA_RE.match(formula)
    if formula_match:
        return formula_match.group(1)

    for candidate in (
        cell_text(cell),
        entered.get("stringValue"),
        (cell.get("effectiveValue") or {}).get("stringValue"),
    ):
        candidate_text = normalize_text(candidate)
        if candidate_text.startswith(("http://", "https://")):
            return candidate_text
    return None


def sheet_to_matrix(sheet: dict[str, Any]) -> list[list[dict[str, Any]]]:
    properties = sheet.get("properties") or {}
    grid = properties.get("gridProperties") or {}
    row_count = int(grid.get("rowCount") or 0)
    column_count = int(grid.get("columnCount") or 0)

    for block in sheet.get("data") or []:
        start_row = int(block.get("startRow") or 0)
        start_col = int(block.get("startColumn") or 0)
        row_data = block.get("rowData") or []
        row_count = max(row_count, start_row + len(row_data))
        for row in row_data:
            column_count = max(column_count, start_col + len((row or {}).get("values") or []))

    matrix: list[list[dict[str, Any]]] = [
        [{} for _ in range(column_count)] for _ in range(row_count)
    ]
    for block in sheet.get("data") or []:
        start_row = int(block.get("startRow") or 0)
        start_col = int(block.get("startColumn") or 0)
        for row_offset, row in enumerate(block.get("rowData") or []):
            for col_offset, value in enumerate((row or {}).get("values") or []):
                matrix[start_row + row_offset][start_col + col_offset] = value or {}
    return matrix


def first_nonempty_text(row: list[dict[str, Any]], start_col: int = 0) -> str:
    for cell in row[start_col:]:
        value = cell_text(cell)
        if value:
            return value
    return ""


def row_cell_text(matrix: list[list[dict[str, Any]]], row: int, col: int) -> str:
    if row < 0 or row >= len(matrix) or col < 0 or col >= len(matrix[row]):
        return ""
    return cell_text(matrix[row][col])


def detect_city_blocks(matrix: list[list[dict[str, Any]]]) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for row_index in range(max(0, len(matrix) - 4)):
        labels = [normalize_label(row_cell_text(matrix, row_index + offset, 0)) for offset in range(1, 5)]
        if labels != ["бренд", "материал", "толщина", "магазин"]:
            continue
        city = first_nonempty_text(matrix[row_index], start_col=1)
        if city:
            blocks.append((row_index, city))
    return blocks


def packaging_defaults(material: str, thickness_mm: int | None, price_unit: str) -> tuple[float | None, float | None, int | None]:
    key = normalize_label(material)
    thickness = int(thickness_mm or 0)

    if any(token in key for token in ("техноплекс", "carbon eco", "carbon prof")):
        length, width = 1180.0, 580.0
        pieces = 8 if thickness == 50 else 4 if thickness == 100 else None
    elif any(token in key for token in ("теплекс", "комфорт", "фундамент")):
        length, width = 1185.0, 585.0
        if "теплекс" in key:
            pieces = 8 if thickness == 50 else 4 if thickness == 100 else None
        else:
            pieces = 7 if thickness == 50 else 4 if thickness == 100 else None
    else:
        return None, None, None

    if price_unit == "sheet":
        pieces = 1
    return length, width, pieces


def infer_price_unit(store_name: str, product_url: str) -> str:
    host = urlparse(product_url).hostname or ""
    if "megastroy.com" in host or "мегастрой" in normalize_label(store_name):
        return "sheet"
    return "package"


def should_use_proxy(store_name: str, store_url: str, proxy_stores: list[str]) -> bool:
    haystack = f"{store_name} {store_url}".lower()
    return any(normalize_label(value) in haystack for value in proxy_stores if normalize_text(value))


def parse_google_sheet_stores(
    payload: dict[str, Any],
    selected_cities: list[str],
    quantity_m2: float,
    proxy_stores: list[str],
    sheet_name: str | None,
) -> list[StoreInput]:
    selected = {normalize_label(city) for city in selected_cities if normalize_text(city)}
    stores: list[StoreInput] = []

    available_sheets = payload.get("sheets") or []
    if sheet_name:
        available_sheets = [
            sheet
            for sheet in available_sheets
            if normalize_label((sheet.get("properties") or {}).get("title")) == normalize_label(sheet_name)
        ]
        if not available_sheets:
            raise ValueError(f"Лист Google Таблицы '{sheet_name}' не найден")

    for sheet in available_sheets:
        title = normalize_text((sheet.get("properties") or {}).get("title"))
        matrix = sheet_to_matrix(sheet)
        blocks = detect_city_blocks(matrix)
        Actor.log.info("Лист %s: найдено городских блоков=%d", title, len(blocks))

        for block_position, (city_row, city) in enumerate(blocks):
            if selected and normalize_label(city) not in selected:
                continue

            next_city_row = blocks[block_position + 1][0] if block_position + 1 < len(blocks) else len(matrix)
            brand_row = city_row + 1
            material_row = city_row + 2
            thickness_row = city_row + 3
            data_start_row = city_row + 5

            max_cols = max((len(row) for row in matrix), default=0)
            column_meta: dict[int, tuple[str, str, int]] = {}
            current_brand = ""
            current_material = ""
            for col in range(2, max_cols):
                brand_value = row_cell_text(matrix, brand_row, col)
                material_value = row_cell_text(matrix, material_row, col)
                if brand_value:
                    current_brand = brand_value
                if material_value:
                    current_material = material_value

                thickness_number = parse_number(row_cell_text(matrix, thickness_row, col))
                if not current_material or thickness_number is None:
                    continue
                thickness = int(round(thickness_number))
                if thickness not in (50, 100):
                    continue
                column_meta[col] = (current_brand, current_material, thickness)

            for row_index in range(data_start_row, next_city_row):
                store_name = row_cell_text(matrix, row_index, 0)
                if not store_name:
                    continue
                if normalize_label(store_name) in {"магазин", "бренд", "материал", "толщина"}:
                    continue

                store_url = None
                if row_index < len(matrix) and len(matrix[row_index]) > 1:
                    store_url = cell_url(matrix[row_index][1]) or row_cell_text(matrix, row_index, 1) or None

                products: list[ProductInput] = []
                for col, (brand, material, thickness) in column_meta.items():
                    if row_index >= len(matrix) or col >= len(matrix[row_index]):
                        continue
                    product_url = cell_url(matrix[row_index][col])
                    if not product_url:
                        continue

                    price_unit = infer_price_unit(store_name, product_url)
                    length, width, pieces = packaging_defaults(material, thickness, price_unit)
                    requested_name = normalize_text(f"{brand} {material}")
                    products.append(
                        ProductInput(
                            name=requested_name,
                            product_url=product_url,
                            thickness_mm=thickness,
                            quantity=quantity_m2,
                            unit="m2",
                            length_mm=length,
                            width_mm=width,
                            pieces_per_package=pieces,
                            price_unit=price_unit,
                        )
                    )
                    if not store_url:
                        parsed = urlparse(product_url)
                        if parsed.scheme and parsed.netloc:
                            store_url = f"{parsed.scheme}://{parsed.netloc}/"

                if not products:
                    continue

                store_url = store_url or products[0].product_url
                stores.append(
                    StoreInput(
                        name=store_name,
                        url=store_url,
                        city=city,
                        products=products,
                        use_proxy=should_use_proxy(store_name, store_url, proxy_stores),
                    )
                )
                Actor.log.info(
                    "Google Таблица: город=%s, магазин=%s, товаров=%d, proxy=%s",
                    city,
                    store_name,
                    len(products),
                    stores[-1].use_proxy,
                )

    if selected:
        found_cities = {normalize_label(store.city) for store in stores if store.city}
        missing = sorted(city for city in selected if city not in found_cities)
        if missing:
            Actor.log.warning("В таблице не найдены выбранные города: %s", ", ".join(missing))
    return stores


# ---------------------------------------------------------------------------
# Playwright extraction
# ---------------------------------------------------------------------------


async def body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=12_000)
    except Exception:
        return ""


async def click_first(page: Page, selectors: list[str], timeout: int = 1300) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=timeout):
                await locator.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def dismiss_popups(page: Page) -> None:
    await click_first(
        page,
        [
            "button:has-text('Принять')",
            "button:has-text('Согласен')",
            "button:has-text('Хорошо')",
            "button:has-text('Понятно')",
            "button:has-text('Закрыть')",
            "[aria-label='Закрыть']",
            "[aria-label='Close']",
        ],
        timeout=800,
    )


async def select_city(page: Page, city: str | None) -> bool:
    if not city:
        return False
    current_text = await body_text(page)
    if city.lower() in current_text.lower():
        return True

    opened = await click_first(
        page,
        [
            "button:has-text('Выбрать город')",
            "button:has-text('Укажите город')",
            "a:has-text('Выбрать город')",
            "[class*='region'] button",
            "[class*='city'] button",
            "[data-testid*='city']",
            "[data-testid*='region']",
        ],
        timeout=1800,
    )
    if not opened:
        return False

    await page.wait_for_timeout(800)
    for selector in [
        "input[placeholder*='город' i]",
        "input[placeholder*='насел' i]",
        "[role='dialog'] input",
        "input[type='search']",
    ]:
        field = page.locator(selector).last
        try:
            if not await field.is_visible(timeout=1000):
                continue
            await field.fill(city)
            await page.wait_for_timeout(900)
            option = page.get_by_text(city, exact=True).last
            if await option.is_visible(timeout=1500):
                await option.click()
            else:
                await field.press("Enter")
            await page.wait_for_timeout(1400)
            return city.lower() in (await body_text(page)).lower()
        except Exception:
            continue
    return False


async def json_ld_nodes(page: Page) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    scripts = page.locator("script[type='application/ld+json']")
    for index in range(await scripts.count()):
        try:
            raw = await scripts.nth(index).text_content()
            if raw:
                parsed = json.loads(raw)
                nodes.extend(node for node in walk_json(parsed) if isinstance(node, dict))
        except Exception:
            continue
    return nodes


async def meta(page: Page, selector: str, attribute: str = "content") -> str | None:
    try:
        value = await page.locator(selector).first.get_attribute(attribute, timeout=1000)
        return value.strip() if value else None
    except Exception:
        return None


def offer_from_product(product_node: dict[str, Any]) -> dict[str, Any]:
    offers = product_node.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        return next((item for item in offers if isinstance(item, dict)), {})
    return {}


async def extract_title(page: Page, product_node: dict[str, Any]) -> str | None:
    candidates: list[str | None] = [
        str(product_node.get("name")) if product_node.get("name") else None,
        await meta(page, "meta[property='og:title']"),
        await meta(page, "meta[name='twitter:title']"),
    ]
    try:
        candidates.append((await page.locator("h1").first.inner_text(timeout=2000)).strip())
    except Exception:
        pass
    try:
        candidates.append((await page.title()).strip())
    except Exception:
        pass
    for value in candidates:
        if value and value.lower() not in {"none", "null"}:
            return value
    return None


async def extract_price(
    page: Page,
    product_node: dict[str, Any],
    visible_text: str,
) -> tuple[float | None, str | None, str]:
    candidates: list[tuple[float, str]] = []
    offer = offer_from_product(product_node)
    currency = str(offer.get("priceCurrency") or "RUB")

    for key in ("price", "lowPrice", "highPrice"):
        parsed = parse_number(offer.get(key))
        if parsed:
            candidates.append((parsed, f"json-ld:{key}"))

    for selector in [
        "meta[property='product:price:amount']",
        "meta[itemprop='price']",
        "[itemprop='price']",
        "[data-price]",
    ]:
        attribute = "data-price" if selector == "[data-price]" else "content"
        parsed = parse_number(await meta(page, selector, attribute))
        if parsed:
            candidates.append((parsed, f"dom:{selector}"))

    for match in MONEY_RE.finditer(visible_text):
        parsed = parse_number(match.group(1))
        if parsed and 10 <= parsed <= 10_000_000:
            candidates.append((parsed, "visible-text"))

    if not candidates:
        return None, None, currency

    unique: dict[float, tuple[float, str]] = {}
    for value, source in candidates:
        unique.setdefault(round(value, 2), (value, source))

    ordered = list(unique.values())
    ordered.sort(
        key=lambda item: (
            0 if item[1].startswith("json-ld") else 1 if item[1].startswith("dom") else 2
        )
    )
    return ordered[0][0], ordered[0][1], currency


def infer_dimensions(
    text: str,
    url: str,
    item: ProductInput,
) -> tuple[float | None, float | None, float | None, int | None]:
    length = item.length_mm
    width = item.width_mm
    thickness = float(item.thickness_mm) if item.thickness_mm else None
    pieces = item.pieces_per_package

    combined = f"{text} {urlparse(url).path}".replace("-", " ")
    for triple in TRIPLE_RE.findall(combined):
        numbers = [float(value) for value in triple]
        requested = float(item.thickness_mm) if item.thickness_mm else None
        thickness_candidate = None
        if requested is not None:
            thickness_candidate = next((number for number in numbers if abs(number - requested) < 0.1), None)
        if thickness_candidate is None:
            small = [number for number in numbers if number <= 200]
            thickness_candidate = min(small) if small else min(numbers)

        faces = numbers.copy()
        try:
            faces.remove(thickness_candidate)
        except ValueError:
            pass
        if len(faces) == 2:
            length = length or max(faces)
            width = width or min(faces)
            thickness = thickness or thickness_candidate
            break

    # Значение из Google Таблицы имеет приоритет. По тексту ищем только когда оно неизвестно.
    if pieces is None:
        piece_match = PIECES_RE.search(text)
        if piece_match:
            value = int(piece_match.group(1))
            if 1 <= value <= 50:
                pieces = value

    return length, width, thickness, pieces


def calculate_packaging(result: ProductResult, item: ProductInput) -> None:
    area = result.area_per_package_m2
    volume = result.volume_per_package_m3
    price = result.package_price

    if price and result.pieces_per_package:
        result.price_per_piece = round(price / result.pieces_per_package, 2)
    if price and area:
        result.price_per_m2 = round(price / area, 2)
    if price and volume:
        result.price_per_m3 = round(price / volume, 2)

    if item.quantity is None:
        return
    capacity = (
        1.0
        if item.unit == "packages"
        else area
        if item.unit == "m2"
        else volume
        if item.unit == "m3"
        else None
    )
    if not capacity or capacity <= 0:
        return

    packages = math.ceil(item.quantity / capacity)
    result.required_packages = packages
    result.total_quantity = round(packages * capacity, 4)
    if price:
        result.total_cost = round(packages * price, 2)


async def save_debug_files(
    page: Page,
    html: str,
    store_index: int,
    product_index: int,
    thickness: int | None,
) -> None:
    prefix = f"S{store_index}_P{product_index}_T{thickness or 'NA'}"
    try:
        await Actor.set_value(
            f"{prefix}_SCREENSHOT",
            await page.screenshot(full_page=True),
            content_type="image/png",
        )
    except Exception as exc:
        Actor.log.warning("Не удалось сохранить скриншот: %s", exc)
    try:
        await Actor.set_value(
            f"{prefix}_HTML",
            html,
            content_type="text/html; charset=utf-8",
        )
    except Exception as exc:
        Actor.log.warning("Не удалось сохранить HTML: %s", exc)


async def inspect_product(
    page: Page,
    item: ProductInput,
    store: StoreInput,
    city_selected: bool,
    save_debug: bool,
    save_debug_on_success: bool,
    store_index: int,
    product_index: int,
    page_timeout_ms: int,
) -> ProductResult:
    result = ProductResult(
        store_name=store.name,
        store_url=store.url,
        city=store.city,
        requested_name=item.name,
        requested_thickness_mm=item.thickness_mm,
        requested_quantity=item.quantity,
        requested_unit=item.unit,
        product_url=item.product_url,
        status="error",
        checked_at=datetime.now(timezone.utc).isoformat(),
        city_selected=city_selected,
        price_unit=item.price_unit,
    )

    html = ""
    try:
        response = await page.goto(
            item.product_url,
            wait_until="domcontentloaded",
            timeout=page_timeout_ms,
        )
        result.http_status = response.status if response else None
        await page.wait_for_timeout(2200)
        await dismiss_popups(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass

        visible_text = await body_text(page)
        html = await page.content()
        nodes = await json_ld_nodes(page)
        product_nodes = [
            node
            for node in nodes
            if str(node.get("@type", "")).lower() == "product"
            or (
                isinstance(node.get("@type"), list)
                and "product" in [str(value).lower() for value in node.get("@type", [])]
            )
        ]
        product_node = product_nodes[0] if product_nodes else {}
        title = await extract_title(page, product_node)

        captcha_text = f"{title or ''}\n{visible_text[:5000]}"
        is_captcha = bool(CAPTCHA_RE.search(captcha_text))

        length, width, thickness, pieces = infer_dimensions(
            f"{title or ''}\n{visible_text}",
            item.product_url,
            item,
        )
        result.length_mm = length
        result.width_mm = width
        result.thickness_mm = thickness
        result.pieces_per_package = pieces

        # Для известных размеров всегда считаем площадь и объём сами.
        # Это исключает случайные значения вроде "2 м³" со страницы.
        area = None
        if length and width and pieces:
            area = length / 1000 * width / 1000 * pieces
        elif not is_captcha:
            area_match = AREA_RE.search(visible_text)
            if area_match:
                area = parse_number(area_match.group(1))

        volume = area * thickness / 1000 if area and thickness else None
        if volume is None and not is_captcha:
            volume_match = VOLUME_RE.search(visible_text)
            if volume_match:
                volume = parse_number(volume_match.group(1))

        result.area_per_package_m2 = round(area, 4) if area else None
        result.volume_per_package_m3 = round(volume, 6) if volume else None

        if is_captcha:
            result.status = "captcha"
            result.error = "Сайт показал CAPTCHA. Цена, наличие, продавец и артикул не получены."
            calculate_packaging(result, item)
            if save_debug:
                await save_debug_files(page, html, store_index, product_index, item.thickness_mm)
            return result

        result.found_name = title
        offer = offer_from_product(product_node)
        sku = product_node.get("sku") or product_node.get("mpn")
        if not sku:
            sku_match = SKU_RE.search(visible_text)
            sku = sku_match.group(1) if sku_match else None
        result.sku = str(sku).strip() if sku else None

        seller = offer.get("seller")
        if isinstance(seller, dict):
            seller = seller.get("name")
        result.seller = str(seller).strip() if seller else None

        availability = offer.get("availability")
        if availability:
            result.availability = str(availability).rsplit("/", 1)[-1]
        elif re.search(r"нет\s+в\s+наличии", visible_text, re.I):
            result.availability = "OutOfStock"
        elif re.search(r"под\s+заказ", visible_text, re.I):
            result.availability = "PreOrder"
        elif re.search(r"в\s+наличии", visible_text, re.I):
            result.availability = "InStock"

        price, source, currency = await extract_price(page, product_node, visible_text)
        result.package_price = price
        result.price_source = source
        result.currency = currency
        calculate_packaging(result, item)

        blocked_page = bool(BLOCKED_RE.search(f"{title or ''}\n{visible_text[:2000]}"))
        has_product_data = price is not None and bool(title) and not blocked_page

        if has_product_data:
            result.status = "found"
            result.error = None
            if result.http_status and result.http_status >= 400:
                Actor.log.warning(
                    "%s: HTTP %s, но название и цена получены; сохраняем status=found",
                    item.product_url,
                    result.http_status,
                )
        elif result.http_status in (401, 403):
            result.status = "access_blocked"
            result.error = f"Карточка вернула HTTP {result.http_status}"
        elif result.http_status == 429:
            result.status = "rate_limited"
            result.error = "Сайт ограничил частоту запросов: HTTP 429"
        elif result.http_status in (404, 410):
            result.status = "page_unavailable"
            result.error = f"Карточка недоступна: HTTP {result.http_status}"
        elif result.http_status and result.http_status >= 500:
            result.status = "server_error"
            result.error = f"Ошибка сервера: HTTP {result.http_status}"
        elif title and not blocked_page:
            result.status = "found_without_price"
            result.error = "Карточка открыта, но цена не извлечена"
        else:
            result.status = "page_unavailable"
            result.error = "Не удалось получить содержимое карточки"

        if save_debug and (save_debug_on_success or result.status != "found"):
            await save_debug_files(page, html, store_index, product_index, item.thickness_mm)
        return result
    except Exception as exc:
        result.status = "error"
        result.error = str(exc)
        if save_debug and html:
            await save_debug_files(page, html, store_index, product_index, item.thickness_mm)
        return result


# ---------------------------------------------------------------------------
# Input, proxy and browser contexts
# ---------------------------------------------------------------------------


def parse_products(raw: list[dict[str, Any]]) -> list[ProductInput]:
    products: list[ProductInput] = []
    for item in raw:
        name = normalize_text(item.get("name"))
        url = normalize_text(item.get("productUrl"))
        if not name or not url:
            continue
        products.append(
            ProductInput(
                name=name,
                product_url=url,
                thickness_mm=int(item["thicknessMm"]) if item.get("thicknessMm") is not None else None,
                quantity=float(item["quantity"]) if item.get("quantity") is not None else None,
                unit=str(item.get("unit") or "m2"),
                length_mm=parse_number(item.get("lengthMm")),
                width_mm=parse_number(item.get("widthMm")),
                pieces_per_package=int(item["piecesPerPackage"]) if item.get("piecesPerPackage") is not None else None,
                price_unit=str(item.get("priceUnit") or "package"),
            )
        )
    return products


def parse_stores(raw: list[dict[str, Any]]) -> list[StoreInput]:
    stores: list[StoreInput] = []
    for item in raw:
        name = normalize_text(item.get("storeName"))
        url = normalize_text(item.get("storeUrl"))
        products = parse_products(item.get("products") or [])
        if not name or not url or not products:
            continue
        stores.append(
            StoreInput(
                name=name,
                url=url,
                city=normalize_text(item.get("city")) or None,
                products=products,
                use_proxy=bool(item.get("useProxy", False)),
            )
        )
    return stores


def to_playwright_proxy(proxy_url: str) -> dict[str, str]:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError("Apify вернул некорректный URL прокси")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


async def create_context(
    browser: Browser,
    proxy: dict[str, str] | None,
) -> BrowserContext:
    context = await browser.new_context(
        locale="ru-RU",
        timezone_id="Asia/Yekaterinburg",
        viewport={"width": 1440, "height": 1000},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        proxy=proxy,
    )
    return context


async def prepare_store_page(
    context: BrowserContext,
    store: StoreInput,
    page_timeout_ms: int,
) -> tuple[Page, bool]:
    page = await context.new_page()
    page.set_default_timeout(10_000)
    city_selected = False
    try:
        await page.goto(store.url, wait_until="domcontentloaded", timeout=page_timeout_ms)
        await page.wait_for_timeout(1500)
        await dismiss_popups(page)
        city_selected = await select_city(page, store.city)
    except Exception as exc:
        Actor.log.warning("%s: главная страница не подготовлена: %s", store.name, exc)
    return page, city_selected


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        google_sheet_url = normalize_text(actor_input.get("googleSheetUrl"))
        api_key = normalize_text(actor_input.get("googleSheetsApiKey"))
        sheet_name = normalize_text(actor_input.get("sheetName")) or None
        selected_cities = [normalize_text(value) for value in actor_input.get("cities") or [] if normalize_text(value)]
        quantity_m2 = float(actor_input.get("quantityM2", 100))
        proxy_stores = [normalize_text(value) for value in actor_input.get("proxyStores") or ["САКСЭС", "s-stroy.ru"]]

        if google_sheet_url:
            if not api_key:
                raise ValueError("Заполните поле googleSheetsApiKey в Input")
            spreadsheet_id = extract_spreadsheet_id(google_sheet_url)
            Actor.log.info("Загружаем Google Таблицу: %s", spreadsheet_id)
            payload = await download_google_sheet_json(spreadsheet_id, api_key, sheet_name)
            stores = parse_google_sheet_stores(
                payload=payload,
                selected_cities=selected_cities,
                quantity_m2=quantity_m2,
                proxy_stores=proxy_stores,
                sheet_name=sheet_name,
            )
        else:
            # Оставлена обратная совместимость со старым Input stores.
            stores = parse_stores(actor_input.get("stores") or [])

        if not stores:
            raise ValueError("В Google Таблице не найдено ни одной товарной ссылки")

        headless = bool(actor_input.get("headless", True))
        save_debug = bool(actor_input.get("saveDebug", True))
        save_debug_on_success = bool(actor_input.get("saveDebugOnSuccess", False))
        request_delay_ms = max(0, int(actor_input.get("requestDelayMs", 4000)))
        max_retries = max(0, int(actor_input.get("maxRetries", 2)))
        page_timeout_ms = max(10_000, int(actor_input.get("pageTimeoutMs", 60_000)))
        use_residential_proxy = bool(actor_input.get("useResidentialProxy", True))
        proxy_required = bool(actor_input.get("proxyRequired", False))
        max_proxy_rotations = max(0, int(actor_input.get("maxProxyRotations", 2)))

        proxy_configuration = None
        if use_residential_proxy and any(store.use_proxy for store in stores):
            try:
                proxy_configuration = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"],
                    country_code="RU",
                )
                Actor.log.info("Residential proxy RU создан")
            except Exception as exc:
                if proxy_required:
                    raise RuntimeError(f"Residential proxy обязателен, но не создан: {exc}") from exc
                Actor.log.warning("Residential proxy недоступен, продолжаем напрямую: %s", exc)

        total_products = sum(len(store.products) for store in stores)
        Actor.log.info("Запуск проверки: магазинов=%d, товаров=%d", len(stores), total_products)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            for store_index, store in enumerate(stores, start=1):
                Actor.log.info(
                    "[Магазин %d/%d] %s / %s / товаров=%d",
                    store_index,
                    len(stores),
                    store.city,
                    store.name,
                    len(store.products),
                )

                proxy_rotation = 0

                async def new_store_context() -> tuple[BrowserContext, Page, bool]:
                    nonlocal proxy_rotation
                    proxy_settings = None
                    if store.use_proxy and proxy_configuration is not None:
                        session_id = f"s{store_index}_r{proxy_rotation}"
                        proxy_url = await proxy_configuration.new_url(session_id)
                        proxy_settings = to_playwright_proxy(proxy_url)
                        Actor.log.info(
                            "%s: residential proxy RU, session=%s",
                            store.name,
                            session_id,
                        )
                    context = await create_context(browser, proxy_settings)
                    page, city_selected = await prepare_store_page(context, store, page_timeout_ms)
                    Actor.log.info("%s: город выбран=%s", store.name, city_selected)
                    return context, page, city_selected

                context, page, city_selected = await new_store_context()

                for product_index, product in enumerate(store.products, start=1):
                    Actor.log.info(
                        "[%s %d/%d] %s, %s мм",
                        store.name,
                        product_index,
                        len(store.products),
                        product.name,
                        product.thickness_mm,
                    )

                    retry = 0
                    while True:
                        result = await inspect_product(
                            page=page,
                            item=product,
                            store=store,
                            city_selected=city_selected,
                            save_debug=save_debug,
                            save_debug_on_success=save_debug_on_success,
                            store_index=store_index,
                            product_index=product_index,
                            page_timeout_ms=page_timeout_ms,
                        )

                        if (
                            result.status == "access_blocked"
                            and store.use_proxy
                            and proxy_configuration is not None
                            and proxy_rotation < max_proxy_rotations
                        ):
                            proxy_rotation += 1
                            Actor.log.warning(
                                "%s: HTTP %s, меняем proxy-сессию (%d/%d)",
                                store.name,
                                result.http_status,
                                proxy_rotation,
                                max_proxy_rotations,
                            )
                            await context.close()
                            context, page, city_selected = await new_store_context()
                            continue

                        if result.status in {"rate_limited", "server_error", "error"} and retry < max_retries:
                            retry += 1
                            Actor.log.warning(
                                "%s: повтор %d/%d после status=%s",
                                product.product_url,
                                retry,
                                max_retries,
                                result.status,
                            )
                            await page.wait_for_timeout(request_delay_ms)
                            continue
                        break

                    await Actor.push_data(asdict(result))
                    Actor.log.info(
                        "%s / %s мм: status=%s, http=%s, price=%s",
                        store.name,
                        product.thickness_mm,
                        result.status,
                        result.http_status,
                        result.package_price,
                    )
                    if request_delay_ms:
                        await page.wait_for_timeout(request_delay_ms)

                await context.close()

            await browser.close()

        Actor.log.info("Проверка всех магазинов завершена")


if __name__ == "__main__":
    asyncio.run(main())

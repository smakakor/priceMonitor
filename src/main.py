from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from apify import Actor
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright


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
    r"проверка\s+безопасности|access\s+denied|доступ\s+ограничен|cloudflare",
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


@dataclass
class StoreInput:
    name: str
    url: str
    city: str | None
    products: list[ProductInput]


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
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?", str(value))
    if not match:
        return None
    return float(
        match.group(0)
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace(",", ".")
    )


def safe_key(value: str) -> str:
    normalized = re.sub(r"[^A-Za-zА-Яа-я0-9]+", "_", value).strip("_")
    return normalized[:50] or "STORE"


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


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
                nodes.extend(
                    node for node in walk_json(parsed) if isinstance(node, dict)
                )
        except Exception:
            continue
    return nodes


async def meta(page: Page, selector: str, attribute: str = "content") -> str | None:
    try:
        value = await page.locator(selector).first.get_attribute(
            attribute, timeout=1000
        )
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


async def extract_title(
    page: Page, product_node: dict[str, Any]
) -> str | None:
    candidates: list[str | None] = [
        str(product_node.get("name")) if product_node.get("name") else None,
        await meta(page, "meta[property='og:title']"),
        await meta(page, "meta[name='twitter:title']"),
    ]
    try:
        candidates.append(
            (await page.locator("h1").first.inner_text(timeout=2000)).strip()
        )
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
            0
            if item[1].startswith("json-ld")
            else 1
            if item[1].startswith("dom")
            else 2
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
    triples = TRIPLE_RE.findall(combined)
    for triple in triples:
        numbers = [float(value) for value in triple]
        requested = float(item.thickness_mm) if item.thickness_mm else None

        thickness_candidate = None
        if requested is not None:
            thickness_candidate = next(
                (number for number in numbers if abs(number - requested) < 0.1),
                None,
            )
        if thickness_candidate is None:
            small = [number for number in numbers if number <= 200]
            thickness_candidate = min(small) if small else min(numbers)

        faces = numbers.copy()
        try:
            faces.remove(thickness_candidate)
        except ValueError:
            pass

        if len(faces) == 2:
            inferred_length = max(faces)
            inferred_width = min(faces)
            length = length or inferred_length
            width = width or inferred_width
            thickness = thickness or thickness_candidate
            break

    if pieces is None:
        piece_match = PIECES_RE.search(text)
        if piece_match:
            pieces = int(piece_match.group(1))

    if pieces is None:
        slug = urlparse(url).path.lower()
        slug_match = re.search(
            r"-(\d+)-(?:plit|plity|plit[а-я]*)", slug
        )
        if slug_match:
            pieces = int(slug_match.group(1))

    return length, width, thickness, pieces


def calculate_packaging(
    result: ProductResult,
    item: ProductInput,
) -> None:
    area = result.area_per_package_m2
    volume = result.volume_per_package_m3
    price = result.package_price

    if price and result.pieces_per_package:
        result.price_per_piece = round(
            price / result.pieces_per_package, 2
        )
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
    store_name: str,
    thickness: int | None,
) -> None:
    prefix = (
        f"STORE_{store_index}_{safe_key(store_name)}_"
        f"PRODUCT_{product_index}_{thickness or 'NA'}"
    )
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
    store_index: int,
    product_index: int,
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
    )

    try:
        response = await page.goto(
            item.product_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        result.http_status = response.status if response else None
        await page.wait_for_timeout(2500)
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
                and "product"
                in [str(value).lower() for value in node.get("@type", [])]
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

        area = None
        area_match = AREA_RE.search(visible_text)
        if area_match and not is_captcha:
            area = parse_number(area_match.group(1))
        if not area and length and width and pieces:
            area = length / 1000 * width / 1000 * pieces

        volume = None
        volume_match = VOLUME_RE.search(visible_text)
        if volume_match and not is_captcha:
            volume = parse_number(volume_match.group(1))
        if not volume and area and thickness:
            volume = area * thickness / 1000

        result.area_per_package_m2 = round(area, 4) if area else None
        result.volume_per_package_m3 = round(volume, 6) if volume else None

        if is_captcha:
            result.status = "captcha"
            result.error = (
                "Сайт показал CAPTCHA. Цена, наличие, продавец и артикул "
                "не получены."
            )
            calculate_packaging(result, item)
            if save_debug:
                await save_debug_files(
                    page,
                    html,
                    store_index,
                    product_index,
                    store.name,
                    item.thickness_mm,
                )
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

        price, source, currency = await extract_price(
            page,
            product_node,
            visible_text,
        )
        result.package_price = price
        result.price_source = source
        result.currency = currency
        calculate_packaging(result, item)

        if result.http_status and result.http_status >= 400:
            result.status = "access_blocked"
            result.error = f"Карточка вернула HTTP {result.http_status}"
        elif price is not None:
            result.status = "found"
        elif title:
            result.status = "found_without_price"
            result.error = "Карточка открыта, но цена не извлечена"
        else:
            result.status = "page_unavailable"
            result.error = "Не удалось получить содержимое карточки"

        if save_debug:
            await save_debug_files(
                page,
                html,
                store_index,
                product_index,
                store.name,
                item.thickness_mm,
            )
        return result
    except Exception as exc:
        result.status = "error"
        result.error = str(exc)
        return result


def parse_products(raw: list[dict[str, Any]]) -> list[ProductInput]:
    products: list[ProductInput] = []
    for item in raw:
        name = str(item.get("name") or "").strip()
        url = str(item.get("productUrl") or "").strip()
        if not name or not url:
            continue
        products.append(
            ProductInput(
                name=name,
                product_url=url,
                thickness_mm=(
                    int(item["thicknessMm"])
                    if item.get("thicknessMm") is not None
                    else None
                ),
                quantity=(
                    float(item["quantity"])
                    if item.get("quantity") is not None
                    else None
                ),
                unit=str(item.get("unit") or "m2"),
                length_mm=parse_number(item.get("lengthMm")),
                width_mm=parse_number(item.get("widthMm")),
                pieces_per_package=(
                    int(item["piecesPerPackage"])
                    if item.get("piecesPerPackage") is not None
                    else None
                ),
            )
        )
    return products


def parse_stores(raw: list[dict[str, Any]]) -> list[StoreInput]:
    stores: list[StoreInput] = []
    for item in raw:
        name = str(item.get("storeName") or "").strip()
        url = str(item.get("storeUrl") or "").strip()
        products = parse_products(item.get("products") or [])
        if not name or not url or not products:
            continue
        stores.append(
            StoreInput(
                name=name,
                url=url,
                city=str(item.get("city") or "").strip() or None,
                products=products,
            )
        )
    return stores


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        stores = parse_stores(actor_input.get("stores") or [])
        headless = bool(actor_input.get("headless", True))
        save_debug = bool(actor_input.get("saveDebug", True))

        if not stores:
            raise ValueError(
                "Не задан массив stores с магазинами и товарами"
            )

        total_products = sum(len(store.products) for store in stores)
        Actor.log.info(
            "Запуск проверки: магазинов=%d, товаров=%d",
            len(stores),
            total_products,
        )

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
                    "[Магазин %d/%d] %s",
                    store_index,
                    len(stores),
                    store.name,
                )

                context = await browser.new_context(
                    locale="ru-RU",
                    timezone_id="Asia/Yekaterinburg",
                    viewport={"width": 1440, "height": 1000},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                page.set_default_timeout(10_000)

                city_selected = False
                try:
                    await page.goto(
                        store.url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await page.wait_for_timeout(1600)
                    await dismiss_popups(page)
                    city_selected = await select_city(page, store.city)
                    Actor.log.info(
                        "%s: город выбран=%s",
                        store.name,
                        city_selected,
                    )
                except Exception as exc:
                    Actor.log.warning(
                        "%s: главная страница не подготовлена: %s",
                        store.name,
                        exc,
                    )

                for product_index, product in enumerate(
                    store.products, start=1
                ):
                    Actor.log.info(
                        "[%s %d/%d] %s, %s мм",
                        store.name,
                        product_index,
                        len(store.products),
                        product.name,
                        product.thickness_mm,
                    )
                    result = await inspect_product(
                        page=page,
                        item=product,
                        store=store,
                        city_selected=city_selected,
                        save_debug=save_debug,
                        store_index=store_index,
                        product_index=product_index,
                    )
                    await Actor.push_data(asdict(result))
                    Actor.log.info(
                        "%s / %s мм: status=%s, price=%s",
                        store.name,
                        product.thickness_mm,
                        result.status,
                        result.package_price,
                    )

                await context.close()

            await browser.close()

        Actor.log.info("Проверка всех магазинов завершена")

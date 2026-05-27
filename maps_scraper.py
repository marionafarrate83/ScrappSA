"""
Scraper best-effort para resultados públicos de Google Maps.
Google Maps renderiza gran parte del contenido con JavaScript, por lo que este
módulo extrae lo disponible en el HTML inicial y conserva el enlace al lugar.
"""
import html
import json
import re
import time
from typing import Optional
from urllib.parse import quote_plus, unquote, urljoin

import requests
from bs4 import BeautifulSoup

import linkedin_enricher as le

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:
    PlaywrightTimeoutError = Exception
    sync_playwright = None


BASE_URL = "https://www.google.com"
MAPS_SEARCH_URL = "https://www.google.com/maps/search/{query}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.7",
}

PHONE_RE = re.compile(r"(?:\+?52[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{4}")
RATING_RE = re.compile(r"([1-5](?:[.,]\d)?)\s*(?:estrellas?|stars?|star rating)", re.I)
REVIEWS_RE = re.compile(r"([\d,.]+)\s*(?:reseñas?|opiniones|reviews?)", re.I)
COORD_RE = re.compile(r"(?:@|!3d)(-?\d+\.\d+)(?:,|!4d)(-?\d+\.\d+)")
BLOCKED_RE = re.compile(r"captcha|unusual traffic|verify you are human|sorry/index", re.I)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def empty_lead_fields() -> dict:
    return {
        "linkedin_url": "",
        "lider_nombre": "",
        "lider_cargo": "",
        "lider_fuente": "",
        "lider_confianza": "",
    }


def with_lead_fields(item: dict) -> dict:
    for key, value in empty_lead_fields().items():
        item.setdefault(key, value)
    return item


def normalize_html(raw: str) -> str:
    text = raw.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    text = html.unescape(text)
    return text


def build_search_phrase(
    query: str,
    location: str,
    radius_km: Optional[float] = None,
    open_now: bool = False,
) -> str:
    parts = [query]
    if location:
        parts.append(f"en {location}")
    if radius_km:
        parts.append(f"a {radius_km:g} km")
    if open_now:
        parts.append("abierto ahora")
    return " ".join(parts)


def build_maps_url(phrase: str) -> str:
    return MAPS_SEARCH_URL.format(query=quote_plus(phrase)) + "?hl=es-419&gl=mx"


def max_results_from(filters: dict) -> int:
    try:
        return max(1, min(200, int(filters.get("max_results") or 25)))
    except (TypeError, ValueError):
        return 25


def parse_number(value: str) -> Optional[float]:
    if not value:
        return None
    value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str) -> Optional[int]:
    if not value:
        return None
    value = value.replace(",", "").replace(".", "")
    try:
        return int(value)
    except ValueError:
        return None


def text_or_empty(locator) -> str:
    try:
        return clean_text(locator.inner_text(timeout=700))
    except Exception:
        return ""


def attr_or_empty(locator, name: str) -> str:
    try:
        return locator.get_attribute(name, timeout=700) or ""
    except Exception:
        return ""


def parse_rating_from_text(text: str) -> Optional[float]:
    match = re.search(r"\b([1-5](?:[.,]\d)?)\b", text)
    return parse_number(match.group(1)) if match else None


def parse_reviews_from_text(text: str) -> Optional[int]:
    match = re.search(r"\(([\d,.]+)\)", text)
    return parse_int(match.group(1)) if match else None


def extract_card_from_browser(card) -> Optional[dict]:
    name = ""
    for selector in [".fontHeadlineSmall", "[role='heading']", ".qBF1Pd"]:
        locator = card.locator(selector).first
        name = text_or_empty(locator)
        if name:
            break
    if not name:
        return None

    maps_url = attr_or_empty(card.locator("a[href*='/maps/place']").first, "href")
    if not maps_url:
        maps_url = attr_or_empty(card.locator("a[href*='google.com/maps']").first, "href")

    rating_text = text_or_empty(card.locator("span[role='img'][aria-label*='estrellas']").first)
    if not rating_text:
        rating_text = attr_or_empty(card.locator("span[role='img'][aria-label*='estrellas']").first, "aria-label")
    if not rating_text:
        rating_text = attr_or_empty(card.locator("span[role='img'][aria-label*='stars']").first, "aria-label")

    full_text = text_or_empty(card)
    phone_match = PHONE_RE.search(full_text)
    coords = COORD_RE.search(maps_url)

    return {
        "nombre": name,
        "categoria": "",
        "calificacion": parse_rating_from_text(rating_text),
        "resenas": parse_reviews_from_text(rating_text),
        "telefono": phone_match.group(0) if phone_match else "",
        "direccion": "",
        "sitio_web": "",
        "google_maps_url": maps_url,
        "latitud": coords.group(1) if coords else "",
        "longitud": coords.group(2) if coords else "",
        "estado": "Encontrado",
    }


def enrich_browser_result(page, item: dict) -> dict:
    url = item.get("google_maps_url")
    if not url:
        return item

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=12000)
        page.wait_for_timeout(1000)
    except Exception:
        return item

    try:
        heading = text_or_empty(page.locator("h1").first)
        if heading:
            item["nombre"] = heading
    except Exception:
        pass

    for selector in [
        "button[data-item-id='address']",
        "button[aria-label^='Dirección:']",
        "button[aria-label^='Address:']",
    ]:
        value = text_or_empty(page.locator(selector).first)
        if value:
            item["direccion"] = value
            break

    website = attr_or_empty(page.locator("a[data-item-id='authority']").first, "href")
    if not website:
        website = attr_or_empty(page.locator("a[aria-label^='Sitio web:']").first, "href")
    if website:
        item["sitio_web"] = website

    for selector in [
        "button[data-item-id^='phone:tel:']",
        "button[aria-label^='Teléfono:']",
        "button[aria-label^='Phone:']",
    ]:
        value = text_or_empty(page.locator(selector).first)
        if value:
            item["telefono"] = value
            break

    category = text_or_empty(page.locator("button[jsaction*='category']").first)
    if category:
        item["categoria"] = category

    return item


def scrape_maps_browser(phrase: str, filters: dict, progress=None) -> list[dict]:
    if sync_playwright is None:
        raise RuntimeError("Playwright no está disponible en este entorno.")

    def log(message: str):
        if progress:
            progress(message)

    max_results = max_results_from(filters)
    url = build_maps_url(phrase)
    log("Abriendo Google Maps con navegador real...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="es-MX",
            viewport={"width": 1365, "height": 900},
            user_agent=HEADERS["User-Agent"],
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.get_by_role("button", name=re.compile("Aceptar|Accept|Estoy de acuerdo", re.I)).click(timeout=2500)
        except Exception:
            pass

        try:
            page.wait_for_selector("div[role='feed'], a[href*='/maps/place']", timeout=15000)
        except PlaywrightTimeoutError:
            html_text = page.content()
            browser.close()
            if BLOCKED_RE.search(html_text):
                raise RuntimeError("Google pidió verificación o bloqueó la solicitud.")
            raise RuntimeError("No se detectó el panel de resultados de Google Maps.")

        feed = page.locator("div[role='feed']").first
        seen_count = 0
        stable_rounds = 0
        max_scrolls = max(8, min(80, max_results * 2))

        for step in range(max_scrolls):
            cards_count = page.locator("div[role='article']").count()
            links_count = page.locator("a[href*='/maps/place']").count()
            current_count = max(cards_count, links_count)
            if current_count >= max_results:
                break

            if current_count == seen_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
                seen_count = current_count

            if stable_rounds >= 5:
                break

            try:
                feed.evaluate("(el) => { el.scrollTop = el.scrollHeight; }", timeout=1000)
            except Exception:
                page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1100)
            if (step + 1) % 5 == 0:
                log(f"Scroll {step + 1}: {current_count} resultados cargados...")

        cards = page.locator("div[role='article']")
        results = []
        count = cards.count()
        for index in range(count):
            item = extract_card_from_browser(cards.nth(index))
            if item:
                results.append(item)

        if not results:
            links = page.locator("a[href*='/maps/place']")
            for index in range(links.count()):
                link = links.nth(index)
                url_value = attr_or_empty(link, "href")
                name = text_or_empty(link)
                if not name:
                    name = name_from_url(url_value)
                if name:
                    results.append({
                        "nombre": name,
                        "categoria": "",
                        "calificacion": None,
                        "resenas": None,
                        "telefono": "",
                        "direccion": "",
                        "sitio_web": "",
                        "google_maps_url": url_value,
                        "latitud": "",
                        "longitud": "",
                        "estado": "Encontrado",
                    })

        unique = dedupe(results)
        filtered = apply_filters(unique, filters)

        should_enrich = bool(filters.get("enrich_details"))
        if should_enrich:
            enriched = []
            for index, item in enumerate(filtered[:max_results], start=1):
                log(f"Abriendo detalle {index}/{len(filtered[:max_results])}: {item.get('nombre', '')}")
                enriched.append(enrich_browser_result(page, item))
            filtered = apply_filters(enriched, filters)

        browser.close()
        return filtered[:max_results]


def extract_place_urls(text: str) -> list[tuple[str, str]]:
    urls = []
    patterns = [
        r"https://www\.google\.com/maps/place/[^\"'<>\\]+",
        r"/maps/place/[^\"'<>\\]+",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            url = match.group(0)
            if url.startswith("/"):
                url = urljoin(BASE_URL, url)
            url = url.split("&amp;")[0].split("\\")[0]
            name = name_from_url(url)
            if name:
                urls.append((name, url))
    return urls


def extract_internal_map_url(text: str) -> str:
    match = re.search(r"/search\?tbm=map[^\"']+", text)
    if not match:
        return ""
    return urljoin(BASE_URL, match.group(0))


def load_google_json(text: str):
    body = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    return json.loads(body)


def walk_lists(value):
    if isinstance(value, list):
        yield value
        for child in value:
            yield from walk_lists(child)


def first_chij(value) -> str:
    if isinstance(value, str) and value.startswith("ChIJ"):
        return value
    if isinstance(value, list):
        for child in value:
            found = first_chij(child)
            if found:
                return found
    return ""


def parse_internal_record(record: list) -> Optional[dict]:
    if len(record) < 19:
        return None
    if not isinstance(record[11], str) or not record[11].strip():
        return None
    if not isinstance(record[9], list) or len(record[9]) < 4:
        return None

    name = clean_text(record[11])
    address_parts = record[2] if isinstance(record[2], list) else []
    address = clean_text(", ".join(str(part) for part in address_parts if part))
    full_address = clean_text(record[18] if isinstance(record[18], str) else "")
    rating = None
    if isinstance(record[4], list) and len(record[4]) > 7:
        rating = parse_number(str(record[4][7]))

    website = ""
    if isinstance(record[7], list) and record[7]:
        website = record[7][0] or ""

    phone = ""
    if len(record) > 178 and isinstance(record[178], list) and record[178]:
        phone_data = record[178][0]
        if isinstance(phone_data, list) and phone_data:
            phone = phone_data[0] or ""
        elif isinstance(phone_data, str):
            phone = phone_data

    categories = record[13] if isinstance(record[13], list) else []
    category = ", ".join(str(item) for item in categories[:3] if item)
    lat = record[9][2] if len(record[9]) > 2 else ""
    lng = record[9][3] if len(record[9]) > 3 else ""
    place_id = first_chij(record)
    query = quote_plus(full_address or name)
    maps_url = f"https://www.google.com/maps/search/?api=1&query={query}"
    if place_id:
        maps_url += f"&query_place_id={quote_plus(place_id)}"

    return {
        "nombre": name,
        "categoria": category,
        "calificacion": rating,
        "resenas": None,
        "telefono": phone,
        "direccion": full_address or address,
        "sitio_web": website,
        "google_maps_url": maps_url,
        "latitud": lat,
        "longitud": lng,
        "estado": "Encontrado",
    }


def extract_internal_results(text: str) -> list[dict]:
    internal_url = extract_internal_map_url(text)
    if not internal_url:
        return []

    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(internal_url, timeout=25)
    response.raise_for_status()
    data = load_google_json(normalize_html(response.text))

    results = []
    for record in walk_lists(data):
        item = parse_internal_record(record)
        if item:
            results.append(item)
    return dedupe(results)


def name_from_url(url: str) -> str:
    try:
        chunk = url.split("/maps/place/", 1)[1]
    except IndexError:
        return ""
    chunk = chunk.split("/data=", 1)[0].split("/@", 1)[0].split("?", 1)[0]
    return clean_text(unquote(chunk.replace("+", " ")))


def context_for(text: str, needle: str, width: int = 2200) -> str:
    index = text.find(needle)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + len(needle) + width)
    return text[start:end]


def extract_first(pattern: re.Pattern, text: str):
    match = pattern.search(text)
    return match.group(1) if match else ""


def extract_business(name: str, url: str, text: str) -> dict:
    context = context_for(text, url) or context_for(text, quote_plus(name)) or text[:4000]
    rating = parse_number(extract_first(RATING_RE, context))
    reviews = parse_int(extract_first(REVIEWS_RE, context))
    phone_match = PHONE_RE.search(context)
    coords = COORD_RE.search(url) or COORD_RE.search(context)

    lat = coords.group(1) if coords else ""
    lng = coords.group(2) if coords else ""

    return {
        "nombre": name,
        "categoria": "",
        "calificacion": rating,
        "resenas": reviews,
        "telefono": phone_match.group(0) if phone_match else "",
        "direccion": "",
        "sitio_web": "",
        "google_maps_url": url,
        "latitud": lat,
        "longitud": lng,
        "estado": "Encontrado",
    }


def enrich_linkedin_results(results: list[dict], location: str, progress=None) -> list[dict]:
    def log(message: str):
        if progress:
            progress(message)

    session = requests.Session()
    session.headers.update(le.HEADERS)
    enriched = []
    total = len(results)

    for index, item in enumerate(results, start=1):
        item = with_lead_fields(item)
        name = item.get("nombre", "")
        if not name:
            enriched.append(item)
            continue
        log(f"LinkedIn {index}/{total}: buscando líder de {name}...")
        item.update(le.infer_leader(name, location=location, session=session))
        enriched.append(item)
        time.sleep(0.7)

    return enriched


def dedupe(results: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for item in results:
        key = (
            item.get("nombre", "").lower().strip(),
            item.get("latitud", ""),
            item.get("longitud", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(with_lead_fields(item))
    return unique


def apply_filters(results: list[dict], filters: dict) -> list[dict]:
    min_rating = parse_number(str(filters.get("min_rating") or ""))
    require_phone = bool(filters.get("require_phone"))
    require_website = bool(filters.get("require_website"))
    max_results = max_results_from(filters)

    filtered = []
    for item in results:
        if min_rating is not None:
            rating = item.get("calificacion")
            if rating is None or rating < min_rating:
                continue
        if require_phone and not item.get("telefono"):
            continue
        if require_website and not item.get("sitio_web"):
            continue
        filtered.append(item)
        if len(filtered) >= max_results:
            break
    return filtered


def scrape_maps(query: str, location: str, filters: Optional[dict] = None, progress=None) -> list[dict]:
    filters = filters or {}

    def log(message: str):
        if progress:
            progress(message)

    phrase = build_search_phrase(
        query=query,
        location=location,
        radius_km=parse_number(str(filters.get("radius_km") or "")),
        open_now=bool(filters.get("open_now")),
    )
    url = build_maps_url(phrase)

    session = requests.Session()
    session.headers.update(HEADERS)

    log(f"Iniciando búsqueda en Google Maps: '{phrase}'")
    use_browser = filters.get("use_browser", True)
    if use_browser:
        try:
            browser_results = scrape_maps_browser(phrase, filters, progress=progress)
            if filters.get("enrich_linkedin"):
                browser_results = enrich_linkedin_results(browser_results, location, progress=progress)
            log(f"DONE:{len(browser_results)}")
            return browser_results
        except Exception as exc:
            log(f"No se pudo usar navegador real; usando fallback HTML: {exc}")

    log("Descargando resultados iniciales...")

    response = session.get(url, timeout=25)
    response.raise_for_status()
    text = normalize_html(response.text)

    if BLOCKED_RE.search(text):
        log("Google pidió verificación o bloqueó la solicitud.")
        return [{
            "nombre": "",
            "categoria": "",
            "calificacion": None,
            "resenas": None,
            "telefono": "",
            "direccion": "",
            "sitio_web": "",
            "google_maps_url": url,
            "latitud": "",
            "longitud": "",
            "linkedin_url": "",
            "lider_nombre": "",
            "lider_cargo": "",
            "lider_fuente": "",
            "lider_confianza": "",
            "estado": "Google pidió verificación o bloqueó la solicitud.",
        }]

    candidates = extract_place_urls(text)
    try:
        internal_results = extract_internal_results(text)
    except Exception as exc:
        log(f"No se pudo leer el JSON interno de Maps: {exc}")
        internal_results = []
    if internal_results:
        log(f"Resultados de Maps detectados: {len(internal_results)}")
        filtered = apply_filters(internal_results, filters)
        if filters.get("enrich_linkedin"):
            filtered = enrich_linkedin_results(filtered, location, progress=progress)
        log(f"DONE:{len(filtered)}")
        return filtered

    log(f"Candidatos detectados: {len(candidates)}")

    results = []
    for index, (name, place_url) in enumerate(candidates, start=1):
        results.append(extract_business(name, place_url, text))
        if index % 10 == 0:
            log(f"Procesados {index} candidatos...")
        time.sleep(0.05)

    unique = dedupe(results)
    filtered = apply_filters(unique, filters)
    if filters.get("enrich_linkedin"):
        filtered = enrich_linkedin_results(filtered, location, progress=progress)
    log(f"DONE:{len(filtered)}")
    return filtered

"""
Comparador de precios para farmacias y supermercados en México.
Busca un producto por tienda y extrae el mejor candidato visible en HTML.
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9",
}


@dataclass(frozen=True)
class Store:
    name: str
    base_url: str
    search_url: str


STORES = [
    Store("Farmacias Benavides", "https://www.benavides.com.mx", "https://www.benavides.com.mx/catalogsearch/result/?q={query}"),
    Store("Farmacias del Ahorro", "https://www.fahorro.com", "https://www.fahorro.com/catalogsearch/result/?q={query}"),
    Store("Farmatodo", "https://www.farmatodo.com.mx", "https://www.farmatodo.com.mx/buscar?product={query}"),
    Store("Farmacia San Pablo", "https://www.farmaciasanpablo.com.mx", "https://www.farmaciasanpablo.com.mx/search/?text={query}"),
    Store("Walmart", "https://www.walmart.com.mx", "https://www.walmart.com.mx/search?q={query}"),
    Store("Chedraui", "https://www.chedraui.com.mx", "https://www.chedraui.com.mx/search?text={query}"),
    Store("Soriana", "https://www.soriana.com", "https://www.soriana.com/buscar?q={query}"),
    Store("La Comer", "https://www.lacomer.com.mx", "https://www.lacomer.com.mx/lacomer/#!/search?text={query}"),
]


PRICE_RE = re.compile(r"\$\s?([0-9]{1,3}(?:[, ]?[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)")
BLOCKED_RE = re.compile(
    r"captcha|access denied|forbidden|verifica que eres humano|verify you are human|unusual traffic",
    re.I,
)


def normalize_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value)
    match = re.search(r"([0-9]{1,3}(?:[, ]?[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)", text)
    if not match:
        return None
    number = match.group(1).replace(",", "").replace(" ", "")
    try:
        return round(float(number), 2)
    except ValueError:
        return None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def similarity_score(query: str, title: str) -> int:
    query_words = {w for w in re.findall(r"[a-záéíóúüñ0-9]+", query.lower()) if len(w) > 2}
    title_words = {w for w in re.findall(r"[a-záéíóúüñ0-9]+", title.lower()) if len(w) > 2}
    if not query_words:
        return 0
    return len(query_words & title_words)


def parse_json_ld(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    products = []

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for item in walk_json(data):
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type") or item.get("type")
            if isinstance(item_type, list):
                is_product = "Product" in item_type
            else:
                is_product = item_type == "Product"
            if not is_product:
                continue

            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = normalize_price(offers.get("price") if isinstance(offers, dict) else None)
            name = clean_text(item.get("name", ""))
            if name and price:
                products.append({
                    "tienda": store.name,
                    "producto": name,
                    "precio": price,
                    "precio_texto": f"${price:,.2f}",
                    "url": absolute_url(store, item.get("url") or offers.get("url", "")),
                    "fuente": "json-ld",
                    "score": similarity_score(query, name),
                })

    return products


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_meta_product(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    title = ""
    title_tag = soup.find("meta", property="og:title") or soup.find("title")
    if title_tag:
        title = clean_text(title_tag.get("content", "") or title_tag.get_text())

    price = None
    for selector in [
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ]:
        tag = soup.find("meta", selector)
        if tag:
            price = normalize_price(tag.get("content"))
            if price:
                break

    if title and price:
        return [{
            "tienda": store.name,
            "producto": title,
            "precio": price,
            "precio_texto": f"${price:,.2f}",
            "url": store.base_url,
            "fuente": "meta",
            "score": similarity_score(query, title),
        }]
    return []


def parse_visible_prices(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    products = []
    candidates = soup.find_all(["article", "li", "div"], limit=400)

    for node in candidates:
        text = clean_text(node.get_text(" ", strip=True))
        price_match = PRICE_RE.search(text)
        if not price_match:
            continue

        title = extract_title(node, text)
        if not title or len(title) < 4:
            continue

        price = normalize_price(price_match.group(0))
        if not price:
            continue

        link = node.find("a", href=True)
        products.append({
            "tienda": store.name,
            "producto": title[:180],
            "precio": price,
            "precio_texto": f"${price:,.2f}",
            "url": absolute_url(store, link.get("href", "")) if link else store.base_url,
            "fuente": "html",
            "score": similarity_score(query, title),
        })

    return products


def parse_product_page(html: str, store: Store, query: str, url: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")
    products = []
    products.extend(parse_json_ld(soup, store, query))
    products.extend(parse_meta_product(soup, store, query))
    products.extend(parse_visible_prices(soup, store, query))

    best = choose_best(products)
    if best:
        best["url"] = best.get("url") or url
        best["estado"] = "Encontrado"
    return best


def discover_product_urls(session: requests.Session, store: Store, query: str) -> list[str]:
    domain = urlparse(store.base_url).netloc.replace("www.", "")
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(f'site:{domain} {query}')}"
    response = session.get(search_url, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    urls = []
    for link in soup.select("a.result__a[href], a[href]"):
        href = link.get("href", "")
        if "duckduckgo.com/l/?" in href:
            parsed = urlparse(href)
            href = parse_qs(parsed.query).get("uddg", [""])[0]
            href = unquote(href)
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        if domain not in parsed.netloc:
            continue
        if href not in urls:
            urls.append(href)
        if len(urls) >= 4:
            break
    return urls


def scrape_discovered_pages(session: requests.Session, store: Store, query: str) -> Optional[dict]:
    for url in discover_product_urls(session, store, query):
        try:
            response = session.get(url, timeout=25)
            response.raise_for_status()
            if BLOCKED_RE.search(response.text):
                continue
            best = parse_product_page(response.text, store, query, url)
            if best:
                best["fuente"] = f"{best.get('fuente', 'html')} + buscador"
                return best
        except Exception:
            continue
    return None


def extract_title(node, fallback: str) -> str:
    for selector in ["[itemprop='name']", "h1", "h2", "h3", "h4", "a[title]", "img[alt]"]:
        tag = node.select_one(selector)
        if not tag:
            continue
        text = clean_text(tag.get("title") or tag.get("alt") or tag.get_text(" ", strip=True))
        if text:
            return text

    before_price = PRICE_RE.split(fallback, maxsplit=1)[0]
    before_price = re.sub(r"(?i)\b(precio|oferta|agregar|comprar|en linea)\b", " ", before_price)
    return clean_text(before_price)[-180:]


def absolute_url(store: Store, url: str) -> str:
    if not url:
        return store.base_url
    return urljoin(store.base_url, url)


def choose_best(products: list[dict]) -> Optional[dict]:
    if not products:
        return None
    ranked = sorted(
        products,
        key=lambda item: (item.get("score", 0), item.get("fuente") == "json-ld", -item.get("precio", 0)),
        reverse=True,
    )
    best = ranked[0]
    best.pop("score", None)
    return best


def scrape_store(session: requests.Session, store: Store, query: str) -> dict:
    url = store.search_url.format(query=quote_plus(query))
    search_error = ""
    response = None
    try:
        response = session.get(url, timeout=25)
        response.raise_for_status()
    except Exception as exc:
        search_error = str(exc)

    if response is not None and BLOCKED_RE.search(response.text):
        search_error = "La tienda bloqueó o pidió verificación."
        response = None

    if response is not None:
        best = parse_product_page(response.text, store, query, url)
        if best:
            return best

    try:
        best = scrape_discovered_pages(session, store, query)
        if best:
            return best
    except Exception as exc:
        search_error = search_error or str(exc)

    return {
        "tienda": store.name,
        "producto": "",
        "precio": None,
        "precio_texto": "",
        "url": url,
        "fuente": "sin_resultado",
        "estado": search_error or "Sin precio visible en HTML.",
    }


def compare_prices(query: str, progress=None) -> list[dict]:
    def log(message: str):
        if progress:
            progress(message)

    session = requests.Session()
    session.headers.update(HEADERS)

    log(f"Iniciando comparación de precios: '{query}'")
    results = []

    for index, store in enumerate(STORES, start=1):
        log(f"[{index}/{len(STORES)}] Buscando en {store.name}...")
        try:
            result = scrape_store(session, store, query)
            results.append(result)
            if result.get("precio") is not None:
                log(f"{store.name}: {result['precio_texto']} - {result['producto'][:80]}")
            else:
                log(f"{store.name}: {result.get('estado', 'Sin resultado')}")
        except Exception as exc:
            results.append({
                "tienda": store.name,
                "producto": "",
                "precio": None,
                "precio_texto": "",
                "url": store.search_url.format(query=quote_plus(query)),
                "fuente": "error",
                "estado": str(exc),
            })
            log(f"Error en {store.name}: {exc}")
        time.sleep(0.8)

    found = [item for item in results if item.get("precio") is not None]
    if found:
        lowest = min(found, key=lambda item: item["precio"])
        log(f"Mejor precio: {lowest['tienda']} - {lowest['precio_texto']}")

    log(f"DONE:{len(found)}")
    return sorted(results, key=lambda item: item["precio"] if item.get("precio") is not None else 10**9)

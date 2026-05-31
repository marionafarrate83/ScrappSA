"""
Comparador de precios para farmacias y supermercados en México.
Estrategia por capas:
  1. requests + extracción de JSON embebido (__NEXT_DATA__, Magento, JSON-LD)
  2. Playwright headless si la capa 1 falla o está bloqueada
  3. DuckDuckGo como último recurso
"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except Exception:
    PWTimeout = Exception
    sync_playwright = None


# ── Headers realistas de Chrome ─────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}


@dataclass(frozen=True)
class Store:
    name: str
    base_url: str
    search_url: str
    needs_browser: bool = False   # siempre usa Playwright si está disponible


STORES = [
    Store("Farmacias Benavides",  "https://www.benavides.com.mx",      "https://www.benavides.com.mx/catalogsearch/result/?q={query}"),
    Store("Farmacias del Ahorro", "https://www.fahorro.com",            "https://www.fahorro.com/catalogsearch/result/?q={query}"),
    Store("Farmatodo",            "https://www.farmatodo.com.mx",       "https://www.farmatodo.com.mx/buscar?product={query}"),
    Store("Farmacia San Pablo",   "https://www.farmaciasanpablo.com.mx","https://www.farmaciasanpablo.com.mx/search/?text={query}"),
    Store("Walmart",              "https://www.walmart.com.mx",         "https://www.walmart.com.mx/search?q={query}",         needs_browser=True),
    Store("Chedraui",             "https://www.chedraui.com.mx",        "https://www.chedraui.com.mx/search?text={query}",     needs_browser=True),
    Store("Soriana",              "https://www.soriana.com",            "https://www.soriana.com/buscar?q={query}",            needs_browser=True),
    Store("La Comer",             "https://www.lacomer.com.mx",         "https://www.lacomer.com.mx/lacomer/#!/search?text={query}"),
]

PRICE_RE = re.compile(r"\$\s?([0-9]{1,3}(?:[,\s]?[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)")
BLOCKED_RE = re.compile(
    r"captcha|access denied|forbidden|verifica que eres humano|verify you are human|unusual traffic|cloudflare",
    re.I,
)


# ── Helpers ─────────────────────────────────────────────────
def normalize_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value)
    match = re.search(r"([0-9]{1,3}(?:[,\s]?[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)", text)
    if not match:
        return None
    number = match.group(1).replace(",", "").replace(" ", "")
    try:
        val = round(float(number), 2)
        return val if 1 <= val <= 99999 else None
    except ValueError:
        return None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def similarity_score(query: str, title: str) -> int:
    q_words = {w for w in re.findall(r"[a-záéíóúüñ0-9]+", query.lower()) if len(w) > 2}
    t_words = {w for w in re.findall(r"[a-záéíóúüñ0-9]+", title.lower()) if len(w) > 2}
    return len(q_words & t_words) if q_words else 0


def absolute_url(store: Store, url: str) -> str:
    if not url:
        return store.base_url
    return urljoin(store.base_url, url)


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def choose_best(products: list[dict]) -> Optional[dict]:
    if not products:
        return None
    ranked = sorted(
        products,
        key=lambda p: (p.get("score", 0), p.get("fuente") == "json-ld", -p.get("precio", 0)),
        reverse=True,
    )
    best = ranked[0]
    best.pop("score", None)
    return best


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


# ── Extractores de JSON embebido ──────────────────────────────
def extract_next_data(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    """Extrae productos de __NEXT_DATA__ (Next.js — Walmart, Chedraui, etc.)"""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return []
    try:
        data = json.loads(tag.string or "")
    except Exception:
        return []

    products = []
    for item in walk_json(data):
        if not isinstance(item, dict):
            continue
        # Walmart.com.mx / Chedraui structure
        name = (item.get("name") or item.get("displayName") or
                item.get("productName") or item.get("title") or "")
        price = normalize_price(
            item.get("price") or item.get("salePrice") or item.get("regularPrice") or
            item.get("currentPrice") or item.get("priceInfo", {}).get("currentPrice") if isinstance(item.get("priceInfo"), dict) else None
        )
        url = item.get("canonicalUrl") or item.get("url") or item.get("pdpUrl") or ""
        if isinstance(name, str) and name and price:
            products.append({
                "tienda": store.name,
                "producto": clean_text(name)[:180],
                "precio": price,
                "precio_texto": f"${price:,.2f}",
                "url": absolute_url(store, url),
                "fuente": "next-data",
                "score": similarity_score(query, name),
            })
    return products


def extract_magento_json(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    """Extrae productos de scripts Magento (Benavides, Fahorro, Farmatodo, San Pablo)"""
    products = []
    for script in soup.find_all("script", type="text/x-magento-init"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in walk_json(data):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("product_name") or item.get("productName") or ""
            price = normalize_price(
                item.get("price") or item.get("final_price") or
                item.get("minimalPrice") or item.get("regularPrice")
            )
            url = item.get("product_url") or item.get("url") or ""
            if isinstance(name, str) and name and price:
                products.append({
                    "tienda": store.name,
                    "producto": clean_text(name)[:180],
                    "precio": price,
                    "precio_texto": f"${price:,.2f}",
                    "url": absolute_url(store, url),
                    "fuente": "magento-json",
                    "score": similarity_score(query, name),
                })
    return products


def extract_inline_json(soup: BeautifulSoup, store: Store, query: str) -> list[dict]:
    """Extrae productos de patrones window.__STATE__, dataLayer[], etc."""
    products = []
    patterns = [
        r"window\.__PRELOADED_STATE__\s*=\s*({.+?});?\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*({.+?});?\s*</script>",
        r"window\.__STORE__\s*=\s*({.+?});?\s*</script>",
        r'"products"\s*:\s*(\[.+?\])\s*[,}]',
    ]
    html = str(soup)
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except Exception:
            continue
        for item in walk_json(data):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("title") or ""
            price = normalize_price(item.get("price") or item.get("salePrice"))
            if isinstance(name, str) and name and price:
                products.append({
                    "tienda": store.name,
                    "producto": clean_text(name)[:180],
                    "precio": price,
                    "precio_texto": f"${price:,.2f}",
                    "url": absolute_url(store, item.get("url") or ""),
                    "fuente": "inline-json",
                    "score": similarity_score(query, name),
                })
        if products:
            break
    return products


# ── Parsers estándar ─────────────────────────────────────────
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
                    "url": absolute_url(store, item.get("url") or (offers.get("url", "") if isinstance(offers, dict) else "")),
                    "fuente": "json-ld",
                    "score": similarity_score(query, name),
                })
    return products


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
    for node in soup.find_all(["article", "li", "div"], limit=400):
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
    # JSON embebido primero (más confiable)
    products.extend(extract_next_data(soup, store, query))
    products.extend(extract_magento_json(soup, store, query))
    products.extend(extract_inline_json(soup, store, query))
    # Luego métodos estándar
    products.extend(parse_json_ld(soup, store, query))
    products.extend(parse_meta_product(soup, store, query))
    products.extend(parse_visible_prices(soup, store, query))

    best = choose_best(products)
    if best:
        best["url"] = best.get("url") or url
        best["estado"] = "Encontrado"
    return best


# ── Playwright browser scraper ───────────────────────────────
def scrape_store_browser(store: Store, query: str) -> Optional[dict]:
    """Abre la tienda con un navegador real para evitar bloqueos JS/Cloudflare."""
    if sync_playwright is None:
        return None

    url = store.search_url.format(query=quote_plus(query))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                locale="es-MX",
                viewport={"width": 1366, "height": 768},
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={
                    "sec-ch-ua": HEADERS["sec-ch-ua"],
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "none",
                    "sec-fetch-user": "?1",
                    "Accept-Language": "es-MX,es;q=0.9",
                },
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)

            # Aceptar cookies / banners
            for label in ["Aceptar", "Acepto", "Accept", "Entendido", "De acuerdo", "Acepto todo"]:
                try:
                    page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            # Esperar a que cargue contenido dinámico
            page.wait_for_timeout(2500)

            # Scroll para disparar lazy-load
            page.evaluate("window.scrollTo(0, 600)")
            page.wait_for_timeout(1000)

            html = page.content()
            browser.close()

        best = parse_product_page(html, store, query, url)
        if best:
            best["fuente"] = best.get("fuente", "html") + " (browser)"
        return best
    except Exception:
        return None


# ── DuckDuckGo fallback ──────────────────────────────────────
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
        if domain not in urlparse(href).netloc:
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
                best["fuente"] = best.get("fuente", "html") + " + DDG"
                return best
        except Exception:
            continue
    return None


# ── Scraper principal por tienda ─────────────────────────────
def scrape_store(
    session: requests.Session,
    store: Store,
    query: str,
    use_browser: bool = False,
) -> dict:
    url = store.search_url.format(query=quote_plus(query))
    search_error = ""

    # Capa 1: requests (rápido)
    should_try_requests = not store.needs_browser or not use_browser
    if should_try_requests:
        try:
            response = session.get(url, timeout=25)
            response.raise_for_status()
            if BLOCKED_RE.search(response.text):
                search_error = "Bloqueado — intentando con navegador..."
            else:
                best = parse_product_page(response.text, store, query, url)
                if best:
                    return best
        except Exception as exc:
            search_error = str(exc)

    # Capa 2: Playwright (si use_browser o si la tienda lo requiere)
    if use_browser or store.needs_browser:
        best = scrape_store_browser(store, query)
        if best:
            return best

    # Capa 3: DuckDuckGo
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
        "estado": search_error or "Sin precio encontrado.",
    }


def compare_prices(query: str, progress=None, use_browser: bool = False) -> list[dict]:
    def log(message: str):
        if progress:
            progress(message)

    session = requests.Session()
    session.headers.update(HEADERS)

    mode = "navegador + requests" if use_browser else "requests + JSON embebido"
    log(f"Iniciando comparación de precios: '{query}' [{mode}]")
    results = []

    for index, store in enumerate(STORES, start=1):
        log(f"[{index}/{len(STORES)}] {store.name}...")
        try:
            result = scrape_store(session, store, query, use_browser=use_browser)
            results.append(result)
            if result.get("precio") is not None:
                log(f"✓ {store.name}: {result['precio_texto']} — {result['producto'][:60]}")
            else:
                log(f"✗ {store.name}: {result.get('estado', 'Sin resultado')}")
        except Exception as exc:
            results.append({
                "tienda": store.name,
                "producto": "",
                "precio": None,
                "precio_texto": "",
                "url": url if (url := store.search_url.format(query=quote_plus(query))) else "",
                "fuente": "error",
                "estado": str(exc),
            })
            log(f"Error en {store.name}: {exc}")
        time.sleep(0.6 if not use_browser else 1.2)

    found = [r for r in results if r.get("precio") is not None]
    if found:
        lowest = min(found, key=lambda r: r["precio"])
        log(f"Mejor precio: {lowest['tienda']} — {lowest['precio_texto']}")

    log(f"DONE:{len(found)}")
    return sorted(results, key=lambda r: r["precio"] if r.get("precio") is not None else 10**9)

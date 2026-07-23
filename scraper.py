"""
Módulo de scraping para Sección Amarilla México.
Acepta parámetros dinámicos de búsqueda y reporta progreso por callback.
"""
import requests
import json
import csv
import time
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

BASE_URL = "https://www.seccionamarilla.com.mx"
SEARCH_URL = f"{BASE_URL}/buscar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9",
    "Referer": BASE_URL,
}


def clean_website(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        return f"{BASE_URL}{url}"
    if "minisitios.seccionamarilla.com.mx" in url:
        slug = url.split("minisitios.seccionamarilla.com.mx/")[-1].split("?")[0]
        return f"https://minisitios.seccionamarilla.com.mx/{slug}"
    return url.split("?utm_source=")[0].split("?")[0]


def extract_whatsapp_number(href: str) -> str:
    if not href:
        return ""
    match = re.search(r"phone=(\d+)", href)
    if match:
        number = match.group(1)
        if number.startswith("521"):
            number = number[3:]
        elif number.startswith("52") and len(number) > 12:
            number = number[2:]
        return number
    return ""


def extract_phone_number(value: str) -> str:
    if not value:
        return ""
    value = value.replace("tel:", "")
    match = re.search(r"[\d()\-\s+]{7,}", value)
    return re.sub(r"\s+", "", match.group(0)).strip() if match else ""


def parse_legacy_rows(soup: BeautifulSoup) -> list:
    rows = soup.find_all("tr", class_="table-row")
    results = []

    for row in rows:
        name_tag = row.find("h2", class_="bussines_name")
        if not name_tag:
            continue
        name = name_tag.get_text(strip=True)

        name_link = name_tag.find("a")
        website = clean_website(name_link.get("href", "")) if name_link else ""

        addr_tags = row.find_all("small", class_="short_address")
        address_parts = []
        for addr in addr_tags:
            text = addr.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip(", ")
            text = re.sub(r",?\s*C\.P\.?\s*$", "", text).strip(", ")
            if text:
                address_parts.append(text)
        address = " | ".join(address_parts)

        phone_tag = row.find("a", class_="color-llamar")
        phone = phone_tag.get("data-number", "").strip() if phone_tag else ""

        wa_tag = row.find("a", class_=re.compile(r"color-whatsapp"))
        whatsapp = extract_whatsapp_number(wa_tag.get("href", "")) if wa_tag else ""

        email_match = re.search(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", str(row)
        )
        email = email_match.group(0) if email_match else ""

        results.append({
            "nombre": name,
            "telefono": phone,
            "whatsapp": whatsapp,
            "direccion": address,
            "sitio_web": website,
            "email": email,
        })

    return results


def parse_article_cards(soup: BeautifulSoup) -> list:
    articles = soup.find_all("article", attrs={"data-name": True})
    results = []

    for article in articles:
        name = article.get("data-name", "").strip()
        if not name:
            name_tag = article.find("h2")
            name = name_tag.get_text(" ", strip=True) if name_tag else ""
        if not name:
            continue

        website = ""
        website_link = article.find("a", attrs={"data-click-type": "pagina_web"})
        if not website_link:
            website_link = article.select_one("h2 a.business-click")
        if website_link:
            website = clean_website(website_link.get("href", ""))

        phone = extract_phone_number(article.get("data-phone", ""))
        if not phone:
            phone_link = article.find("a", href=re.compile(r"^tel:", re.I))
            phone = extract_phone_number(phone_link.get("href", "")) if phone_link else ""

        wa_tag = article.find("a", href=re.compile(r"whatsapp\.com/send", re.I))
        whatsapp = extract_whatsapp_number(wa_tag.get("href", "")) if wa_tag else ""

        address = article.get("data-address", "").strip()
        if not address:
            address_tag = article.find(string=re.compile(r"\bCP\s*\d{5}\b", re.I))
            address = re.sub(r"\s+", " ", address_tag.strip()) if address_tag else ""

        email_match = re.search(
            r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", str(article)
        )
        email = email_match.group(0) if email_match else ""

        results.append({
            "nombre": name,
            "telefono": phone,
            "whatsapp": whatsapp,
            "direccion": address,
            "sitio_web": website,
            "email": email,
        })

    return results


def parse_page(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    results = parse_legacy_rows(soup)
    if results:
        return results
    return parse_article_cards(soup)


def get_pagination_info(html: str, current_url: str = "") -> tuple:
    """Devuelve (base_url_paginacion, total_pages)."""
    soup = BeautifulSoup(html, "lxml")
    pages = []
    base_url = None

    pagination = soup.find(class_="pagination")
    links = pagination.find_all("a", class_="page-link") if pagination else []

    if not links:
        links = [
            a for a in soup.find_all("a", href=True)
            if any(str(cls).startswith("paginacion-item") for cls in a.get("class", []))
        ]

    for a in links:
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if text.isdigit() and href:
            pages.append(int(text))
            base_url = re.sub(r"/\d+$", "", href)

    if current_url:
        current_match = re.search(r"/(\d+)$", urlparse(current_url).path)
        if current_match:
            pages.append(int(current_match.group(1)))
            if not base_url:
                base_url = re.sub(r"/\d+$", "", current_url)

    total = max(pages) if pages else 1
    return base_url, total


def build_page_url(base_url: str, page: int) -> str:
    if not base_url:
        return ""
    return urljoin(BASE_URL, f"{base_url.rstrip('/')}/{page}")


def scrape_all(query: str, location: str, progress=None) -> list:
    """
    Ejecuta el scraping completo.
    progress(msg: str) se llama con cada actualización de estado.
    """
    def log(msg):
        if progress:
            progress(msg)

    session = requests.Session()
    session.headers.update(HEADERS)

    log(f"Iniciando búsqueda: '{query}' en '{location}'")

    r = session.post(
        SEARCH_URL,
        data={
            "flagUseSppeach": "False",
            "typefieldQue": query,
            "typefieldDonde": location,
        },
        timeout=20,
    )
    r.raise_for_status()

    base_url, total_pages = get_pagination_info(r.text, r.url)
    log(f"Páginas encontradas: {total_pages}")

    all_results = parse_page(r.text)
    log(f"Página 1/{total_pages} — {len(all_results)} resultados")

    for page in range(2, total_pages + 1):
        time.sleep(1.2)
        url = build_page_url(base_url, page)
        if not url:
            break
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            page_results = parse_page(r.text)
            all_results.extend(page_results)
            log(f"Página {page}/{total_pages} — {len(page_results)} resultados (acumulado: {len(all_results)})")
        except Exception as e:
            log(f"Error en página {page}: {e}")

    # Deduplicar
    seen = set()
    unique = []
    for item in all_results:
        key = item["nombre"].upper().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    log(f"DONE:{len(unique)}")
    return unique


def save_results(results: list, base_name: str) -> tuple:
    """Guarda JSON y CSV, devuelve (json_path, csv_path)."""
    json_path = f"{base_name}.json"
    csv_path = f"{base_name}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    fields = ["nombre", "telefono", "whatsapp", "direccion", "sitio_web", "email"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    return json_path, csv_path


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "contadores"
    loc = sys.argv[2] if len(sys.argv) > 2 else "Ciudad de Mexico"
    results = scrape_all(q, loc, progress=print)
    print(f"\nTotal: {len(results)}")
    save_results(results, f"{q.replace(' ', '_')}_{loc.replace(' ', '_')}")

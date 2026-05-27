"""
Enriquecimiento público de empresas con señales de LinkedIn/liderazgo.
No inicia sesión ni intenta saltarse bloqueos de LinkedIn; usa resultados web
públicos y devuelve una inferencia con fuente y confianza.
"""
import re
import time
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.7",
}

ROLE_RE = re.compile(
    r"\b(CEO|Chief Executive Officer|Director General|Directora General|Gerente General|"
    r"Founder|Co-Founder|Fundador|Fundadora|Dueño|Dueña|Owner|Propietario|Propietaria)\b",
    re.I,
)
NAME_ROLE_RE = re.compile(
    r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.-]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.-]+){1,4})"
    r"(?:\s+[-–|,]\s+|\s+)(CEO|Chief Executive Officer|Director General|Directora General|Gerente General|"
    r"Founder|Co-Founder|Fundador|Fundadora|Dueño|Dueña|Owner|Propietario|Propietaria)",
    re.I,
)
ROLE_NAME_RE = re.compile(
    r"(CEO|Chief Executive Officer|Director General|Directora General|Gerente General|"
    r"Founder|Co-Founder|Fundador|Fundadora|Dueño|Dueña|Owner|Propietario|Propietaria)"
    r"(?:\s+[-–|,]\s+|\s+de\s+|\s+at\s+)"
    r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.-]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.-]+){1,4})",
    re.I,
)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_google_url(url: str) -> str:
    if url.startswith("/url?"):
        parsed = urlparse(url)
        return unquote(parse_qs(parsed.query).get("q", [""])[0])
    return url


def google_search(session: requests.Session, query: str, limit: int = 6) -> list[dict]:
    url = f"https://www.google.com/search?hl=es-419&gl=mx&q={quote_plus(query)}"
    response = session.get(url, timeout=18)
    if response.status_code in (429, 503):
        return []
    response.raise_for_status()
    if "tráfico inusual" in response.text.lower() or "unusual traffic" in response.text.lower():
        return []

    soup = BeautifulSoup(response.text, "lxml")
    results = []
    for block in soup.select("div.g, div.tF2Cxc, div.MjjYud"):
        link = block.find("a", href=True)
        if not link:
            continue
        href = clean_google_url(link.get("href", ""))
        if not href.startswith("http"):
            continue
        title = clean_text(block.find("h3").get_text(" ", strip=True)) if block.find("h3") else clean_text(link.get_text(" ", strip=True))
        snippet = clean_text(block.get_text(" ", strip=True))
        if title or snippet:
            results.append({"title": title, "snippet": snippet, "url": href})
        if len(results) >= limit:
            break
    return results


def bing_search(session: requests.Session, query: str, limit: int = 6) -> list[dict]:
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    response = session.get(url, timeout=18)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    results = []
    for block in soup.select("li.b_algo"):
        link = block.find("a", href=True)
        if not link:
            continue
        title = clean_text(link.get_text(" ", strip=True))
        snippet = clean_text(block.get_text(" ", strip=True))
        results.append({"title": title, "snippet": snippet, "url": link.get("href", "")})
        if len(results) >= limit:
            break
    return results


def public_search(session: requests.Session, query: str, limit: int = 6) -> list[dict]:
    results = google_search(session, query, limit=limit)
    if results:
        return results
    return bing_search(session, query, limit=limit)


def extract_leader_from_text(text: str) -> tuple[str, str]:
    text = clean_text(text)

    match = NAME_ROLE_RE.search(text)
    if match:
        return clean_text(match.group(1)), clean_text(match.group(2))

    match = ROLE_NAME_RE.search(text)
    if match:
        return clean_text(match.group(2)), clean_text(match.group(1))

    return "", ""


def best_linkedin_url(results: list[dict], kind: str) -> str:
    needle = "linkedin.com/company" if kind == "company" else "linkedin.com/in"
    for result in results:
        url = result.get("url", "")
        if needle in url:
            return url.split("?")[0]
    return ""


def infer_leader(company_name: str, location: str = "", session: requests.Session = None) -> dict:
    session = session or requests.Session()
    session.headers.update(HEADERS)

    base = f'"{company_name}"'
    if location:
        base += f' "{location}"'

    company_results = []
    people_results = []
    try:
        company_results = public_search(session, f'{base} LinkedIn empresa', limit=5)
        time.sleep(0.5)
        people_results = public_search(
            session,
            f'{base} CEO "Director General" dueño founder fundador LinkedIn',
            limit=8,
        )
        time.sleep(0.5)
        leadership_results = public_search(
            session,
            f'{base} CEO "Director General" dueño founder fundador',
            limit=8,
        )
    except Exception as exc:
        return {
            "linkedin_url": "",
            "lider_nombre": "",
            "lider_cargo": "",
            "lider_fuente": str(exc),
            "lider_confianza": "error",
        }

    company_url = best_linkedin_url(company_results, "company")
    person_url = best_linkedin_url(people_results, "person")
    leader_name = ""
    leader_role = ""
    source = ""
    confidence = "baja"

    for result in people_results + leadership_results + company_results:
        text = f"{result.get('title', '')} {result.get('snippet', '')}"
        name, role = extract_leader_from_text(text)
        if name and role:
            leader_name = name
            leader_role = role
            source = result.get("url", "")
            confidence = "media" if "linkedin.com/in" in source else "baja"
            break

    if person_url and not source:
        source = person_url
    if person_url and confidence == "baja":
        confidence = "media"

    return {
        "linkedin_url": company_url or person_url,
        "lider_nombre": leader_name,
        "lider_cargo": leader_role,
        "lider_fuente": source,
        "lider_confianza": confidence if leader_name or person_url or company_url else "sin dato",
    }

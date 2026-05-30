"""
Enriquecimiento público de empresas con señales de LinkedIn/liderazgo.
No inicia sesión ni intenta saltarse bloqueos de LinkedIn; usa resultados web
públicos. Si hay ANTHROPIC_API_KEY disponible, usa Claude Haiku para la
extracción; si no, cae al método regex original.
"""
import json
import os
import re
import time
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup


# ── Claude AI para extracción de líderes ─────────────────────
_anthropic_client = None
try:
    import anthropic as _anthropic_mod
    _api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if _api_key:
        _anthropic_client = _anthropic_mod.Anthropic(api_key=_api_key)
except Exception:
    pass


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


def _infer_leader_with_ai(company_name: str, all_results: list[dict]) -> Optional[dict]:
    if not _anthropic_client or not all_results:
        return None

    results_text = "\n".join(
        f"[{i+1}] {r.get('title', '')}\n    {r.get('snippet', '')}\n    URL: {r.get('url', '')}"
        for i, r in enumerate(all_results[:14])
    )

    prompt = (
        f'Analiza estos resultados de búsqueda web sobre la empresa "{company_name}" '
        f'y extrae la información del líder principal '
        f'(CEO, Director General, Gerente General, dueño, fundador, o equivalente).\n\n'
        f'Resultados:\n{results_text}\n\n'
        f'Responde SOLO con JSON válido (usa "" si no hay datos suficientes). '
        f'Para las URLs de LinkedIn incluye solo la URL limpia sin parámetros:\n'
        f'{{"lider_nombre":"","lider_cargo":"","linkedin_empresa":"","linkedin_persona":""}}'
    )

    try:
        msg = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=220,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception:
        pass
    return None


def infer_leader(company_name: str, location: str = "", session: requests.Session = None) -> dict:
    session = session or requests.Session()
    session.headers.update(HEADERS)

    base = f'"{company_name}"'
    if location:
        base += f' "{location}"'

    company_results = []
    people_results = []
    leadership_results = []
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
    all_results = company_results + people_results + leadership_results

    # ── Extracción con IA ─────────────────────────────────────
    ai = _infer_leader_with_ai(company_name, all_results)
    if ai:
        leader_name = (ai.get("lider_nombre") or "").strip()
        leader_role = (ai.get("lider_cargo") or "").strip()
        li_empresa = (ai.get("linkedin_empresa") or "").strip()
        li_persona = (ai.get("linkedin_persona") or "").strip()

        if li_empresa and "linkedin.com/company" in li_empresa:
            company_url = li_empresa.split("?")[0]
        if li_persona and "linkedin.com/in" in li_persona:
            person_url = li_persona.split("?")[0]

        if leader_name and person_url:
            confidence = "alta"
        elif leader_name and leader_role:
            confidence = "media"
        elif company_url or person_url:
            confidence = "baja"
        else:
            confidence = "sin dato"

        return {
            "linkedin_url": company_url or person_url,
            "lider_nombre": leader_name,
            "lider_cargo": leader_role,
            "lider_fuente": person_url or company_url or (all_results[0].get("url") if all_results else ""),
            "lider_confianza": confidence,
        }

    # ── Fallback: regex ───────────────────────────────────────
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

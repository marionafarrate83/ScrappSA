"""
Barrido de actividad economica por zona usando Google Maps.
"""
from collections import defaultdict
from typing import Optional

import maps_scraper as ms


DEFAULT_GIROS = [
    "restaurantes",
    "cafeterias",
    "taquerias",
    "pollerias",
    "carnicerias",
    "tortillerias",
    "panaderias",
    "abarrotes",
    "tiendas de autoservicio",
    "ferreterias",
    "farmacias",
    "peluquerias",
    "esteticas",
    "lavanderias",
    "talleres mecanicos",
    "refaccionarias",
    "veterinarias",
    "consultorios medicos",
    "dentistas",
    "gimnasios",
    "bares",
    "hoteles",
    "bancos",
    "gasolineras",
]


def _business_key(item: dict) -> tuple:
    name = (item.get("nombre") or "").lower().strip()
    lat = str(item.get("latitud") or "")
    lng = str(item.get("longitud") or "")
    if name and lat and lng:
        return ("name_geo", name, lat, lng)
    maps_url = item.get("google_maps_url") or ""
    if maps_url:
        return ("url", maps_url)
    return (
        "name",
        name,
    )


def _normalize_giro(value: str) -> str:
    return ms.clean_text(value).lower()


def _parse_giros(raw: Optional[str]) -> list[str]:
    if not raw:
        return DEFAULT_GIROS
    giros = [_normalize_giro(part) for part in raw.replace("\n", ",").split(",")]
    return [giro for giro in giros if giro] or DEFAULT_GIROS


def scan_activity(location: str, filters: Optional[dict] = None, progress=None) -> list[dict]:
    filters = filters or {}

    def log(message: str):
        if progress:
            progress(message)

    giros = _parse_giros(filters.get("giros"))
    try:
        per_giro_limit = max(5, min(80, int(filters.get("per_giro_limit") or 25)))
    except (TypeError, ValueError):
        per_giro_limit = 25

    query_filters = {
        "radius_km": filters.get("radius_km") or "",
        "max_results": per_giro_limit,
        "use_browser": bool(filters.get("use_browser", False)),
    }

    rows = []
    global_seen = set()

    log(f"Peinando actividad economica en {location}...")
    for index, giro in enumerate(giros, start=1):
        log(f"Giro {index}/{len(giros)}: {giro}")
        try:
            results = ms.scrape_maps(giro, location, filters=query_filters, progress=progress)
        except Exception as exc:
            rows.append({
                "giro": giro,
                "conteo": 0,
                "con_telefono": 0,
                "con_sitio_web": 0,
                "categorias_detectadas": "",
                "negocios": "",
                "estado": f"Error: {exc}",
            })
            continue

        unique = []
        local_seen = set()
        category_counts = defaultdict(int)
        for item in results:
            key = _business_key(item)
            if key in local_seen:
                continue
            local_seen.add(key)
            global_seen.add(key)
            unique.append(item)
            for category in (item.get("categoria") or "").split(","):
                category = ms.clean_text(category)
                if category:
                    category_counts[category] += 1

        category_summary = ", ".join(
            category for category, _ in sorted(category_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        )
        business_names = " | ".join(item.get("nombre", "") for item in unique[:20] if item.get("nombre"))
        rows.append({
            "giro": giro,
            "conteo": len(unique),
            "con_telefono": len([item for item in unique if item.get("telefono")]),
            "con_sitio_web": len([item for item in unique if item.get("sitio_web")]),
            "categorias_detectadas": category_summary,
            "negocios": business_names,
            "estado": "Encontrado" if unique else "Sin resultados",
        })

    rows.sort(key=lambda row: (-int(row.get("conteo") or 0), row.get("giro") or ""))
    log(f"DONE:{len(global_seen)}")
    return rows

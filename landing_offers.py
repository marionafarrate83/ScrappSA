"""Versiones de la portada para campañas.

Cada versión solo cambia mensaje, beneficio, imagen, CTA y caso de uso destacado.
La identidad, el precio y las condiciones son siempre los mismos.
Se activa con ?v=<clave>, por ejemplo /?v=agencias. Ver BRANDING.md.
"""
from urllib.parse import urlencode

DEFAULT_OFFER = {
    'headline': 'Tus próximos clientes',
    'highlight': 'están aquí.',
    'subtitle': 'Encuentra empresas mexicanas por giro y ubicación, genera tus propias listas '
                'de prospectos y exporta tus resultados a Excel.',
    'benefit': 'Más de 2 millones de empresas',
    # Ruta dentro de static/. Vacío: se muestra la representación del producto.
    'hero_image': '',
    'hero_image_alt': '',
    'cta': 'Empieza a prospectar',
    # vendedores, agencias o distribuidores. Vacío: ninguno destacado.
    'featured_case': '',
}

OFFER_VARIANTS = {
    'vendedores': {
        'headline': 'Encuentra a quién',
        'highlight': 'venderle.',
        'subtitle': 'Filtra empresas mexicanas por giro y zona, arma tu lista de prospectos y '
                    'registra el seguimiento de cada contacto.',
        'benefit': 'Tus prospectos y su seguimiento en un solo lugar',
        'featured_case': 'vendedores',
    },
    'agencias': {
        'headline': 'Prospectos para',
        'highlight': 'tu agencia.',
        'subtitle': 'Busca negocios mexicanos por giro y ubicación, revisa los datos de contacto '
                    'y sitios web disponibles y exporta tu lista a Excel.',
        'benefit': 'Listas segmentadas para tus campañas de venta',
        'featured_case': 'agencias',
    },
    'distribuidores': {
        'headline': 'Lleva tu producto a',
        'highlight': 'más negocios.',
        'subtitle': 'Ubica negocios por giro, estado y municipio, arma listas por zona y '
                    'exporta tus resultados a Excel.',
        'benefit': 'Encuentra puntos de venta por zona',
        'featured_case': 'distribuidores',
    },
}


def landing_offer(key=None):
    """Oferta solicitada; las claves desconocidas muestran la versión principal."""
    return {**DEFAULT_OFFER, **OFFER_VARIANTS.get((key or '').strip().lower(), {})}


def utm_suffix(args):
    """Conserva los parámetros utm_* entre páginas. No los guarda ni los envía a terceros."""
    kept = [(name, value[:200]) for name, value in args.items(multi=True) if name.startswith('utm_')]
    return f'?{urlencode(kept[:10])}' if kept else ''

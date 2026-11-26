# Sistema de diseño de ScrapSA.app

**Concepto:** inteligencia comercial accesible. **Eslogan:** Encuentra. Prospecta. Vende.

Personalidad: tecnológica, directa, profesional, ágil, moderna, accesible y orientada a resultados.

## 1. Nombre

| Uso | Forma |
|---|---|
| Marca, títulos y logotipo | `ScrapSA.app` |
| Nombre del plan, remitente y archivos exportados | `ScrapSA` |
| Identificadores internos (base de datos, salt, IndexedDB) | `scrappsa` / `scrapsa` en minúsculas. **No cambiarlos:** se perderían usuarios, enlaces o listas |

## 2. Logotipo

Archivos en `static/brand/img/`:

| Archivo | Uso |
|---|---|
| `logo.svg` | Horizontal sobre fondo azul marino: Scrap blanco, SA lima, .app gris |
| `logo-mono.svg` | Un solo color (`currentColor`; se aplica al insertar el SVG en línea) |
| `isotype.svg` | Tres barras ascendentes |
| `favicon.svg`, `favicon.ico` | Pestaña del navegador (16, 32 y 48 px) |
| `apple-touch-icon.png`, `icon-192.png`, `icon-512.png` | Iconos de móvil (`site.webmanifest`) |
| `icon-square.svg` | Fuente de los PNG |

Geometría del isotipo, medida sobre la referencia de campaña (lienzo 111.5 × 80):

```
M0 55H21.5L34 80H12.5Z  M25 27H46.5L73 80H51.5Z  M50 0H71.5L111.5 80H90Z
```

El texto del logotipo es Inter 600 convertido a trazos, así que no depende de que cargue la fuente. La referencia fue una captura de pantalla: si existe el vector original de la campaña, debe sustituir a estos archivos.

Reglas: no deformar, rotar ni recolorear las barras; usar la versión a color solo sobre azul marino; dejar alrededor un margen de al menos el ancho de una barra; ancho mínimo del logotipo horizontal: 128 px.

## 3. Color

Los colores se definen **solo** en `static/brand/tokens.css`; una prueba lo verifica.

| Token | Valor | Uso |
|---|---|---|
| `--brand-primary`, `--background`, `--on-accent` | `#0B1526` | Fondo dominante y texto sobre lima |
| `--brand-secondary`, `--surface` | `#142238` | Tarjetas y paneles |
| `--surface-elevated` | `#1C2D45` | Encabezados de tabla, hover |
| `--border` | `#2B3D55` | Separadores |
| `--border-control` | `#6B82A6` | Contorno de campos y botones secundarios |
| `--brand-accent` | `#C6F432` | Acción principal, búsqueda, exportación, selección, precio, enlaces y pines |
| `--brand-accent-hover` | `#A8D91C` | Hover del botón principal |
| `--text-primary` | `#F8FAFC` | Texto principal |
| `--text-secondary` | `#CBD5E1` | Texto de apoyo |
| `--text-muted` | `#94A3B8` | Metadatos |
| `--success` `--warning` `--info` | `#22C55E` `#F59E0B` `#38BDF8` | Estados |
| `--danger` | `#EF4444` | Bordes y fondos de error |
| `--danger-text` | `#F87171` | Texto de error |

Contraste (WCAG AA):

- Azul marino sobre lima: 14.3:1.
- `--text-muted` sobre las tres superficies: 5.4:1 o más.
- `--danger` no alcanza 4.5:1 como texto sobre las superficies: usar `--danger-text`.
- `--border` no alcanza 3:1: no usarlo para delimitar controles.
- El lima sobre blanco (1.3:1) **no se usa para texto**.

Excepciones obligadas, que repiten valores porque no admiten variables CSS: la flecha de los `select` (SVG en `directory.css`), el correo de confirmación (`app.py`), los SVG de marca, `site.webmanifest` y la etiqueta `theme-color`.

Evitar: lima en bloques grandes, brillos neón, degradados decorativos y sombras fuera de los diálogos.

## 4. Tipografía

Inter variable, alojada en `static/brand/fonts/` (latín y latín extendido, licencia OFL incluida). Sin peticiones a terceros.

| Elemento | Peso |
|---|---|
| Titulares comerciales | 800 |
| Encabezados de sección | 600–700 |
| Texto | 400–500 |
| Etiquetas y botones | 600 |

Los datos numéricos usan `font-variant-numeric: tabular-nums`.

Radios: `--radius-sm` 6 px, `--radius-md` 10 px, `--radius-lg` 16 px.

## 5. Archivos de estilo

| Archivo | Alcance |
|---|---|
| `brand/tokens.css` | Variables |
| `brand/base.css` | Fuente, reinicio, foco, barra superior, movimiento reducido |
| `brand/landing.css` | Portada (mobile first) |
| `brand/auth.css` | Acceso, registro y compra |
| `brand/app.css` | Herramientas, Maps y Actividad económica |
| `directory.css` | Directorio, listas, tablas y diálogos |
| `brand/admin.css` | Administración |

Toda página incluye `templates/partials/brand_head.html`; el logotipo se inserta con `templates/partials/logo.html`.

## 6. Componentes

| Componente | Clases |
|---|---|
| Botón principal | `.btn.btn-primary` (portada, admin), `.btn` (acceso y compra), `.btn-search`, `.dir-btn.dir-primary` |
| Botón secundario | `.btn-ghost`, `.dir-btn`, `.btn-dl`, `.topbar-link` |
| Exportación | Lima: relleno en `.btn-dl.csv`; contorno en los botones de CSV del Directorio y las listas |
| Selección | Fondo `--accent-tint` y borde o marca lima |
| Estados | `.pill-*`, `.error`, `.notice`, `.dir-alert`, `.dir-empty`, `.empty-state` |
| Carga | `.spinner` y barra superior en la tabla con `aria-busy="true"` |

Móvil: los filtros del Directorio se pliegan; por debajo de 600 px sus resultados se muestran como tarjetas. Las demás tablas se desplazan dentro de su contenedor.

## 7. Portada y campañas

La portada se arma con los módulos de `templates/landing/`. El mensaje de cada campaña vive en `landing_offers.py`.

| URL | Versión |
|---|---|
| `/` | Principal |
| `/?v=vendedores` | Vendedores independientes |
| `/?v=agencias` | Agencias y freelancers |
| `/?v=distribuidores` | Distribuidores y proveedores |

Cada versión puede cambiar `headline`, `highlight`, `subtitle`, `benefit`, `hero_image` (ruta dentro de `static/`), `hero_image_alt`, `cta` y `featured_case`. Para crear otra, agrega una entrada a `OFFER_VARIANTS`. El precio y las condiciones salen de `STRIPE_PLANS` y no cambian entre versiones.

La etiqueta `canonical` apunta a `https://www.scrapsa.app/`, para que las versiones no compitan en buscadores. Quien ya inició sesión es redirigido a la aplicación.

### UTM y medición

Los parámetros `utm_*` pasan de la portada y el acceso al checkout. **No se guardan, no se envían a Stripe y no se registran**, y se pierden al salir hacia Stripe o al volver de un pago cancelado.

El proyecto no tiene Meta Pixel, API de conversiones ni analítica. Antes de agregarlos: definir el aviso de privacidad y el consentimiento, y registrar la compra solo con el pago confirmado (webhook o sesión `paid`).

## 8. Redacción

- Frases cortas y directas, en segunda persona.
- No inventar estadísticas, testimonios, clientes ni resultados.
- Las representaciones del producto llevan «datos ficticios».
- Decir que la exportación es CSV (compatible con Excel) o JSON.

## 9. Diagnóstico del branding anterior

- Fondo claro con amarillo: apariencia de herramienta administrativa.
- 322 colores escritos a mano (118 distintos) y ocho páginas con estilos propios.
- Dos símbolos distintos como logotipo, sin favicon, y tipografía del sistema.
- En móvil, los filtros ocupaban la pantalla antes de los resultados; con cuenta de administrador, la aplicación y el panel se desbordaban a 390 px.

Comparativas con datos ficticios en `docs/rebranding/`.

## 10. Pendientes conocidos

- No hay aviso de privacidad, términos ni contacto que enlazar desde el pie.
- No existen recuperación de contraseña ni pantalla de perfil.
- No hay mapa integrado: la aplicación solo enlaza a Google Maps.

## 11. Verificación

```bash
.venv/bin/python -m unittest discover -s tests
```

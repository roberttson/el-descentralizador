"""
Extractor del cuerpo completo de un artículo para el lector integrado.

Visita la página original, localiza el bloque con el texto de la noticia
(el contenedor que concentra más párrafos), elimina menús/publicidad y
devuelve HTML saneado: solo párrafos, subtítulos, listas, citas e imágenes.

El resultado se cachea en la columna `contenido` de noticias.db, así cada
artículo se extrae una sola vez.
"""

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ingestar import PATRON_BASURA, HEADERS

TIMEOUT = 20
MIN_TEXTO = 250          # menos que esto no parece un artículo
MAX_HTML = 300_000       # tope de seguridad para el HTML cacheado

# Módulos de página ajenos al cuerpo (además de PATRON_BASURA de la ingesta)
PATRON_NAVEGACION = re.compile(
    r"(^|[-_ ])(menu|nav|navbar|sidebar|breadcrumbs?|cookie|newsletter|"
    r"suscripcion|subscribe|author|autor|tags|pagination|paginacion|"
    r"copyright|entry-footer|post-meta|caja-poll|encuesta)([-_ ]|$)", re.I)

ETIQUETAS_OK = {"p", "h2", "h3", "h4", "blockquote", "ul", "ol", "li",
                "strong", "em", "b", "i", "a", "img", "figure", "figcaption"}

IMG_BASURA = ("emoji", "gravatar", "avatar", "pixel", "1x1", "blank.",
              "spacer", "logo", "icon")


def _puntaje(el):
    """Cantidad de texto en párrafos que contiene el elemento."""
    return sum(len(p.get_text(strip=True)) for p in el.find_all("p"))


def _mejor_contenedor(soup):
    """El elemento que concentra el texto del artículo."""
    candidatos = soup.find_all(["article", "main", "section", "div"])
    if not candidatos:
        return soup.body or soup
    mejor = max(candidatos, key=_puntaje)
    if _puntaje(mejor) < MIN_TEXTO:
        return None
    # Descender mientras un único hijo concentre casi todo el texto
    while True:
        total = _puntaje(mejor)
        hijo = next((h for h in mejor.find_all(
            ["article", "main", "section", "div"], recursive=False)
            if _puntaje(h) >= 0.9 * total), None)
        if hijo is None:
            return mejor
        mejor = hijo


def _sanear(nodo, base_url, omitir_imagenes):
    """Deja solo etiquetas de la lista blanca, con atributos mínimos."""
    imagenes_vistas = set(omitir_imagenes)
    for tag in list(nodo.find_all(True)):
        if tag.decomposed or tag.parent is None:
            continue
        if tag.name == "img":
            src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src") or ""
            src = urljoin(base_url, src.strip())
            if (not src.startswith("http") or src in imagenes_vistas
                    or any(p in src.lower() for p in IMG_BASURA)):
                tag.decompose()
                continue
            imagenes_vistas.add(src)
            alt = tag.get("alt", "")
            tag.attrs = {"src": src, "loading": "lazy"}
            if alt:
                tag.attrs["alt"] = alt
        elif tag.name == "a":
            href = urljoin(base_url, (tag.get("href") or "").strip())
            if href.startswith("http"):
                tag.attrs = {"href": href, "target": "_blank", "rel": "noopener"}
            else:
                tag.unwrap()
        elif tag.name in ETIQUETAS_OK:
            tag.attrs = {}
        else:
            tag.unwrap()

    # Párrafos vacíos fuera
    for p in nodo.find_all(["p", "figure", "li"]):
        if not p.get_text(strip=True) and not p.find("img"):
            p.decompose()


def extraer_articulo(url, imagen_portada=None):
    """Devuelve el cuerpo del artículo como HTML saneado, o '' si no se pudo."""
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    for tag in soup(["script", "style", "iframe", "form", "nav", "aside",
                     "footer", "header", "noscript", "ins", "button", "svg",
                     "object", "embed", "select", "input", "h1"]):
        tag.decompose()
    # Borrar módulos de basura, pero jamás un contenedor estructural ni uno
    # que concentre el grueso del texto (hay temas que ponen clases tipo
    # "side-widget-…" en el propio <body>)
    total = _puntaje(soup)
    for patron in (PATRON_BASURA, PATRON_NAVEGACION):
        for attr in ("class", "id"):
            for tag in soup.find_all(attrs={attr: patron}):
                if tag.name in ("html", "body", "main", "article"):
                    continue
                if total and _puntaje(tag) >= 0.5 * total:
                    continue
                tag.decompose()

    cuerpo = _mejor_contenedor(soup)
    if cuerpo is None:
        return ""

    omitir = {imagen_portada} if imagen_portada else set()
    _sanear(cuerpo, r.url, omitir)

    if len(cuerpo.get_text(strip=True)) < MIN_TEXTO:
        return ""
    html = "".join(str(c) for c in cuerpo.children).strip()
    return html[:MAX_HTML]

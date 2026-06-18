"""
Scrapers HTML para medios sin RSS público.

Cada scraper devuelve una lista de "entradas" con el mismo shape que
las entries de feedparser, para que `ingestar.py` las consuma sin cambios
adicionales:

    {
        "link": str,         # URL absoluta del artículo
        "title": str,
        "published": str,    # ISO 8601 (UTC) o ""
        "summary": str,      # HTML o texto (la ingesta lo limpia igual)
        "image": str,        # URL absoluta (opcional)
    }

El dispatcher `scrapear(sitio_url)` decide qué scraper usar según el dominio.
Si no hay scraper para el dominio, devuelve None.

REGISTRO de fuentes que usan scraper en lugar de RSS:
    Las filas del CSV con `tiene_rss=False` y `feed_url=""` no se ingestan
    via RSS. La ingesta consulta este módulo y, si scrapear() devuelve algo,
    procesa esas entradas como si vinieran de un feed.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
TIMEOUT = 20

# ─── parseo de fechas en español ────────────────────────────────────
MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def parsear_fecha_es(texto):
    """'7 de junio de 2026 | 12:49' → datetime UTC. Devuelve None si falla."""
    if not texto:
        return None
    t = texto.lower().strip()
    # captura: 7 de junio de 2026 [| 12:49]
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})"
                  r"(?:\s*[|@\-,]\s*(\d{1,2}):(\d{2}))?", t)
    if not m:
        return None
    dia, mes_es, año, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    mes = MESES_ES.get(mes_es)
    if not mes:
        return None
    try:
        return datetime(int(año), mes, int(dia),
                        int(hh) if hh else 12,
                        int(mm) if mm else 0,
                        tzinfo=timezone.utc)
    except ValueError:
        return None


# ─── scraper cadena Mercurio / El Día / Lenders ────────────────────
# Diarios provinciales que comparten CMS (artículos en /noticia/{cat}/{año}/{mes}/{slug})
# Nota: diarioeldia.cl y diariolaprensa.cl tienen CMS distinto, no entran acá.
DOMINIOS_MERCURIO = {
    "diariodeosorno.cl", "diariodepuertomontt.cl", "diariodevaldivia.cl",
    "diarioregionalaysen.cl", "elheraldoaustral.cl",
}


def _fecha_desde_url(url):
    """Extrae año/mes de URLs tipo /noticia/{cat}/2026/06/{slug}."""
    m = re.search(r"/noticia/[^/]+/(\d{4})/(\d{1,2})/", url)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), 15,
                        12, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


MAX_ENTRADAS_SCRAPER = 25


def scrapear_mercurio(sitio_url):
    """Scrapea la portada de un sitio de la cadena Mercurio regional.

    No depende de <article>: cada artículo se identifica por su URL
    /noticia/{cat}/{año}/{mes}/{slug}. Si el sitio tiene dos <a> al mismo
    artículo (uno en la imagen, otro en el título), preferimos el que
    tenga texto.
    """
    from ingestar import SESION  # import diferido para evitar ciclo
    try:
        r = SESION.get(sitio_url, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # mejor <a> por URL absoluta del artículo
    mejor = {}
    for a in soup.find_all("a", href=re.compile(r"/noticia/")):
        href = a.get("href", "").strip()
        if not href:
            continue
        full = urljoin(r.url, href)
        texto = a.get_text(" ", strip=True)
        prev = mejor.get(full)
        # preferimos el <a> que trae texto del título
        if prev is None or (texto and not prev.get_text(strip=True)):
            mejor[full] = a

    entradas = []
    for full, a in mejor.items():
        cont = (a.find_parent("article")
                or a.find_parent(["div", "li"], class_=re.compile(
                    r"item|post|news|noticia|featured|destacad", re.I))
                or a.parent or a)

        titulo_el = (cont.select_one(".news-title, .post-title, .item-title")
                     or cont.find(["h1", "h2", "h3"])
                     or a)
        titulo = titulo_el.get_text(" ", strip=True)
        if not titulo or len(titulo) < 8:
            continue

        fecha_el = cont.select_one(".nota-fecha, .post-date, .date, time")
        fecha = parsear_fecha_es(fecha_el.get_text(" ", strip=True)) if fecha_el else None
        if fecha is None:
            fecha = _fecha_desde_url(full)

        img = ""
        img_el = cont.find("img") if hasattr(cont, "find") else None
        if img_el:
            img = img_el.get("data-src") or img_el.get("src") or ""
            if img and not img.startswith("http"):
                img = urljoin(r.url, img)
            if "loadingCont" in img or "blank" in img.lower():
                img = ""

        entradas.append({
            "link": full,
            "title": titulo,
            "published": fecha.isoformat() if fecha else "",
            "summary": "",
            "image": img,
        })
        if len(entradas) >= MAX_ENTRADAS_SCRAPER:
            break
    return entradas


# ─── dispatcher ─────────────────────────────────────────────────────

def _dominio(url):
    p = urlparse(url if "://" in url else f"https://{url}")
    return p.netloc.lower().removeprefix("www.")


def scrapear(sitio_url):
    """Devuelve entradas si hay scraper para el dominio, None si no."""
    dom = _dominio(sitio_url)
    if dom in DOMINIOS_MERCURIO:
        return scrapear_mercurio(sitio_url)
    return None


def tiene_scraper(sitio_url):
    return _dominio(sitio_url) in DOMINIOS_MERCURIO

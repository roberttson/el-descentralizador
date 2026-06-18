"""
Ingestor de noticias regionales
- Lee los medios con RSS activo desde medios_rss_actualizado.csv
- Descarga y parsea cada feed en paralelo
- Limpia el HTML (sin scripts, iframes ni publicidad) y extrae la imagen principal
- Deduplica noticias repetidas entre medios (similitud de títulos en ventana de ±3 días)
- Guarda todo en noticias.db (SQLite + FTS5)

Uso:
    python ingestar.py
"""

import sys
import csv
import re
import time
import difflib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import warnings

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Algunas URLs de artículos devuelven XML; el parser HTML lo maneja igual
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from dateutil import parser as dateparser

from basedatos import conectar
from scrapers import scrapear, tiene_scraper
import colores as c

# La consola de Windows usa cp1252 por defecto y no soporta ✓/✗
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV_MEDIOS = "medios_rss_actualizado.csv"
# UA de navegador: algunos sitios (p. ej. Diario Los Lagos) redirigen en bucle
# o bloquean a los user-agents que no parecen un navegador real
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 30   # algunos sitios regionales (Diario Los Lagos) tardan >15 s


def crear_sesion():
    """Session de requests con reintentos para errores transitorios.

    Reintenta hasta 2 veces (3 intentos en total) con espera 1s, 2s entre
    cada uno. Cubre timeouts, errores de conexión y los códigos 5xx más
    comunes. Los 4xx (404 etc.) NO se reintentan: la URL está mala o el
    feed se cayó del todo.
    """
    retry = Retry(
        total=2, connect=2, read=2,
        backoff_factor=1.0,
        status_forcelist=[408, 429, 500, 502, 503, 504, 520, 522, 524],
        allowed_methods=["HEAD", "GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=20)
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


SESION = crear_sesion()
MAX_RESUMEN = 700          # caracteres del resumen limpio
UMBRAL_RESUMEN_VACIO = 80  # bajo esto se considera "feed pelado" y se rescata
VENTANA_DUP_DIAS = 3       # ventana temporal para buscar duplicados
UMBRAL_JACCARD = 0.65      # similitud de conjuntos de palabras
UMBRAL_RATIO = 0.85        # similitud de secuencia (difflib)

# Clases/ids típicos de publicidad y módulos ajenos al contenido
PATRON_BASURA = re.compile(
    r"(^|[-_ ])(ad|ads|advert|publicidad|banner|sponsor|promo|sharedaddy|"
    r"jp-relatedposts|related|widget|social|share|comment)([-_ ]|$)", re.I)


# ─────────────────────────────────────────────
# CARGA DE MEDIOS
# ─────────────────────────────────────────────

def cargar_medios(csv_path=CSV_MEDIOS):
    """Devuelve medios ingestables: con RSS funcional o con scraper HTML."""
    medios = []
    # utf-8-sig: tolera el BOM que dejan algunos editores/PowerShell
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["tiene_rss"] == "True" and row["feed_url"]:
                medios.append(row)
            elif tiene_scraper(row.get("url", "")):
                # Sin RSS pero con scraper HTML registrado
                medios.append(row)
    return medios


# ─────────────────────────────────────────────
# LIMPIEZA DE CONTENIDO
# ─────────────────────────────────────────────

def limpiar_html(html):
    """Devuelve (texto_limpio, primera_imagen) a partir de HTML de un feed."""
    if not html:
        return "", None
    soup = BeautifulSoup(html, "html.parser")

    # Eliminar elementos de publicidad, scripts y módulos ajenos a la noticia
    for tag in soup(["script", "style", "iframe", "form", "ins", "noscript",
                     "object", "embed", "button", "aside", "footer"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": PATRON_BASURA}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": PATRON_BASURA}):
        tag.decompose()

    # Primera imagen real (ignorar pixeles de tracking y emojis)
    imagen = None
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src.startswith("http"):
            continue
        ancho = img.get("width")
        if ancho and str(ancho).isdigit() and int(ancho) < 80:
            continue
        if any(p in src.lower() for p in ["emoji", "gravatar", "avatar", "pixel",
                                          "1x1", "blank.", "spacer"]):
            continue
        imagen = src
        break

    texto = soup.get_text(" ", strip=True)
    texto = re.sub(r"\s+", " ", texto).strip()

    # Quitar frases típicas de pie de feed
    texto = re.sub(r"(La entrada|The post|El artículo)\s.{0,120}?"
                   r"(se publicó primero|aparece primero|appeared first)\s.+$",
                   "", texto, flags=re.I).strip()

    if len(texto) > MAX_RESUMEN:
        corte = texto.rfind(" ", 0, MAX_RESUMEN)
        texto = texto[:corte if corte > 0 else MAX_RESUMEN] + "…"
    return texto, imagen


def rescatar_resumen(url, imagen_portada=None):
    """Visita la página y extrae texto plano para feeds que solo entregan títulos."""
    try:
        from lector import extraer_articulo  # import diferido (evita ciclo)
        html = extraer_articulo(url, imagen_portada=imagen_portada)
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        texto = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        if len(texto) > MAX_RESUMEN:
            corte = texto.rfind(" ", 0, MAX_RESUMEN)
            texto = texto[:corte if corte > 0 else MAX_RESUMEN] + "…"
        return texto
    except Exception:
        return ""


def extraer_imagen(entry, imagen_contenido):
    """Busca la imagen en metadatos del feed; si no, usa la del contenido."""
    for media in entry.get("media_content", []) or []:
        url = media.get("url", "")
        if url.startswith("http") and media.get("medium", "image") in ("image", ""):
            return url
    for media in entry.get("media_thumbnail", []) or []:
        if media.get("url", "").startswith("http"):
            return media["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href", "").startswith("http"):
            return enc["href"]
    return imagen_contenido


def extraer_fecha(entry):
    for campo in ("published_parsed", "updated_parsed"):
        t = entry.get(campo)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    for campo in ("published", "updated"):
        if entry.get(campo):
            try:
                f = dateparser.parse(entry[campo])
                if f.tzinfo is None:
                    f = f.replace(tzinfo=timezone.utc)
                return f.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                pass
    return None


# ─────────────────────────────────────────────
# DESCARGA DE FEEDS
# ─────────────────────────────────────────────

def procesar_scraper(medio):
    """Procesa una fuente sin RSS via scraper HTML registrado en scrapers.py."""
    nombre, region, sitio = medio["nombre"], medio["region"], medio.get("url", "")
    try:
        entradas = scrapear(sitio) or []
    except Exception as e:
        return nombre, region, [], f"error scraper: {type(e).__name__}"

    ahora = datetime.now(timezone.utc).isoformat()
    articulos = []
    for e in entradas:
        url = (e.get("link") or "").strip()
        titulo = re.sub(r"\s+", " ", e.get("title") or "").strip()
        if not url or not titulo:
            continue
        resumen, imagen_contenido = limpiar_html(e.get("summary") or "")
        imagen = e.get("image") or imagen_contenido
        fecha = (e.get("published") or "").strip() or ahora
        articulos.append({
            "medio": nombre, "region": region, "titulo": titulo, "url": url,
            "fecha": fecha, "resumen": resumen, "imagen": imagen,
        })
    return nombre, region, articulos, "ok"


def procesar_feed(medio):
    """Descarga un feed RSS o ejecuta el scraper si la fuente no tiene feed."""
    nombre, region = medio["nombre"], medio["region"]
    feed_url = medio.get("feed_url", "")
    # Sin RSS pero con scraper registrado: usar HTML
    if not feed_url:
        return procesar_scraper(medio)
    try:
        r = SESION.get(feed_url, timeout=TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        return nombre, region, [], f"error: {type(e).__name__}"

    articulos = []
    for entry in feed.entries:
        url = entry.get("link", "").strip()
        titulo = re.sub(r"\s+", " ", entry.get("title", "")).strip()
        if not url or not titulo:
            continue

        # Preferir contenido completo; si no hay, el resumen
        html = ""
        if entry.get("content"):
            html = entry.content[0].get("value", "")
        if not html:
            html = entry.get("summary", "")

        resumen, imagen_contenido = limpiar_html(html)
        imagen = extraer_imagen(entry, imagen_contenido)
        if len(resumen) < UMBRAL_RESUMEN_VACIO:
            rescate = rescatar_resumen(url, imagen)
            if rescate:
                resumen = rescate
        fecha = extraer_fecha(entry)

        articulos.append({
            "medio": nombre,
            "region": region,
            "titulo": titulo,
            "url": url,
            "fecha": fecha.isoformat() if fecha else None,
            "resumen": resumen,
            "imagen": imagen,
        })
    return nombre, region, articulos, "ok"


# ─────────────────────────────────────────────
# RESCATE DE IMÁGENES (og:image)
# ─────────────────────────────────────────────

def buscar_og_image(url):
    """Visita la página del artículo y extrae la imagen principal (og:image)."""
    try:
        r = SESION.get(url, timeout=10, stream=True)
        # Las metaetiquetas están en el <head>: basta el inicio del HTML
        html = next(r.iter_content(chunk_size=120_000, decode_unicode=False), b"")
        r.close()
        soup = BeautifulSoup(html, "html.parser")
        for selector in ({"property": "og:image"}, {"name": "twitter:image"}):
            meta = soup.find("meta", attrs=selector)
            if meta and meta.get("content", "").startswith("http"):
                return meta["content"]
    except Exception:
        pass
    return None


def rescatar_imagenes(con, workers=12, progreso=None):
    pendientes = con.execute(
        "SELECT id, url FROM articulos WHERE imagen IS NULL").fetchall()
    if not pendientes:
        return 0
    c.fase(f"Rescatando imágenes (og:image) de {len(pendientes)} notas sin foto")
    if progreso is not None:
        progreso.update(fase="imagenes", hechos=0,
                        total=len(pendientes), inicio=time.time())
    rescatadas = 0
    ancho_n = len(str(len(pendientes)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(buscar_og_image, p["url"]): p["id"]
                   for p in pendientes}
        for i, future in enumerate(as_completed(futures), 1):
            img = future.result()
            if img:
                con.execute("UPDATE articulos SET imagen = ? WHERE id = ?",
                            (img, futures[future]))
                rescatadas += 1
            if progreso is not None:
                progreso["hechos"] = i
            # Barra inline: se reescribe en la misma línea
            barra = _barra_progreso(i, len(pendientes))
            print(f"\r  {c.tenue(f'[{i:>{ancho_n}}/{len(pendientes)}]')} "
                  f"{barra}  {c.claro(str(rescatadas))} "
                  f"{c.tenue('rescatadas')}", end="", flush=True)
    print()  # salto de línea tras la barra
    con.commit()
    return rescatadas


def _barra_progreso(hechos, total, ancho=24):
    """Barra estilo [████████░░░░░░░░] con porcentaje."""
    if total <= 0:
        return ""
    pct = hechos / total
    llenos = int(ancho * pct)
    barra = "█" * llenos + "░" * (ancho - llenos)
    return f"{c.ROJO}{barra}{c.RESET} {c.tenue(f'{int(pct*100):>3}%')}"


# ─────────────────────────────────────────────
# DEDUPLICACIÓN
# ─────────────────────────────────────────────

STOPWORDS = set("""a al ante como con contra de del desde donde el en entre es
fue ha hay la las lo los mas más para per pero por que se será sin sobre son
su sus tras un una uno y ya o u e este esta estos estas ese esa""".split())


def normalizar_titulo(titulo):
    t = unicodedata.normalize("NFD", titulo.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    palabras = [p for p in t.split() if p not in STOPWORDS]
    return " ".join(palabras)


def son_similares(a, b):
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    jaccard = len(ta & tb) / len(ta | tb)
    if jaccard >= UMBRAL_JACCARD:
        return True
    if jaccard < 0.3:   # demasiado distintos: no vale la pena difflib
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= UMBRAL_RATIO


def deduplicar(con):
    """Asigna grupo_id a los artículos nuevos comparando títulos normalizados."""
    nuevos = con.execute(
        "SELECT id, medio, titulo_norm, fecha FROM articulos "
        "WHERE grupo_id IS NULL ORDER BY fecha").fetchall()
    duplicados = 0

    for art in nuevos:
        grupo = art["id"]
        if art["fecha"]:
            f = datetime.fromisoformat(art["fecha"])
            desde = (f - timedelta(days=VENTANA_DUP_DIAS)).isoformat()
            hasta = (f + timedelta(days=VENTANA_DUP_DIAS)).isoformat()
            candidatos = con.execute(
                "SELECT id, grupo_id, titulo_norm FROM articulos "
                "WHERE grupo_id IS NOT NULL AND medio != ? "
                "AND fecha BETWEEN ? AND ?",
                (art["medio"], desde, hasta)).fetchall()
            for c in candidatos:
                if son_similares(art["titulo_norm"], c["titulo_norm"]):
                    grupo = c["grupo_id"]
                    duplicados += 1
                    break
        con.execute("UPDATE articulos SET grupo_id = ? WHERE id = ?",
                    (grupo, art["id"]))

    con.commit()
    return duplicados


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def ingestar(workers=12, progreso=None):
    medios = cargar_medios()
    c.cabecera("EL DESCENTRALIZADOR · Ingesta",
               f"{len(medios)} fuentes en cola · {workers} obreros en paralelo")
    if progreso is not None:
        progreso.update(fase="feeds", hechos=0,
                        total=len(medios), inicio=time.time())

    con = conectar()
    total_nuevos = 0
    fallidos = []
    ancho_n = len(str(len(medios)))

    c.fase("Descargando feeds")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(procesar_feed, m): m for m in medios}
        for i, future in enumerate(as_completed(futures), 1):
            nombre, region, articulos, estado = future.result()
            nuevos = 0
            for a in articulos:
                cur = con.execute(
                    "INSERT OR IGNORE INTO articulos "
                    "(medio, region, titulo, url, fecha, resumen, imagen, titulo_norm) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (a["medio"], a["region"], a["titulo"], a["url"],
                     a["fecha"], a["resumen"], a["imagen"],
                     normalizar_titulo(a["titulo"])))
                nuevos += cur.rowcount
            con.commit()
            total_nuevos += nuevos

            if estado == "ok":
                marca = c.ok("✓")
                if nuevos:
                    detalle = f"{c.claro(str(nuevos))} {c.tenue(f'nuevas de {len(articulos)}')}"
                else:
                    detalle = c.tenue(f"sin novedades ({len(articulos)} en el feed)")
            else:
                marca = c.err("✗")
                detalle = c.err(estado)
                fallidos.append(nombre)

            contador = c.tenue(f"[{i:>{ancho_n}}/{len(medios)}]")
            print(f"  {contador} {marca} {nombre} {c.tenue('·')} {c.tenue(region)}  {detalle}")
            if progreso is not None:
                progreso["hechos"] = i

    rescatar_imagenes(con, workers=workers, progreso=progreso)

    c.fase("Deduplicando noticias entre medios")
    if progreso is not None:
        progreso.update(fase="dedup", hechos=0, total=0, inicio=time.time())
    duplicados = deduplicar(con)
    print(f"  {c.claro(str(duplicados))} {c.tenue('duplicados agrupados')}")

    total = con.execute("SELECT COUNT(*) FROM articulos").fetchone()[0]
    grupos = con.execute("SELECT COUNT(DISTINCT grupo_id) FROM articulos").fetchone()[0]
    con.close()

    print()
    print(c.filete("═"))
    print(f"  {c.AMARILLO}{c.BOLD}RESUMEN DE LA EDICIÓN{c.RESET}")
    print(c.filete("─", c.TENUE))
    def linea(etiq, val, color=c.AMARILLO):
        print(f"  {c.tenue(etiq.ljust(28))} {color}{c.BOLD}{val:>6}{c.RESET}")
    linea("Noticias nuevas",        total_nuevos, c.AMARILLO)
    linea("Duplicados agrupados",   duplicados,   c.CYAN)
    linea("Total en base de datos", total,        c.BLANCO)
    linea("Grupos únicos",          grupos,       c.BLANCO)
    linea("Feeds con error",        len(fallidos),
          c.ROJO if fallidos else c.VERDE)
    print(c.filete("═"))

    if fallidos:
        print()
        print(c.err(f"  Feeds con error ({len(fallidos)}):"))
        for nombre in fallidos:
            print(f"    {c.err('·')} {c.tenue(nombre)}")

    print()
    print(f"  {c.tenue('Para abrir la interfaz:')} "
          f"{c.info('python app.py')} {c.tenue('→')} {c.info('http://localhost:5000')}")
    print()

    return {
        "nuevas": total_nuevos,
        "duplicados": duplicados,
        "total": total,
        "grupos": grupos,
        "fallidos": fallidos,
    }


if __name__ == "__main__":
    ingestar()

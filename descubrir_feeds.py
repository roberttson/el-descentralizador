r"""
Descubre y valida feeds RSS de medios candidatos.

Para cada sitio: busca el feed declarado en el HTML (<link rel="alternate">)
o prueba rutas comunes (/feed/, /rss, etc.), y mide la fecha de la última
publicación. Solo se consideran ACTIVOS los medios con noticias recientes.

El script es IDEMPOTENTE:
    - Lee `medios_rss_actualizado.csv` y omite candidatos cuyo dominio ya está
      catastrado.
    - Al final, hace APPEND al mismo CSV de los nuevos descubiertos
      (con o sin feed), respetando la convención `tiene_rss=True/False`.
    - Nunca borra ni reescribe filas existentes.

REGIÓN METROPOLITANA QUEDA FUERA por decisión del proyecto.

Uso:
    venv\Scripts\python.exe descubrir_feeds.py
"""

import csv
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV_PRINCIPAL = Path(__file__).parent / "medios_rss_actualizado.csv"
REGIONES_VALIDAS = {
    "Arica y Parinacota", "Tarapacá", "Antofagasta", "Atacama", "Coquimbo",
    "Valparaíso", "O'Higgins", "Maule", "Ñuble", "Biobío", "Araucanía",
    "Los Ríos", "Los Lagos", "Aysén", "Magallanes",
}

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 12
DIAS_ACTIVO = 14  # última noticia dentro de esta ventana → medio activo

RUTAS_COMUNES = ["/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml",
                 "/?feed=rss2", "/atom.xml", "/feeds/posts/default?alt=rss"]

# ─────────────────────────────────────────────
# LISTA AMPLIADA DE CANDIDATOS (sin RM)
# ─────────────────────────────────────────────
# Incluye medios independientes, diarios provinciales (cadenas El Mercurio,
# El Austral, La Estrella) y portales locales. Si una URL falla, el script la
# marca como sin-sitio y se descarta. No verificamos manualmente: que sondee.

CANDIDATOS = [
    # Aysén (hoy: 0 noticias)
    ("Diario Regional Aysén", "Aysén", "https://www.diarioregionalaysen.cl"),
    ("Canal Sur Patagonia", "Aysén", "https://www.canalsurpatagonia.cl"),
    ("Coyhaique Diario", "Aysén", "https://coyhaiquediario.cl"),
    ("Diario de Aysén", "Aysén", "https://diariodeaysen.cl"),
    ("El Divisadero", "Aysén", "https://www.eldivisadero.cl"),
    ("Aysén al Día", "Aysén", "https://www.aysenaldia.cl"),
    ("Diario Aysén", "Aysén", "https://www.diarioaysen.cl"),
    # Biobío (hoy: muerto, última noticia 2023)
    ("Sabes.cl", "Biobío", "https://sabes.cl"),
    ("Diario Concepción", "Biobío", "https://www.diarioconcepcion.cl"),
    ("La Tribuna", "Biobío", "https://www.latribuna.cl"),
    ("Resumen", "Biobío", "https://resumen.cl"),
    ("TVU", "Biobío", "https://www.tvu.cl"),
    # Ñuble
    ("La Discusión", "Ñuble", "https://ladiscusion.cl"),
    ("El Resumen Ñuble", "Ñuble", "http://elresumen.cl"),
    ("La Fontana", "Ñuble", "https://lafontana.cl"),
    ("Crónica Chillán", "Ñuble", "https://www.cronicachillan.cl"),
    ("Ñuble Digital", "Ñuble", "https://nubledigital.cl"),
    ("Chillán Online", "Ñuble", "https://www.chillanonline.cl"),
    # Magallanes
    ("El Magallanews", "Magallanes", "https://www.elmagallanews.cl"),
    ("Radio Polar", "Magallanes", "https://www.radiopolar.com"),
    ("El Pingüino", "Magallanes", "https://elpinguino.com"),
    ("El Magallánico", "Magallanes", "https://elmagallanico.com"),
    ("ITV Patagonia", "Magallanes", "https://www.itvpatagonia.com"),
    ("La Prensa Austral", "Magallanes", "https://laprensaaustral.cl"),
    # Los Ríos
    ("Diario de Valdivia", "Los Ríos", "https://www.diariodevaldivia.cl"),
    ("Río en Línea", "Los Ríos", "https://www.rioenlinea.cl"),
    ("El Naveghable", "Los Ríos", "https://www.elnaveghable.cl"),
    # Valparaíso
    ("Diario La Quinta", "Valparaíso", "https://diariolaquinta.cl"),
    ("Diario Aconcagua", "Valparaíso", "https://www.diarioaconcagua.cl"),
    ("El Aconcagua", "Valparaíso", "https://www.elaconcagua.cl"),
    ("El Observador", "Valparaíso", "https://www.observador.cl"),
    ("Epicentro Chile", "Valparaíso", "https://www.epicentrochile.com"),
    ("Puranoticia", "Valparaíso", "https://www.puranoticia.cl"),
    ("El Martutino", "Valparaíso", "https://www.elmartutino.cl"),
    ("El Trabajo", "Valparaíso", "https://www.eltrabajo.cl"),
    ("G5 Noticias", "Valparaíso", "https://g5noticias.cl"),
    # Coquimbo
    ("Diario El Día", "Coquimbo", "https://www.diarioeldia.cl"),
    ("La Serena Online", "Coquimbo", "https://laserenaonline.cl"),
    ("El Observatodo", "Coquimbo", "https://www.elobservatodo.cl"),
    ("El Norte", "Coquimbo", "https://www.elnorte.cl"),
    ("Serena y Coquimbo", "Coquimbo", "https://serenaycoquimbo.cl"),
    ("El Ovallino", "Coquimbo", "https://www.elovallino.cl"),
    # Atacama
    ("Atacama Noticias", "Atacama", "https://www.atacamanoticias.cl"),
    ("Chañarcillo", "Atacama", "https://www.chanarcillo.cl"),
    ("Noticiero del Huasco", "Atacama", "https://www.noticierodelhuasco.cl"),
    ("El Tierramarillano", "Atacama", "https://www.tierramarillano.cl"),
    # Los Lagos
    ("Portal Informativo", "Los Lagos", "https://portalinformativo.cl"),
    ("Diario de Puerto Montt", "Los Lagos", "https://www.diariodepuertomontt.cl"),
    ("Diario de Osorno", "Los Lagos", "https://www.diariodeosorno.cl"),
    ("Vértice TV", "Los Lagos", "https://verticetv.cl"),
    ("Paislobo", "Los Lagos", "https://www.paislobo.cl"),
    ("Diario Los Lagos", "Los Lagos", "https://diarioloslagos.cl"),
    ("El Repuertero", "Los Lagos", "https://www.elrepuertero.cl"),
    ("El Vacanudo", "Los Lagos", "https://www.elvacanudo.cl"),
    ("El Insular", "Los Lagos", "https://www.elinsular.cl"),
    ("Radio Sago", "Los Lagos", "https://www.radiosago.cl"),
    ("El Calbucano", "Los Lagos", "https://www.elcalbucano.cl"),
    ("El Heraldo Austral", "Los Lagos", "https://www.elheraldoaustral.cl"),
    ("Diario Puerto Varas", "Los Lagos", "https://diariopuertovaras.cl"),
    # Arica y Parinacota
    ("El Morrocotudo", "Arica y Parinacota", "https://www.elmorrocotudo.cl"),
    ("Arica al Día", "Arica y Parinacota", "https://www.aricaldia.cl"),
    ("Arica Mía", "Arica y Parinacota", "https://aricamia.cl"),
    # Maule
    ("Diario La Prensa", "Maule", "https://www.diariolaprensa.cl"),
    ("El Heraldo de Linares", "Maule", "https://www.diarioelheraldo.cl"),
    ("El Amaule", "Maule", "https://www.elamaule.cl"),
    ("Diario Talca", "Maule", "https://diariotalca.cl"),

    # ─── EXPANSIÓN (2026-06-16) ───────────────────────────────────
    # Cadena El Mercurio regional
    ("El Mercurio de Antofagasta",   "Antofagasta",         "https://www.mercurioantofagasta.cl"),
    ("El Mercurio de Calama",        "Antofagasta",         "https://www.mercuriocalama.cl"),
    ("El Mercurio de Valparaíso",    "Valparaíso",          "https://www.mercuriovalpo.cl"),
    # Cadena La Estrella regional
    ("La Estrella de Arica",         "Arica y Parinacota",  "https://www.estrellaarica.cl"),
    ("La Estrella de Iquique",       "Tarapacá",            "https://www.estrellaiquique.cl"),
    ("La Estrella del Loa",          "Antofagasta",         "https://www.estrelladelloa.cl"),
    ("La Estrella de Tocopilla",     "Antofagasta",         "https://www.estrelladetocopilla.cl"),
    ("La Estrella de Antofagasta",   "Antofagasta",         "https://www.estrellaantofagasta.cl"),
    ("La Estrella de Valparaíso",    "Valparaíso",          "https://www.estrellavalpo.cl"),
    ("La Estrella de Quillota",      "Valparaíso",          "https://www.estrelladequillota.cl"),
    ("La Estrella de Chiloé",        "Los Lagos",           "https://www.estrellachiloe.cl"),
    # Cadena El Austral
    ("El Austral de Temuco",         "Araucanía",           "https://www.australtemuco.cl"),
    ("El Austral de Valdivia",       "Los Ríos",            "https://www.australvaldivia.cl"),
    ("El Austral de Osorno",         "Los Lagos",           "https://www.australosorno.cl"),
    # Soy Chile (red regional EMOL)
    ("Soy Arica",                    "Arica y Parinacota",  "https://www.soychile.cl/Arica"),
    ("Soy Iquique",                  "Tarapacá",            "https://www.soychile.cl/Iquique"),
    ("Soy Calama",                   "Antofagasta",         "https://www.soychile.cl/Calama"),
    ("Soy Antofagasta",              "Antofagasta",         "https://www.soychile.cl/Antofagasta"),
    ("Soy Copiapó",                  "Atacama",             "https://www.soychile.cl/Copiapo"),
    ("Soy Coquimbo",                 "Coquimbo",            "https://www.soychile.cl/Coquimbo"),
    ("Soy La Serena",                "Coquimbo",            "https://www.soychile.cl/LaSerena"),
    ("Soy Valparaíso",               "Valparaíso",          "https://www.soychile.cl/Valparaiso"),
    ("Soy Quillota",                 "Valparaíso",          "https://www.soychile.cl/Quillota"),
    ("Soy Concepción",               "Biobío",              "https://www.soychile.cl/Concepcion"),
    ("Soy Chillán",                  "Ñuble",               "https://www.soychile.cl/Chillan"),
    ("Soy Talca",                    "Maule",               "https://www.soychile.cl/Talca"),
    ("Soy Temuco",                   "Araucanía",           "https://www.soychile.cl/Temuco"),
    ("Soy Valdivia",                 "Los Ríos",            "https://www.soychile.cl/Valdivia"),
    ("Soy Puerto Montt",             "Los Lagos",           "https://www.soychile.cl/PuertoMontt"),
    ("Soy Osorno",                   "Los Lagos",           "https://www.soychile.cl/Osorno"),
    ("Soy Castro",                   "Los Lagos",           "https://www.soychile.cl/Castro"),
    # Cadena Diario Atacama / Diario Norte / Diarios provinciales
    ("Diario de Atacama",            "Atacama",             "https://www.diarioatacama.cl"),
    ("El Diario de Aysén",           "Aysén",               "https://www.diarioaysen.cl"),
    ("Diario Concepción",            "Biobío",              "https://www.diarioconcepcion.cl"),
    ("Diario El Sur",                "Biobío",              "https://www.elsur.cl"),
    ("Diario Centro Sur",            "Maule",               "https://www.diarioelcentro.cl"),
    ("Diario El Centro de Talca",    "Maule",               "https://www.diarioelcentro.cl"),
    ("Diario VI Región",             "O'Higgins",           "https://www.diariovi.cl"),
    ("El Rancagüino",                "O'Higgins",           "https://www.elrancaguino.cl"),
    ("El Tipógrafo",                 "O'Higgins",           "https://www.eltipografo.cl"),
    ("Diario Pyme",                  "Coquimbo",            "https://www.diariopyme.com"),
    ("La Voz del Norte",             "Antofagasta",         "https://www.lavozdelnorte.cl"),
    ("El Tarapacá",                  "Tarapacá",            "https://www.eltarapaca.cl"),
    ("Iquique al Día",               "Tarapacá",            "https://www.iquiquealdia.cl"),
    ("El Longino",                   "Tarapacá",            "https://www.ellongino.cl"),
    # Magallanes / Aysén ampliados
    ("El Sureño",                    "Magallanes",          "https://www.elsureno.cl"),
    ("Patagonia Press",              "Aysén",               "https://www.patagoniapress.cl"),
    ("El Repuertero (Aysén)",        "Aysén",               "https://www.elrepuertero.cl"),
    # Los Lagos / Los Ríos / Araucanía adicionales
    ("El Llanquihue",                "Los Lagos",           "https://www.ellanquihue.cl"),
    ("Periódico Comunal",            "Los Lagos",           "https://www.periodicocomunal.cl"),
    ("Werkén Rojo",                  "Araucanía",           "https://werkenrojo.cl"),
    ("Mapuexpress",                  "Araucanía",           "https://www.mapuexpress.org"),
    # Maule adicionales
    ("Soyqui Talca",                 "Maule",               "https://www.soyqui.cl"),
    ("Curicó Online",                "Maule",               "https://www.curicoonline.cl"),
    # Ñuble adicionales
    ("Chillán Activo",               "Ñuble",               "https://www.chillanactivo.cl"),
    ("Yo soy Chillán",               "Ñuble",               "https://www.yosoychillan.cl"),
    # Biobío adicionales
    ("Crónica del Biobío",           "Biobío",              "https://www.cronicabiobio.cl"),
    ("Periódico La Voz",             "Biobío",              "https://www.lavoz.cl"),
    ("ADN Bío Bío",                  "Biobío",              "https://www.adnradio.cl/biobio"),
    # O'Higgins adicionales
    ("Diario El Expreso",            "O'Higgins",           "https://www.diarioelexpreso.cl"),
    ("Rancagua Capital",             "O'Higgins",           "https://www.rancaguacapital.cl"),
]


def fecha_entrada(entry):
    for campo in ("published", "updated", "created"):
        if entry.get(campo):
            try:
                f = dateparser.parse(entry[campo])
                if f.tzinfo is None:
                    f = f.replace(tzinfo=timezone.utc)
                return f
            except (ValueError, OverflowError):
                pass
    return None


def evaluar_feed(feed_url):
    """Devuelve (n_entradas, fecha_mas_reciente) o None si no es un feed válido."""
    try:
        r = requests.get(feed_url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            return None
        fechas = [f for f in (fecha_entrada(e) for e in parsed.entries) if f]
        return (len(parsed.entries), max(fechas) if fechas else None)
    except requests.RequestException:
        return None


def feeds_declarados(html, base):
    """Extrae feeds anunciados en <link rel=alternate type=rss/atom>."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for link in soup.find_all("link", rel="alternate"):
        tipo = (link.get("type") or "").lower()
        if "rss" in tipo or "atom" in tipo or "xml" in tipo:
            href = link.get("href")
            if href and "comments" not in href.lower():
                urls.append(urljoin(base, href))
    return urls


def sondear(candidato):
    """Devuelve (nombre, region, sitio, sitio_vivo, feed, n, ultima).

    sitio_vivo = True si el sitio responde 200. feed = None si no se encontró
    feed válido. n y ultima son del feed si existe."""
    nombre, region, sitio = candidato
    sitio_vivo = False
    rutas = []
    try:
        r = requests.get(sitio, headers=UA, timeout=TIMEOUT)
        if r.status_code == 200:
            sitio_vivo = True
            rutas += feeds_declarados(r.text, r.url)
    except requests.RequestException:
        pass
    rutas += [sitio.rstrip("/") + p for p in RUTAS_COMUNES]

    vistos = set()
    for feed_url in rutas:
        if feed_url in vistos:
            continue
        vistos.add(feed_url)
        res = evaluar_feed(feed_url)
        if res:
            n, ultima = res
            return (nombre, region, sitio, True, feed_url, n, ultima)
    return (nombre, region, sitio, sitio_vivo, None, 0, None)


# ─────────────────────────────────────────────
# IDEMPOTENCIA + APPEND AL CSV
# ─────────────────────────────────────────────

def _dominio(url):
    """Normaliza una URL a su dominio raíz (sin www. ni esquema)."""
    if not url:
        return ""
    p = urlparse(url if "://" in url else f"https://{url}")
    return p.netloc.lower().removeprefix("www.")


def cargar_dominios_catastrados():
    if not CSV_PRINCIPAL.exists():
        return set(), []
    dominios = set()
    with CSV_PRINCIPAL.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            dominios.add(_dominio(row.get("url", "")))
    return dominios


def append_csv(filas_nuevas):
    """Hace APPEND al CSV principal preservando filas existentes."""
    if not filas_nuevas:
        return
    existe = CSV_PRINCIPAL.exists()
    with CSV_PRINCIPAL.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["nombre", "region", "url", "sitio_vivo",
                        "tiene_rss", "feed_url"])
        for fila in filas_nuevas:
            w.writerow(fila)


def main():
    dominios_ya = cargar_dominios_catastrados()
    nuevos = [c for c in CANDIDATOS if _dominio(c[2]) not in dominios_ya]
    omitidos = len(CANDIDATOS) - len(nuevos)
    print(f"Candidatos: {len(CANDIDATOS)} | ya catastrados: {omitidos} | "
          f"a sondear: {len(nuevos)}\n")

    if not nuevos:
        print("Nada nuevo que sondear.")
        return

    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_ACTIVO)
    activos, anejos, sin_feed_vivos, muertos = [], [], [], []

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(sondear, c): c for c in nuevos}
        for i, fut in enumerate(as_completed(futures), 1):
            nombre, region, sitio, vivo, feed, n, ultima = fut.result()
            if not feed and not vivo:
                muertos.append((nombre, region, sitio))
                print(f"[{i:>3}/{len(nuevos)}] ✗ {nombre} ({region}): "
                      "sitio caído")
            elif not feed:
                sin_feed_vivos.append((nombre, region, sitio))
                print(f"[{i:>3}/{len(nuevos)}] · {nombre} ({region}): "
                      "sitio vivo, sin feed")
            elif ultima and ultima >= limite:
                activos.append((nombre, region, sitio, feed))
                print(f"[{i:>3}/{len(nuevos)}] ✓ {nombre} ({region}): "
                      f"ACTIVO, última {ultima:%Y-%m-%d}")
            else:
                anejos.append((nombre, region, sitio, feed, ultima))
                f_t = ultima.strftime("%Y-%m-%d") if ultima else "?"
                print(f"[{i:>3}/{len(nuevos)}] ⏳ {nombre} ({region}): "
                      f"AÑEJO, última {f_t}")

    # ─── escritura al CSV ────────────────────────────────────────────
    # Activos y añejos quedan con tiene_rss=True (la ingesta los procesa).
    # Sin-feed-vivos y muertos quedan con tiene_rss=False (el curador los ve
    # como pendientes a arreglar/descartar).
    filas = []
    for nombre, region, sitio, feed in activos:
        filas.append([nombre, region, sitio, "True", "True", feed])
    for nombre, region, sitio, feed, _ in anejos:
        filas.append([nombre, region, sitio, "True", "True", feed])
    for nombre, region, sitio in sin_feed_vivos:
        filas.append([nombre, region, sitio, "True", "False", ""])
    for nombre, region, sitio in muertos:
        filas.append([nombre, region, sitio, "False", "False", ""])
    append_csv(filas)

    print(f"\n{'='*70}")
    print(f"ACTIVOS (últimos {DIAS_ACTIVO}d): {len(activos)}")
    print(f"AÑEJOS (feed sin actividad reciente): {len(anejos)}")
    print(f"VIVOS sin feed (candidatos a scraper o feed manual): "
          f"{len(sin_feed_vivos)}")
    print(f"SITIO CAÍDO: {len(muertos)}")
    print(f"\n→ Agregado a {CSV_PRINCIPAL.name}: {len(filas)} medios nuevos.")


if __name__ == "__main__":
    main()

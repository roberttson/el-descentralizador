r"""
Descubre los sitios web y feeds RSS de las municipalidades de Chile
(todas las regiones EXCEPTO la Metropolitana).

Para cada comuna prueba patrones de dominio típicos (municipalidadde{X}.cl,
muni{X}.cl, im{X}.cl, {X}.cl, ...), valida que el sitio sea realmente
municipal (menciona "municipalidad"/"municipio" en la portada) y busca un
feed RSS con publicaciones recientes.

Salida:
    - municipalidades_rss.csv → mismas columnas que medios_rss_actualizado.csv,
      solo las municipalidades ACTIVAS (feed con publicaciones en ≤30 días)

Uso:
    venv\Scripts\python.exe descubrir_municipalidades.py
"""

import csv
import re
import sys
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 10
DIAS_ACTIVO = 30   # las municipalidades publican menos seguido que los medios
SALIDA = "municipalidades_rss.csv"
CSV_MEDIOS = "medios_rss_actualizado.csv"

RUTAS_COMUNES = ["/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml",
                 "/?feed=rss2", "/atom.xml", "/noticias/feed/",
                 "/category/noticias/feed/"]

# ─────────────────────────────────────────────
# COMUNAS POR REGIÓN (sin Región Metropolitana)
# ─────────────────────────────────────────────

COMUNAS = {
    "Arica y Parinacota": [
        "Arica", "Camarones", "Putre", "General Lagos"],
    "Tarapacá": [
        "Iquique", "Alto Hospicio", "Pozo Almonte", "Camiña", "Colchane",
        "Huara", "Pica"],
    "Antofagasta": [
        "Antofagasta", "Mejillones", "Sierra Gorda", "Taltal", "Calama",
        "Ollagüe", "San Pedro de Atacama", "Tocopilla", "María Elena"],
    "Atacama": [
        "Copiapó", "Caldera", "Tierra Amarilla", "Chañaral",
        "Diego de Almagro", "Vallenar", "Alto del Carmen", "Freirina",
        "Huasco"],
    "Coquimbo": [
        "La Serena", "Coquimbo", "Andacollo", "La Higuera", "Paihuano",
        "Vicuña", "Illapel", "Canela", "Los Vilos", "Salamanca", "Ovalle",
        "Combarbalá", "Monte Patria", "Punitaqui", "Río Hurtado"],
    "Valparaíso": [
        "Valparaíso", "Casablanca", "Concón", "Juan Fernández", "Puchuncaví",
        "Quintero", "Viña del Mar", "Isla de Pascua", "Los Andes",
        "Calle Larga", "Rinconada", "San Esteban", "La Ligua", "Cabildo",
        "Papudo", "Petorca", "Zapallar", "Quillota", "La Calera", "Hijuelas",
        "La Cruz", "Nogales", "San Antonio", "Algarrobo", "Cartagena",
        "El Quisco", "El Tabo", "Santo Domingo", "San Felipe", "Catemu",
        "Llay-Llay", "Panquehue", "Putaendo", "Santa María", "Quilpué",
        "Limache", "Olmué", "Villa Alemana"],
    "O'Higgins": [
        "Rancagua", "Codegua", "Coinco", "Coltauco", "Doñihue", "Graneros",
        "Las Cabras", "Machalí", "Malloa", "Mostazal", "Olivar", "Peumo",
        "Pichidegua", "Quinta de Tilcoco", "Rengo", "Requínoa", "San Vicente",
        "Pichilemu", "La Estrella", "Litueche", "Marchigüe", "Navidad",
        "Paredones", "San Fernando", "Chépica", "Chimbarongo", "Lolol",
        "Nancagua", "Palmilla", "Peralillo", "Placilla", "Pumanque",
        "Santa Cruz"],
    "Maule": [
        "Talca", "Constitución", "Curepto", "Empedrado", "Maule", "Pelarco",
        "Pencahue", "Río Claro", "San Clemente", "San Rafael", "Cauquenes",
        "Chanco", "Pelluhue", "Curicó", "Hualañé", "Licantén", "Molina",
        "Rauco", "Romeral", "Sagrada Familia", "Teno", "Vichuquén",
        "Linares", "Colbún", "Longaví", "Parral", "Retiro", "San Javier",
        "Villa Alegre", "Yerbas Buenas"],
    "Ñuble": [
        "Chillán", "Bulnes", "Chillán Viejo", "El Carmen", "Pemuco", "Pinto",
        "Quillón", "San Ignacio", "Yungay", "Quirihue", "Cobquecura",
        "Coelemu", "Ninhue", "Portezuelo", "Ránquil", "Trehuaco",
        "San Carlos", "Coihueco", "Ñiquén", "San Fabián", "San Nicolás"],
    "Biobío": [
        "Concepción", "Coronel", "Chiguayante", "Florida", "Hualqui", "Lota",
        "Penco", "San Pedro de la Paz", "Santa Juana", "Talcahuano", "Tomé",
        "Hualpén", "Lebu", "Arauco", "Cañete", "Contulmo", "Curanilahue",
        "Los Álamos", "Tirúa", "Los Ángeles", "Antuco", "Cabrero", "Laja",
        "Mulchén", "Nacimiento", "Negrete", "Quilaco", "Quilleco",
        "San Rosendo", "Santa Bárbara", "Tucapel", "Yumbel", "Alto Biobío"],
    "Araucanía": [
        "Temuco", "Carahue", "Cunco", "Curarrehue", "Freire", "Galvarino",
        "Gorbea", "Lautaro", "Loncoche", "Melipeuco", "Nueva Imperial",
        "Padre Las Casas", "Perquenco", "Pitrufquén", "Pucón", "Saavedra",
        "Teodoro Schmidt", "Toltén", "Vilcún", "Villarrica", "Cholchol",
        "Angol", "Collipulli", "Curacautín", "Ercilla", "Lonquimay",
        "Los Sauces", "Lumaco", "Purén", "Renaico", "Traiguén", "Victoria"],
    "Los Ríos": [
        "Valdivia", "Corral", "Lanco", "Los Lagos", "Máfil", "Mariquina",
        "Paillaco", "Panguipulli", "La Unión", "Futrono", "Lago Ranco",
        "Río Bueno"],
    "Los Lagos": [
        "Puerto Montt", "Calbuco", "Cochamó", "Fresia", "Frutillar",
        "Los Muermos", "Llanquihue", "Maullín", "Puerto Varas", "Castro",
        "Ancud", "Chonchi", "Curaco de Vélez", "Dalcahue", "Puqueldón",
        "Queilén", "Quellón", "Quemchi", "Quinchao", "Osorno",
        "Puerto Octay", "Purranque", "Puyehue", "Río Negro",
        "San Juan de la Costa", "San Pablo", "Chaitén", "Futaleufú",
        "Hualaihué", "Palena"],
    "Aysén": [
        "Coyhaique", "Lago Verde", "Aysén", "Cisnes", "Guaitecas",
        "Cochrane", "O'Higgins", "Tortel", "Chile Chico", "Río Ibáñez"],
    "Magallanes": [
        "Punta Arenas", "Laguna Blanca", "Río Verde", "San Gregorio",
        "Cabo de Hornos", "Porvenir", "Primavera", "Timaukel", "Natales",
        "Torres del Paine"],
}

# Slugs alternativos para comunas cuyo dominio no sale del nombre oficial
SLUGS_EXTRA = {
    "Aysén": ["puertoaysen"],
    "Natales": ["puertonatales"],
    "Saavedra": ["puertosaavedra"],
    "O'Higgins": ["villaohiggins"],
    "San Vicente": ["sanvicentedetaguatagua", "sanvicentett"],
    "Marchigüe": ["marchihue"],
    "Llay-Llay": ["llayllay"],
    "Isla de Pascua": ["rapanui"],
    "Cabo de Hornos": ["puertowilliams"],
    "Trehuaco": ["treguaco"],
    "Paihuano": ["paiguano"],
    "Mostazal": ["sanfranciscodemostazal"],
    "Mariquina": ["sanjosedelamariquina"],
}


def slug(nombre):
    """'Viña del Mar' → 'vinadelmar' (sin tildes, ñ→n, sin espacios)."""
    t = unicodedata.normalize("NFD", nombre.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", t)


def normalizar_texto(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def dominios_candidatos(comuna):
    """Dominios a probar, de más a menos confiable."""
    slugs = [slug(comuna)] + SLUGS_EXTRA.get(comuna, [])
    dominios = []
    for s in slugs:
        for patron in (f"municipalidadde{s}", f"municipalidad{s}", f"muni{s}",
                       f"municipio{s}", f"im{s}", s):
            dominios.append((patron + ".cl", patron == s))
    return dominios


def descargar(dominio):
    """Intenta https://www, https:// y http://www; devuelve la Response o None."""
    for url in (f"https://www.{dominio}", f"https://{dominio}",
                f"http://www.{dominio}"):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r
        except requests.RequestException:
            continue
    return None


def es_municipal(html, es_dominio_generico):
    """La portada debe dejar claro que es un sitio municipal."""
    texto = normalizar_texto(html)
    if es_dominio_generico:
        # p. ej. pucon.cl puede ser un portal turístico: exigir frase explícita
        return "ilustre municipalidad" in texto or "municipalidad de" in texto
    return "municipalidad" in texto or "municipio" in texto


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
    """Devuelve (n_entradas, fecha_mas_reciente) o None si no es feed válido."""
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
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for link in soup.find_all("link", rel="alternate"):
        tipo = (link.get("type") or "").lower()
        if "rss" in tipo or "atom" in tipo or "xml" in tipo:
            href = link.get("href")
            if href and "comments" not in href.lower():
                urls.append(urljoin(base, href))
    return urls


def dominios_existentes():
    """Dominios ya catastrados como medios (evita duplicar p. ej. cobquecura.cl)."""
    dominios = set()
    try:
        with open(CSV_MEDIOS, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                net = urlparse(row["url"]).netloc.lower()
                dominios.add(net.removeprefix("www."))
    except FileNotFoundError:
        pass
    return dominios


def sondear_comuna(region, comuna, ya_catastrados):
    """Busca el sitio municipal de la comuna y su feed. Devuelve un dict o None."""
    for dominio, generico in dominios_candidatos(comuna):
        if dominio in ya_catastrados:
            continue
        r = descargar(dominio)
        if r is None:
            continue
        if not es_municipal(r.text, generico):
            continue

        base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
        rutas = feeds_declarados(r.text, r.url)
        rutas += [base + p for p in RUTAS_COMUNES]

        vistos = set()
        for feed_url in rutas:
            if feed_url in vistos:
                continue
            vistos.add(feed_url)
            res = evaluar_feed(feed_url)
            if res:
                n, ultima = res
                return {"region": region, "comuna": comuna, "sitio": base,
                        "feed": feed_url, "n": n, "ultima": ultima}
        # Sitio municipal encontrado pero sin feed: reportarlo igual
        return {"region": region, "comuna": comuna, "sitio": base,
                "feed": None, "n": 0, "ultima": None}
    return None


def main():
    ya = dominios_existentes()
    tareas = [(region, comuna) for region, comunas in COMUNAS.items()
              for comuna in comunas]
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_ACTIVO)

    print(f"Sondeando {len(tareas)} municipalidades (sin Región Metropolitana)...\n")
    activas, anejas, sin_feed, sin_sitio = [], [], [], []

    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = {ex.submit(sondear_comuna, r, c, ya): (r, c) for r, c in tareas}
        for i, fut in enumerate(as_completed(futures), 1):
            region, comuna = futures[fut]
            res = fut.result()
            if res is None:
                sin_sitio.append((region, comuna))
                print(f"[{i:>3}/{len(tareas)}] ✗ {comuna} ({region}): sin sitio")
            elif not res["feed"]:
                sin_feed.append(res)
                print(f"[{i:>3}/{len(tareas)}] · {comuna} ({region}): "
                      f"sitio {res['sitio']} sin feed")
            elif res["ultima"] and res["ultima"] >= limite:
                activas.append(res)
                print(f"[{i:>3}/{len(tareas)}] ✓ {comuna} ({region}): ACTIVA, "
                      f"última {res['ultima']:%Y-%m-%d}")
            else:
                anejas.append(res)
                f = f"{res['ultima']:%Y-%m-%d}" if res["ultima"] else "?"
                print(f"[{i:>3}/{len(tareas)}] ⏳ {comuna} ({region}): AÑEJA, "
                      f"última {f}")

    print(f"\n{'=' * 70}")
    print(f"ACTIVAS (publicación en ≤{DIAS_ACTIVO} días): {len(activas)}")
    print(f"AÑEJAS (feed sin actividad reciente):  {len(anejas)}")
    print(f"CON SITIO PERO SIN FEED:               {len(sin_feed)}")
    print(f"SIN SITIO ENCONTRADO:                  {len(sin_sitio)}")

    # CSV de las activas con las mismas columnas del catastro de medios
    activas.sort(key=lambda x: (x["region"], x["comuna"]))
    with open(SALIDA, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["nombre", "region", "url", "sitio_vivo", "tiene_rss", "feed_url"])
        for a in activas:
            w.writerow([f"Municipalidad de {a['comuna']}", a["region"],
                        a["sitio"], "True", "True", a["feed"]])
    print(f"\n→ Guardado: {SALIDA} ({len(activas)} municipalidades activas)")


if __name__ == "__main__":
    main()

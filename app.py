"""
Interfaz web de noticias regionales de Chile
Filtros: región, tipo de fuente (medios / municipalidades), medio,
fecha (rápidos o rango personalizado) y palabras clave (texto completo)
Las noticias duplicadas entre medios se muestran agrupadas ("también en ...")

Uso:
    python app.py
    → http://localhost:5000
"""

import csv
import os
import re
import sys
import threading

import feedparser
import requests
from flask import Flask, jsonify, render_template, request

from basedatos import conectar
from ingestar import extraer_fecha, extraer_imagen, ingestar, limpiar_html
from lector import extraer_articulo



app = Flask(__name__)

POR_PAGINA = 24

# ── Modo público vs. admin ─────────────────────────────
# MODO_PUBLICO=1 oculta el curador y el botón "Actualizar edición" para
# los visitantes. ADMIN_TOKEN permite seguir entrando al curador con
# ?token=XXX o cabecera X-Admin-Token. REPO_URL aparece como link en
# la cinta superior cuando se está en modo público.
MODO_PUBLICO = os.environ.get("MODO_PUBLICO", "").lower() in ("1", "true", "si", "yes")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
REPO_URL = os.environ.get("REPO_URL", "https://github.com/roberttson/el-descentralizador")


def _es_admin():
    if not MODO_PUBLICO:
        return True
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


def _bloqueo_admin():
    """Devuelve un 403 si el visitante no es admin (en modo público sin token)."""
    if not _es_admin():
        return jsonify({"error": "no autorizado"}), 403
    return None

# Orden geográfico norte → sur
ORDEN_REGIONES = [
    "Arica y Parinacota", "Tarapacá", "Antofagasta", "Atacama", "Coquimbo",
    "Valparaíso", "O'Higgins", "Maule", "Ñuble", "Biobío",
    "Araucanía", "Los Ríos", "Los Lagos", "Aysén", "Magallanes",
]


def consulta_fts(q):
    """Convierte texto libre en consulta FTS5 segura con coincidencia por prefijo."""
    tokens = re.findall(r"\w+", q, re.UNICODE)
    return " ".join(f'"{t}"*' for t in tokens if t)


# El tipo de fuente se deriva del prefijo del nombre
PREFIJO_MUNI = "Municipalidad de %"

TIPOS_CURADOR = {"medios", "municipalidades"}
ESTADOS_CURACION = {"aprobado", "arreglar", "descartado"}

UA_CURADOR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"}


def _tipo_de_nombre(nombre):
    if nombre.startswith("Municipalidad de "):
        return "municipalidades"
    return "medios"


def _cargar_catastro(tipo):
    """Catastro COMPLETO para el curador (con o sin RSS)."""
    base = os.path.dirname(__file__)
    fuentes = []
    ruta = os.path.join(base, "medios_rss_actualizado.csv")
    with open(ruta, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if _tipo_de_nombre(row["nombre"]) != tipo:
                continue
            tiene_rss = row.get("tiene_rss", "") == "True"
            fuentes.append({
                "nombre": row["nombre"],
                "region": row["region"],
                "url": row["url"],
                "feed_url": row.get("feed_url", "") if tiene_rss else "",
                "tiene_rss": tiene_rss,
                "sitio_vivo": row.get("sitio_vivo", "") == "True",
            })
    return fuentes


@app.route("/")
def index():
    return render_template("index.html",
                           modo_publico=MODO_PUBLICO,
                           repo_url=REPO_URL)


# Estado de la ingesta lanzada desde la interfaz (una a la vez)
# progreso lo va mutando ingestar(): {fase, hechos, total, inicio}
_ingesta = {"corriendo": False, "resultado": None, "error": None, "progreso": None}
_ingesta_lock = threading.Lock()


def _correr_ingesta():
    try:
        _ingesta["resultado"] = ingestar(progreso=_ingesta["progreso"])
    except Exception as e:
        _ingesta["error"] = str(e)
    finally:
        _ingesta["corriendo"] = False


@app.route("/api/actualizar", methods=["POST"])
def actualizar():
    if (b := _bloqueo_admin()): return b
    with _ingesta_lock:
        if _ingesta["corriendo"]:
            return jsonify({"corriendo": True}), 409
        _ingesta.update(
            corriendo=True, resultado=None, error=None,
            progreso={"fase": "iniciando", "hechos": 0, "total": 0, "inicio": None},
        )
        threading.Thread(target=_correr_ingesta, daemon=True).start()
    return jsonify({"corriendo": True}), 202


@app.route("/api/actualizar/estado")
def actualizar_estado():
    return jsonify(_ingesta)


@app.route("/api/filtros")
def filtros():
    con = conectar()
    regiones = [r["region"] for r in con.execute(
        "SELECT DISTINCT region FROM articulos").fetchall()]
    regiones.sort(key=lambda r: ORDEN_REGIONES.index(r)
                  if r in ORDEN_REGIONES else 99)
    medios = [dict(m) for m in con.execute(
        "SELECT medio, region, COUNT(*) AS n FROM articulos "
        "GROUP BY medio, region ORDER BY region, medio").fetchall()]
    rango = con.execute(
        "SELECT MIN(fecha) AS desde, MAX(fecha) AS hasta FROM articulos "
        "WHERE fecha IS NOT NULL").fetchone()
    con.close()
    return jsonify({
        "regiones": regiones,
        "medios": medios,
        "rango_fechas": {"desde": rango["desde"], "hasta": rango["hasta"]},
    })


@app.route("/api/articulos")
def articulos():
    region = request.args.get("region", "").strip()
    tipo = request.args.get("tipo", "").strip()
    medio = request.args.get("medio", "").strip()
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde", "").strip()
    hasta = request.args.get("hasta", "").strip()
    pagina = max(1, request.args.get("pagina", 1, type=int))

    condiciones, params = [], []
    if region:
        condiciones.append("a.region = ?")
        params.append(region)
    if tipo == "municipalidades":
        condiciones.append("a.medio LIKE ?")
        params.append(PREFIJO_MUNI)
    elif tipo == "medios":
        condiciones.append("a.medio NOT LIKE ?")
        params.append(PREFIJO_MUNI)
    if medio:
        condiciones.append("a.medio = ?")
        params.append(medio)
    if desde:
        condiciones.append("a.fecha >= ?")
        params.append(desde)
    if hasta:
        condiciones.append("a.fecha <= ?")
        params.append(hasta + "T23:59:59+00:00")
    if q:
        fts = consulta_fts(q)
        if fts:
            condiciones.append(
                "a.id IN (SELECT rowid FROM articulos_fts WHERE articulos_fts MATCH ?)")
            params.append(fts)

    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    # Una noticia por grupo de duplicados: la versión más reciente que cumpla los filtros
    sql = f"""
        WITH filtradas AS (
            SELECT a.*, ROW_NUMBER() OVER (
                PARTITION BY a.grupo_id ORDER BY a.fecha DESC, a.id
            ) AS rn
            FROM articulos a {where}
        )
        SELECT * FROM filtradas WHERE rn = 1
        ORDER BY fecha DESC
        LIMIT ? OFFSET ?
    """
    con = conectar()
    filas = con.execute(sql, params + [POR_PAGINA + 1,
                                       (pagina - 1) * POR_PAGINA]).fetchall()
    hay_mas = len(filas) > POR_PAGINA
    filas = filas[:POR_PAGINA]

    resultado = []
    for f in filas:
        art = {k: f[k] for k in ("id", "medio", "region", "titulo", "url",
                                 "fecha", "resumen", "imagen")}
        # Otros medios que publicaron la misma noticia
        otros = con.execute(
            "SELECT medio, url FROM articulos WHERE grupo_id = ? AND id != ?",
            (f["grupo_id"], f["id"])).fetchall()
        art["tambien_en"] = [dict(o) for o in otros]
        resultado.append(art)
    con.close()

    return jsonify({"articulos": resultado, "pagina": pagina, "hay_mas": hay_mas})


@app.route("/api/articulo/<int:art_id>")
def articulo(art_id):
    """Artículo completo para el lector integrado.

    La primera vez visita la página original, extrae el cuerpo y lo cachea;
    las siguientes salen directo de la base. `contenido` vacío significa
    que no se pudo extraer: el lector muestra el resumen y el link original.
    """
    con = conectar()
    f = con.execute("SELECT * FROM articulos WHERE id = ?", (art_id,)).fetchone()
    if f is None:
        con.close()
        return jsonify({"error": "no existe"}), 404

    contenido = f["contenido"]
    if contenido is None:
        try:
            contenido = extraer_articulo(f["url"], imagen_portada=f["imagen"])
        except Exception:
            contenido = ""
        con.execute("UPDATE articulos SET contenido = ? WHERE id = ?",
                    (contenido, art_id))
        con.commit()

    otros = con.execute(
        "SELECT medio, url FROM articulos WHERE grupo_id = ? AND id != ?",
        (f["grupo_id"], f["id"])).fetchall()
    art = {k: f[k] for k in ("id", "medio", "region", "titulo", "url",
                             "fecha", "resumen", "imagen")}
    art["contenido"] = contenido
    art["tambien_en"] = [dict(o) for o in otros]
    con.close()
    return jsonify(art)


# ─────────────────────────────────────────────
# CURADOR DE FUENTES
# ─────────────────────────────────────────────

@app.route("/curador/<tipo>")
def curador(tipo):
    if (b := _bloqueo_admin()): return b
    if tipo not in TIPOS_CURADOR:
        return "tipo desconocido", 404
    return render_template("curador.html", tipo=tipo)


@app.route("/api/curador/<tipo>/fuentes")
def curador_fuentes(tipo):
    """Lista TODAS las fuentes del tipo solicitado (con o sin RSS) + estado."""
    if (b := _bloqueo_admin()): return b
    if tipo not in TIPOS_CURADOR:
        return jsonify({"error": "tipo desconocido"}), 404

    fuentes = _cargar_catastro(tipo)

    con = conectar()
    estados = {f["nombre"]: dict(f) for f in con.execute(
        "SELECT nombre, estado, comentario, actualizado FROM fuentes_curacion"
    ).fetchall()}
    con.close()

    out = []
    for m in fuentes:
        info = estados.get(m["nombre"])
        item = dict(m)
        item["estado"] = info["estado"] if info else "pendiente"
        item["comentario"] = info["comentario"] if info else ""
        item["actualizado"] = info["actualizado"] if info else None
        out.append(item)
    # ordenar: pendientes con RSS primero, luego pendientes sin RSS, luego el resto
    orden_estado = {"pendiente": 0, "arreglar": 1, "aprobado": 2, "descartado": 3}
    out.sort(key=lambda f: (orden_estado.get(f["estado"], 9),
                            0 if f.get("tiene_rss") else 1,
                            f["nombre"]))
    return jsonify({"fuentes": out})


@app.route("/api/curador/muestra")
def curador_muestra():
    """Descarga el feed y devuelve las últimas 10 entradas para evaluar la fuente."""
    if (b := _bloqueo_admin()): return b
    feed_url = request.args.get("feed_url", "").strip()
    if not feed_url:
        return jsonify({"error": "feed_url requerido"}), 400
    try:
        r = requests.get(feed_url, headers=UA_CURADOR, timeout=15)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    entradas = []
    for entry in feed.entries[:10]:
        html = ""
        if entry.get("content"):
            html = entry.content[0].get("value", "")
        if not html:
            html = entry.get("summary", "")
        resumen, imagen_contenido = limpiar_html(html)
        imagen = extraer_imagen(entry, imagen_contenido)
        fecha = extraer_fecha(entry)
        entradas.append({
            "titulo": (entry.get("title") or "").strip(),
            "url": (entry.get("link") or "").strip(),
            "fecha": fecha.isoformat() if fecha else None,
            "resumen": resumen[:400],
            "imagen": imagen,
        })
    return jsonify({"entradas": entradas,
                    "titulo_feed": (feed.feed.get("title") if feed.feed else "") or ""})


@app.route("/api/curador/clasificar", methods=["POST"])
def curador_clasificar():
    if (b := _bloqueo_admin()): return b
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    estado = (data.get("estado") or "").strip()
    comentario = (data.get("comentario") or "").strip()
    if not nombre or estado not in ESTADOS_CURACION:
        return jsonify({"error": "nombre o estado inválido"}), 400
    con = conectar()
    con.execute(
        "INSERT INTO fuentes_curacion(nombre, estado, comentario, actualizado) "
        "VALUES(?, ?, ?, datetime('now')) "
        "ON CONFLICT(nombre) DO UPDATE SET estado=excluded.estado, "
        "comentario=excluded.comentario, actualizado=excluded.actualizado",
        (nombre, estado, comentario))
    con.commit()
    con.close()
    return jsonify({"ok": True, "nombre": nombre, "estado": estado})


@app.route("/api/curador/reset", methods=["POST"])
def curador_reset():
    """Devuelve una fuente al estado 'pendiente' (borra su registro)."""
    if (b := _bloqueo_admin()): return b
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "nombre requerido"}), 400
    con = conectar()
    con.execute("DELETE FROM fuentes_curacion WHERE nombre = ?", (nombre,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import logging
    import colores as c
    # Silenciar el banner ruidoso de Werkzeug
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    cli = sys.modules.get("flask.cli")
    if cli:
        cli.show_server_banner = lambda *a, **k: None

    c.cabecera("EL DESCENTRALIZADOR", "noticias de todo Chile (menos Santiago)")
    modo = c.err("PÚBLICO") if MODO_PUBLICO else c.ok("desarrollo (curador visible)")
    print(f"  {c.tenue('Modo:')}      {modo}")
    print(f"  {c.tenue('Sirviendo:')} {c.info('http://localhost:5000')}")
    if not MODO_PUBLICO:
        print(f"  {c.tenue('Curador:')}   {c.info('http://localhost:5000/curador/medios')}")
    print(f"  {c.tenue('Detener:')}   Ctrl+C")
    print()
    app.run(debug=False, port=5000)

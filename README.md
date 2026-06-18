# El Descentralizador

*Noticias de todo Chile (menos Santiago)*

Agregador de medios regionales chilenos y municipalidades. Una sola interfaz
con filtros por región, tipo de fuente, medio, fecha y palabras clave.
La idea: que la gente pueda informarse de lo que pasa en su región sin
quedar pegada al centralismo informativo de Santiago. La Región Metropolitana
queda explícitamente fuera del catálogo.

## ¿Qué hace?

- **Junta** ~190 fuentes: medios regionales con RSS, scrapers HTML para los
  que no tienen feed (cadena El Mercurio regional, por ejemplo), y las
  publicaciones de 106 municipalidades de las 15 regiones.
- **Limpia** el HTML de las noticias: fuera publicidad, scripts, módulos
  sociales y "relacionados". Solo texto + foto principal + link al original.
- **Deduplica** las mismas noticias entre medios y las muestra agrupadas
  ("↗ También en: …").
- **Rescata imágenes** vía `og:image` para los feeds que no traen foto.
- **Lector integrado**: al hacer clic en una tarjeta se abre el cuerpo
  completo extraído de la página original (cacheado en la BD).
- **Versión móvil** automática para celulares (responsive).

## Stack

Python 3.10+, Flask, SQLite (con FTS5 para búsqueda), feedparser,
BeautifulSoup. Sin frontend framework — HTML/CSS/JS plano.

## Correr localmente

```bash
# 1. Clonar y entrar
git clone https://github.com/roberttson/el-descentralizador.git
cd el-descentralizador

# 2. Crear entorno e instalar dependencias
python -m venv venv
venv\Scripts\activate           # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt

# 3. Cargar noticias (la primera vez tarda ~2-3 min)
python ingestar.py

# 4. Levantar el servidor
python app.py
# → http://localhost:5000
```

## Variables de entorno

Para uso **personal/desarrollo** no necesitas configurar nada.

Para **despliegue público** (servidor compartido con visitantes):

| Variable | Para qué |
|---|---|
| `MODO_PUBLICO=1` | Oculta el curador y el botón "Actualizar edición" en la portada. En su lugar muestra un link al repositorio. |
| `ADMIN_TOKEN=…` | Token para entrar al curador en modo público. Acceso vía `?token=XXX` o cabecera `X-Admin-Token`. |
| `REPO_URL=…` | URL que aparece en la cinta superior cuando `MODO_PUBLICO` está activo. Por defecto apunta a este repo. |

## Componentes

| Archivo | Función |
|---|---|
| `app.py` | Servidor Flask: interfaz, API y curador. |
| `ingestar.py` | Descarga feeds, limpia HTML, deduplica y guarda en SQLite. |
| `basedatos.py` | Esquema SQLite + FTS5 (`noticias.db`). |
| `lector.py` | Extracción del cuerpo completo de cada artículo. |
| `scrapers.py` | Scrapers HTML para medios sin feed RSS. |
| `descubrir_feeds.py` | Sondea sitios para encontrar feeds RSS activos. |
| `descubrir_municipalidades.py` | Sondea las municipalidades de Chile buscando sus feeds. |
| `medios_rss_actualizado.csv` | Catastro maestro de fuentes (~200 filas). |
| `templates/index.html` | Portada estilo diario impreso en oscuro. |
| `templates/curador.html` | Herramienta privada para revisar fuentes una por una. |

## Ingesta automática

La ingesta es **idempotente** — se puede correr cuantas veces se quiera. Para
mantener noticias frescas en producción, agendar `python ingestar.py` cada
1-2 horas con el scheduler de la plataforma (cron, Programador de Tareas,
panel de PythonAnywhere, etc.).

## API

- `GET /api/articulos?region=&tipo=&medio=&q=&desde=&hasta=&pagina=` —
  noticias paginadas (24 por página), una por grupo de duplicados.
  `q` usa búsqueda FTS5. `tipo` acepta `medios` o `municipalidades`.
- `GET /api/articulo/<id>` — artículo completo para el lector.
- `GET /api/filtros` — regiones, medios y rango de fechas disponibles.
- `POST /api/actualizar` — lanza la ingesta en segundo plano (requiere
  `ADMIN_TOKEN` en modo público).

## Curador

`/curador/medios` y `/curador/municipalidades` permiten revisar fuentes
una por una: muestra la últimas 10 entradas en vivo y se pueden clasificar
con atajos de teclado (1 = aprobar, 2 = arreglar, 3 = descartar). Los
estados se guardan en la tabla `fuentes_curacion`.

En modo público estas rutas requieren `?token=<ADMIN_TOKEN>`.

## Despliegue rápido en PythonAnywhere

1. Subir el repo (vía git clone desde una consola de PythonAnywhere).
2. Crear un virtualenv e instalar `requirements.txt`.
3. Web → Add a new web app → Flask → apuntar a `app.py` (variable
   `app`). Setear `WORKING_DIRECTORY` al directorio del repo.
4. Variables de entorno (panel Web → Environment variables):
   - `MODO_PUBLICO=1`
   - `ADMIN_TOKEN=<algo-secreto>`
5. Scheduled tasks → agregar `python ingestar.py` cada 60 min.
6. Primer arranque: correr `python ingestar.py` una vez en la consola
   para poblar la BD.

## Sobre el copyright

El lector integrado muestra el cuerpo completo solo de las
**municipalidades** (información pública). Para los medios de prensa
muestra los **3 primeros párrafos** + un botón "Leer completo en [Medio]"
que abre el original. Esto respeta el derecho de cita del Art. 71 de
la Ley 17.336 chilena.

## Licencia

Código abierto bajo licencia MIT. Las noticias y contenidos enlazados son
propiedad de sus respectivos medios. Este proyecto no aloja ni redistribuye
los artículos — solo los indexa y enlaza al original.

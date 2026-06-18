"""
Capa de base de datos compartida (SQLite + FTS5)
Usada por ingestar.py y app.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "noticias.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS articulos (
    id INTEGER PRIMARY KEY,
    medio TEXT NOT NULL,
    region TEXT NOT NULL,
    titulo TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    fecha TEXT,                 -- ISO 8601 (UTC)
    resumen TEXT,               -- texto limpio, sin HTML ni publicidad
    imagen TEXT,                -- URL de la imagen principal
    titulo_norm TEXT,           -- título normalizado para deduplicación
    grupo_id INTEGER,           -- id del artículo canónico del grupo de duplicados
    creado TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_articulos_fecha ON articulos(fecha);
CREATE INDEX IF NOT EXISTS idx_articulos_region ON articulos(region);
CREATE INDEX IF NOT EXISTS idx_articulos_grupo ON articulos(grupo_id);

CREATE VIRTUAL TABLE IF NOT EXISTS articulos_fts USING fts5(
    titulo, resumen,
    content='articulos', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS articulos_ai AFTER INSERT ON articulos BEGIN
    INSERT INTO articulos_fts(rowid, titulo, resumen)
    VALUES (new.id, new.titulo, new.resumen);
END;

CREATE TRIGGER IF NOT EXISTS articulos_ad AFTER DELETE ON articulos BEGIN
    INSERT INTO articulos_fts(articulos_fts, rowid, titulo, resumen)
    VALUES ('delete', old.id, old.titulo, old.resumen);
END;
"""


def conectar():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    _migrar(con)
    return con


def _migrar(con):
    """Columnas agregadas después del esquema original."""
    columnas = {fila[1] for fila in con.execute("PRAGMA table_info(articulos)")}
    if "contenido" not in columnas:
        # HTML saneado del cuerpo completo, cacheado por el lector integrado
        con.execute("ALTER TABLE articulos ADD COLUMN contenido TEXT")
        con.commit()

    # Curación manual de fuentes: el usuario marca cada medio/municipalidad
    # como aprobado / arreglar / descartado. La ingesta sigue siendo
    # ciega a este estado por ahora; el front del curador lo lee/escribe.
    con.execute("""
        CREATE TABLE IF NOT EXISTS fuentes_curacion (
            nombre TEXT PRIMARY KEY,
            estado TEXT NOT NULL CHECK(estado IN ('aprobado','arreglar','descartado')),
            comentario TEXT,
            actualizado TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()

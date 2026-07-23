"""Base local SQLite con los movimientos de las tarjetas."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent / "tarjetas.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS movimientos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    banco        TEXT    NOT NULL,
    periodo      TEXT    NOT NULL,   -- YYYY-MM del cierre del resumen
    fecha        TEXT,               -- fecha de origen del consumo
    descripcion  TEXT    NOT NULL,
    comercio     TEXT    NOT NULL,   -- descripcion normalizada
    categoria    TEXT    NOT NULL,
    importe      REAL    NOT NULL,
    moneda       TEXT    NOT NULL,   -- ARS | USD
    tipo         TEXT    NOT NULL,   -- purchase | cost | payment
    cuota_nro    INTEGER,
    cuota_total  INTEGER,
    titular      TEXT,
    comprobante  TEXT,
    archivo      TEXT    NOT NULL,
    hoja         INTEGER,
    orden        INTEGER NOT NULL,   -- posicion dentro del resumen
    categoria_manual INTEGER NOT NULL DEFAULT 0,  -- 1 = corregida a mano
    -- Evita duplicar si se vuelve a cargar el mismo PDF, pero SIN fusionar
    -- movimientos repetidos legitimos: dos pagos iguales el mismo dia, o dos
    -- compras identicas en el mismo comercio, son filas distintas del resumen
    -- y deben seguir siendo dos. Por eso la clave incluye `orden`.
    UNIQUE (banco, periodo, orden, descripcion, importe, moneda)
);

CREATE INDEX IF NOT EXISTS idx_periodo   ON movimientos(periodo);
CREATE INDEX IF NOT EXISTS idx_categoria ON movimientos(categoria);
CREATE INDEX IF NOT EXISTS idx_comercio  ON movimientos(comercio);

-- Correcciones de categoria hechas a mano, guardadas POR COMERCIO.
-- La categoria es una propiedad del comercio, no de un movimiento suelto: si
-- MAITILAC es gastronomia, lo es en todos los resumenes. Guardarlo asi hace que
-- la correccion valga tambien para los movimientos que entren en el futuro, y
-- que no se pierda cuando se reaplican las reglas de categories.json.
CREATE TABLE IF NOT EXISTS categorias_manuales (
    comercio  TEXT PRIMARY KEY,
    categoria TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumenes (
    banco       TEXT NOT NULL,
    periodo     TEXT NOT NULL,
    archivo     TEXT NOT NULL,
    cierre      TEXT,
    vencimiento TEXT,
    control_ok  INTEGER NOT NULL,   -- 1 si reconcilia con los totales del PDF
    control_txt TEXT,
    -- Un resumen por banco y periodo, sin importar como se llame el archivo.
    -- Si el mismo resumen se vuelve a subir con otro nombre, reemplaza la fila
    -- en vez de sumar una segunda y contar dos veces el mismo periodo.
    PRIMARY KEY (banco, periodo)
);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrar(conn)
    return conn


def _migrar(conn) -> None:
    """Agrega columnas nuevas a bases ya creadas, sin perder lo cargado."""
    columnas = {r[1] for r in conn.execute("PRAGMA table_info(movimientos)")}
    if "categoria_manual" not in columnas:
        conn.execute("ALTER TABLE movimientos "
                     "ADD COLUMN categoria_manual INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def overrides(conn) -> dict[str, str]:
    """Correcciones manuales: {comercio: categoria}."""
    return {r["comercio"]: r["categoria"]
            for r in conn.execute("SELECT comercio, categoria FROM categorias_manuales")}


def set_categoria(conn, mov_id: int, categoria: str, solo_este: bool = False) -> dict:
    """Cambia la categoria de un movimiento.

    Por defecto la correccion se guarda para el COMERCIO: se aplica a todos sus
    movimientos y queda registrada para los que vengan en resumenes futuros.
    Con `solo_este` se cambia unicamente esa fila (para los casos en que un
    mismo comercio agrupa compras de rubros distintos).
    """
    fila = conn.execute(
        "SELECT comercio FROM movimientos WHERE id = ?", (mov_id,)
    ).fetchone()
    if fila is None:
        raise LookupError(f"No existe el movimiento {mov_id}")
    comercio = fila["comercio"]

    if solo_este:
        conn.execute(
            "UPDATE movimientos SET categoria = ?, categoria_manual = 1 WHERE id = ?",
            (categoria, mov_id),
        )
        afectados = 1
    else:
        conn.execute(
            """INSERT INTO categorias_manuales (comercio, categoria) VALUES (?, ?)
               ON CONFLICT(comercio) DO UPDATE SET categoria = excluded.categoria""",
            (comercio, categoria),
        )
        cur = conn.execute(
            "UPDATE movimientos SET categoria = ?, categoria_manual = 1 WHERE comercio = ?",
            (categoria, comercio),
        )
        afectados = cur.rowcount
    conn.commit()
    return {"comercio": comercio, "afectados": afectados}


def insert_transactions(conn, transactions, categorizer) -> int:
    """Inserta movimientos ignorando los que ya estaban. Devuelve cuantos entraron."""
    from categorize import normalize_merchant

    # Una correccion manual previa sobre ese comercio gana sobre las reglas:
    # si ya dijiste que MAITILAC es gastronomia, los movimientos nuevos de
    # MAITILAC entran directamente asi.
    manuales = overrides(conn)

    rows = []
    for orden, t in enumerate(transactions):
        merchant = normalize_merchant(t.description)
        manual = merchant in manuales
        categoria = manuales[merchant] if manual else categorizer.categorize(t.description)
        rows.append((
            t.bank, t.period, t.date.isoformat() if t.date else None,
            t.description, merchant, categoria,
            float(t.amount), t.currency, t.kind,
            t.cuota_nro, t.cuota_total, t.cardholder, t.receipt,
            t.source_file, t.page, orden, 1 if manual else 0,
        ))

    before = conn.total_changes
    conn.executemany(
        """INSERT OR IGNORE INTO movimientos
           (banco, periodo, fecha, descripcion, comercio, categoria, importe,
            moneda, tipo, cuota_nro, cuota_total, titular, comprobante,
            archivo, hoja, orden, categoria_manual)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def recategorize(conn, categorizer) -> dict:
    """Reaplica categories.json a lo ya cargado, sin releer los PDFs.

    Las correcciones manuales NO se pisan: se reaplican por encima de las
    reglas. Si no fuera asi, un `--recategorizar` borraria todo el trabajo de
    haber corregido los comercios a mano.
    """
    manuales = overrides(conn)
    updates = []
    respetados = 0
    for rid, desc, comercio in conn.execute(
            "SELECT id, descripcion, comercio FROM movimientos").fetchall():
        if comercio in manuales:
            updates.append((manuales[comercio], 1, rid))
            respetados += 1
        else:
            updates.append((categorizer.categorize(desc), 0, rid))
    conn.executemany(
        "UPDATE movimientos SET categoria=?, categoria_manual=? WHERE id=?",
        updates,
    )
    conn.commit()
    return {"total": len(updates), "manuales": respetados}

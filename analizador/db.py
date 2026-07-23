"""Base local SQLite con los movimientos de las tarjetas.

Todo lo que se guarda cuelga de un usuario (`usuario_id`). Ninguna funcion de
este modulo consulta ni modifica nada sin recibir de que usuario se trata: es lo
que hace que un usuario no pueda ver ni tocar los movimientos de otro, aunque se
invente un id de movimiento en la URL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import auth

DEFAULT_DB = Path(__file__).parent.parent / "tarjetas.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS movimientos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id   INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
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
    -- Incluye `usuario_id` porque dos personas pueden tener el mismo consumo,
    -- y el de una no tiene que tapar el de la otra.
    UNIQUE (usuario_id, banco, periodo, orden, descripcion, importe, moneda)
);

CREATE INDEX IF NOT EXISTS idx_usuario   ON movimientos(usuario_id);
CREATE INDEX IF NOT EXISTS idx_periodo   ON movimientos(usuario_id, periodo);
CREATE INDEX IF NOT EXISTS idx_categoria ON movimientos(usuario_id, categoria);
CREATE INDEX IF NOT EXISTS idx_comercio  ON movimientos(usuario_id, comercio);

-- Correcciones de categoria hechas a mano, guardadas POR COMERCIO.
-- La categoria es una propiedad del comercio, no de un movimiento suelto: si
-- MAITILAC es gastronomia, lo es en todos los resumenes. Guardarlo asi hace que
-- la correccion valga tambien para los movimientos que entren en el futuro, y
-- que no se pierda cuando se reaplican las reglas de categories.json.
-- Es por usuario: cada uno clasifica sus comercios como quiere.
CREATE TABLE IF NOT EXISTS categorias_manuales (
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    comercio   TEXT NOT NULL,
    categoria  TEXT NOT NULL,
    PRIMARY KEY (usuario_id, comercio)
);

CREATE TABLE IF NOT EXISTS resumenes (
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
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
    PRIMARY KEY (usuario_id, banco, periodo)
);
"""

# Columnas de movimientos que se copian tal cual al migrar (todas menos id y
# usuario_id, que la migracion pone a mano).
_COLUMNAS_MOV = (
    "banco, periodo, fecha, descripcion, comercio, categoria, importe, moneda, "
    "tipo, cuota_nro, cuota_total, titular, comprobante, archivo, hoja, orden, "
    "categoria_manual"
)


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Las tablas de usuarios van primero: la migracion necesita a quien
    # asignarle lo que ya estaba cargado.
    conn.executescript(auth.SCHEMA)
    auth.asegurar_usuario_inicial(conn)
    _migrar(conn)
    conn.executescript(SCHEMA)
    return conn


def _tablas(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columnas(conn, tabla: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabla})")}


def _migrar(conn) -> None:
    """Lleva una base vieja (sin usuarios) al esquema actual, sin perder nada.

    Todo lo que estaba cargado pasa a ser del usuario mas antiguo: antes de que
    hubiera usuarios habia una sola persona usando esto, y sus movimientos,
    resumenes y correcciones de categoria son suyos.

    Agregar `usuario_id` a las claves UNIQUE/PRIMARY no se puede con ALTER
    TABLE, asi que hay que reconstruir las tablas y copiar el contenido.
    """
    tablas = _tablas(conn)
    if "movimientos" not in tablas or "usuario_id" in _columnas(conn, "movimientos"):
        return

    uid = auth.asegurar_usuario_inicial(conn)

    # Una base de la primera version puede no tener ni esta columna; se agrega
    # antes de copiar para que el SELECT de abajo la encuentre.
    if "categoria_manual" not in _columnas(conn, "movimientos"):
        conn.execute("ALTER TABLE movimientos "
                     "ADD COLUMN categoria_manual INTEGER NOT NULL DEFAULT 0")

    # Los indices viejos siguen a la tabla renombrada y chocarian de nombre con
    # los que crea SCHEMA.
    conn.executescript("""
        DROP INDEX IF EXISTS idx_periodo;
        DROP INDEX IF EXISTS idx_categoria;
        DROP INDEX IF EXISTS idx_comercio;
        ALTER TABLE movimientos RENAME TO _movimientos_viejo;
    """)
    for tabla in ("resumenes", "categorias_manuales"):
        if tabla in tablas:
            conn.execute(f"ALTER TABLE {tabla} RENAME TO _{tabla}_viejo")

    conn.executescript(SCHEMA)

    conn.execute(
        f"INSERT INTO movimientos (usuario_id, {_COLUMNAS_MOV}) "
        f"SELECT ?, {_COLUMNAS_MOV} FROM _movimientos_viejo", (uid,))
    conn.execute("DROP TABLE _movimientos_viejo")

    if "resumenes" in tablas:
        conn.execute(
            "INSERT INTO resumenes (usuario_id, banco, periodo, archivo, cierre, "
            "                       vencimiento, control_ok, control_txt) "
            "SELECT ?, banco, periodo, archivo, cierre, vencimiento, control_ok, "
            "       control_txt FROM _resumenes_viejo", (uid,))
        conn.execute("DROP TABLE _resumenes_viejo")

    if "categorias_manuales" in tablas:
        conn.execute(
            "INSERT INTO categorias_manuales (usuario_id, comercio, categoria) "
            "SELECT ?, comercio, categoria FROM _categorias_manuales_viejo", (uid,))
        conn.execute("DROP TABLE _categorias_manuales_viejo")

    conn.commit()


# --------------------------------------------------------------------------
def overrides(conn, usuario_id: int) -> dict[str, str]:
    """Correcciones manuales de ese usuario: {comercio: categoria}."""
    return {r["comercio"]: r["categoria"] for r in conn.execute(
        "SELECT comercio, categoria FROM categorias_manuales WHERE usuario_id = ?",
        (usuario_id,))}


def set_categoria(conn, usuario_id: int, mov_id: int, categoria: str,
                  solo_este: bool = False) -> dict:
    """Cambia la categoria de un movimiento del usuario.

    Por defecto la correccion se guarda para el COMERCIO: se aplica a todos sus
    movimientos y queda registrada para los que vengan en resumenes futuros.
    Con `solo_este` se cambia unicamente esa fila (para los casos en que un
    mismo comercio agrupa compras de rubros distintos).

    El movimiento se busca acotado al usuario: pedir el id de un movimiento
    ajeno da "no existe", no el movimiento de otro.
    """
    fila = conn.execute(
        "SELECT comercio FROM movimientos WHERE id = ? AND usuario_id = ?",
        (mov_id, usuario_id),
    ).fetchone()
    if fila is None:
        raise LookupError(f"No existe el movimiento {mov_id}")
    comercio = fila["comercio"]

    if solo_este:
        conn.execute(
            "UPDATE movimientos SET categoria = ?, categoria_manual = 1 "
            "WHERE id = ? AND usuario_id = ?",
            (categoria, mov_id, usuario_id),
        )
        afectados = 1
    else:
        conn.execute(
            """INSERT INTO categorias_manuales (usuario_id, comercio, categoria)
               VALUES (?, ?, ?)
               ON CONFLICT(usuario_id, comercio)
               DO UPDATE SET categoria = excluded.categoria""",
            (usuario_id, comercio, categoria),
        )
        cur = conn.execute(
            "UPDATE movimientos SET categoria = ?, categoria_manual = 1 "
            "WHERE comercio = ? AND usuario_id = ?",
            (categoria, comercio, usuario_id),
        )
        afectados = cur.rowcount
    conn.commit()
    return {"comercio": comercio, "afectados": afectados}


def insert_transactions(conn, usuario_id: int, transactions, categorizer) -> int:
    """Inserta movimientos ignorando los que ya estaban. Devuelve cuantos entraron."""
    from categorize import normalize_merchant

    # Una correccion manual previa sobre ese comercio gana sobre las reglas:
    # si ya dijiste que MAITILAC es gastronomia, los movimientos nuevos de
    # MAITILAC entran directamente asi.
    manuales = overrides(conn, usuario_id)

    rows = []
    for orden, t in enumerate(transactions):
        merchant = normalize_merchant(t.description)
        manual = merchant in manuales
        categoria = manuales[merchant] if manual else categorizer.categorize(t.description)
        rows.append((
            usuario_id,
            t.bank, t.period, t.date.isoformat() if t.date else None,
            t.description, merchant, categoria,
            float(t.amount), t.currency, t.kind,
            t.cuota_nro, t.cuota_total, t.cardholder, t.receipt,
            t.source_file, t.page, orden, 1 if manual else 0,
        ))

    before = conn.total_changes
    conn.executemany(
        f"""INSERT OR IGNORE INTO movimientos (usuario_id, {_COLUMNAS_MOV})
            VALUES ({','.join('?' * 18)})""",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def guardar_resumen(conn, usuario_id: int, statement, control_ok: bool,
                    detalle: str, archivo: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO resumenes
           (usuario_id, banco, periodo, archivo, cierre, vencimiento,
            control_ok, control_txt)
           VALUES (?,?,?,?,?,?,?,?)""",
        (usuario_id, statement.bank, statement.period, archivo,
         statement.close_date.isoformat() if statement.close_date else None,
         statement.due_date.isoformat() if statement.due_date else None,
         1 if control_ok else 0, detalle),
    )
    conn.commit()


def recategorize(conn, usuario_id: int, categorizer) -> dict:
    """Reaplica categories.json a lo ya cargado, sin releer los PDFs.

    Las correcciones manuales NO se pisan: se reaplican por encima de las
    reglas. Si no fuera asi, un `--recategorizar` borraria todo el trabajo de
    haber corregido los comercios a mano.
    """
    manuales = overrides(conn, usuario_id)
    updates = []
    respetados = 0
    for rid, desc, comercio in conn.execute(
            "SELECT id, descripcion, comercio FROM movimientos WHERE usuario_id = ?",
            (usuario_id,)).fetchall():
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

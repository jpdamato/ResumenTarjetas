"""Usuarios, contraseñas y sesiones.

Cada usuario ve unicamente lo suyo: todo lo que se guarda lleva `usuario_id` y
las consultas lo filtran siempre (ver db.py).

Las contraseñas se guardan con PBKDF2-HMAC-SHA256 y sal por usuario, nunca en
claro. Las sesiones viven en la base: en la cookie viaja un token al azar y en
la tabla queda solo su SHA-256, asi tener una copia de la base no alcanza para
hacerse pasar por nadie. Ademas sobreviven a reiniciar el contenedor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario      TEXT NOT NULL,
    -- En minusculas y unico: "JPD" y "jpd" son la misma persona, y que existan
    -- las dos cuentas seria una forma facil de confundirse de sesion.
    usuario_norm TEXT NOT NULL UNIQUE,
    clave        TEXT NOT NULL,          -- pbkdf2_sha256$iteraciones$sal$hash
    creado       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    token_hash TEXT PRIMARY KEY,         -- SHA-256 del token que va en la cookie
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creado     TEXT NOT NULL,
    visto      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones(usuario_id);
"""

ITERACIONES = 240_000
DIAS_SESION = 30
CLAVE_MINIMA = 8
USUARIO_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

# Primer usuario, el que queda dueño de todo lo que ya estaba cargado antes de
# que existieran los usuarios. Se puede cambiar por entorno antes del primer
# arranque; despues ya no tiene efecto (ver asegurar_usuario_inicial).
USUARIO_INICIAL = os.environ.get("USUARIO_INICIAL", "jpd")
CLAVE_INICIAL = os.environ.get("CLAVE_INICIAL", "$proj1978$")

# La registracion abierta se puede cerrar sin tocar codigo: REGISTRO_ABIERTO=0.
REGISTRO_ABIERTO = os.environ.get("REGISTRO_ABIERTO", "1") not in ("0", "false", "no")


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# --------------------------------------------------------------------------
# contraseñas
def hash_clave(clave: str, iteraciones: int = ITERACIONES) -> str:
    sal = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, iteraciones)
    return f"pbkdf2_sha256${iteraciones}${_b64(sal)}${_b64(dk)}"


def verificar_clave(clave: str, guardado: str) -> bool:
    try:
        algo, iteraciones, sal_b64, dk_b64 = (guardado or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (clave or "").encode("utf-8"),
                                 base64.b64decode(sal_b64), int(iteraciones))
    except (ValueError, TypeError):
        return False
    # compare_digest y no ==: comparar hashes con == filtra por el tiempo que
    # tarda cuantos bytes iniciales coinciden.
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


# --------------------------------------------------------------------------
# usuarios
def validar(usuario: str, clave: str) -> None:
    """Levanta ValueError con un mensaje mostrable si algo no sirve."""
    if not USUARIO_RE.match(usuario or ""):
        raise ValueError("El usuario debe tener entre 3 y 32 caracteres: "
                         "letras, numeros, punto, guion o guion bajo.")
    if len(clave or "") < CLAVE_MINIMA:
        raise ValueError(f"La contraseña necesita al menos {CLAVE_MINIMA} caracteres.")


def crear_usuario(conn, usuario: str, clave: str) -> int:
    usuario = (usuario or "").strip()
    validar(usuario, clave)
    try:
        cur = conn.execute(
            "INSERT INTO usuarios (usuario, usuario_norm, clave, creado) VALUES (?,?,?,?)",
            (usuario, usuario.lower(), hash_clave(clave), _ahora()),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"El usuario «{usuario}» ya existe.") from None
    conn.commit()
    return cur.lastrowid


def buscar_usuario(conn, usuario: str):
    return conn.execute(
        "SELECT * FROM usuarios WHERE usuario_norm = ?",
        ((usuario or "").strip().lower(),),
    ).fetchone()


def cambiar_clave(conn, usuario_id: int, clave: str) -> None:
    if len(clave or "") < CLAVE_MINIMA:
        raise ValueError(f"La contraseña necesita al menos {CLAVE_MINIMA} caracteres.")
    conn.execute("UPDATE usuarios SET clave = ? WHERE id = ?",
                 (hash_clave(clave), usuario_id))
    # Cambiar la clave cierra las sesiones abiertas: si la cambiaste porque
    # alguien la sabia, dejarle la sesion viva no arregla nada.
    conn.execute("DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,))
    conn.commit()


def autenticar(conn, usuario: str, clave: str):
    """Devuelve la fila del usuario, o None si el usuario o la clave no van."""
    fila = buscar_usuario(conn, usuario)
    if fila is None or not verificar_clave(clave, fila["clave"]):
        return None
    return fila


def asegurar_usuario_inicial(conn) -> int:
    """Crea el primer usuario la primera vez, y devuelve su id.

    Si ya hay usuarios no toca nada y devuelve el mas antiguo: ese es el dueño
    de lo que se haya cargado antes de que existieran los usuarios.
    """
    fila = conn.execute("SELECT id FROM usuarios ORDER BY id LIMIT 1").fetchone()
    if fila is not None:
        return fila["id"]
    return crear_usuario(conn, USUARIO_INICIAL, CLAVE_INICIAL)


def usuario_legado(conn) -> int:
    """El usuario mas antiguo. Sus PDFs siguen viviendo en /datos/<banco>."""
    return asegurar_usuario_inicial(conn)


def resolver_usuario(conn, nombre: str | None) -> int:
    """id del usuario pedido por nombre; si no se pide ninguno, el inicial."""
    if not nombre:
        return asegurar_usuario_inicial(conn)
    fila = buscar_usuario(conn, nombre)
    if fila is None:
        raise LookupError(f"No existe el usuario «{nombre}». "
                          f"Crealo con: python usuarios.py crear {nombre}")
    return fila["id"]


# --------------------------------------------------------------------------
# sesiones
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def abrir_sesion(conn, usuario_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sesiones (token_hash, usuario_id, creado, visto) VALUES (?,?,?,?)",
        (_hash_token(token), usuario_id, _ahora(), _ahora()),
    )
    _purgar_vencidas(conn)
    conn.commit()
    return token


def usuario_de_sesion(conn, token: str | None):
    """Fila del usuario dueño de esa sesion, o None si no existe o vencio."""
    if not token:
        return None
    th = _hash_token(token)
    fila = conn.execute(
        """SELECT u.id AS id, u.usuario AS usuario, s.creado AS creado
             FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id
            WHERE s.token_hash = ?""",
        (th,),
    ).fetchone()
    if fila is None:
        return None
    if _vencida(fila["creado"]):
        conn.execute("DELETE FROM sesiones WHERE token_hash = ?", (th,))
        conn.commit()
        return None
    conn.execute("UPDATE sesiones SET visto = ? WHERE token_hash = ?", (_ahora(), th))
    conn.commit()
    return fila


def cerrar_sesion(conn, token: str | None) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sesiones WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


def _vencida(creado: str) -> bool:
    try:
        nacimiento = datetime.fromisoformat(creado)
    except ValueError:
        return True
    if nacimiento.tzinfo is None:
        nacimiento = nacimiento.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - nacimiento > timedelta(days=DIAS_SESION)


def _purgar_vencidas(conn) -> None:
    limite = (datetime.now(timezone.utc) - timedelta(days=DIAS_SESION)).isoformat()
    conn.execute("DELETE FROM sesiones WHERE creado < ?", (limite,))

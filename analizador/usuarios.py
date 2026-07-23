"""Administrar los usuarios desde la linea de comandos.

    python usuarios.py listar
    python usuarios.py crear ana
    python usuarios.py clave ana
    python usuarios.py borrar ana

La contraseña se pide por teclado y no se ve al tipearla, para que no quede en
el historial del shell. Se puede pasar con --clave cuando hace falta hacerlo sin
interaccion (por ejemplo desde un script), sabiendo que ahi si queda escrita.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import auth
import db


def _pedir_clave(clave: str | None) -> str:
    if clave:
        return clave
    primera = getpass.getpass("Contraseña: ")
    if primera != getpass.getpass("Repetir contraseña: "):
        raise ValueError("Las contraseñas no coinciden.")
    return primera


def main() -> int:
    ap = argparse.ArgumentParser(description="Usuarios del tablero de tarjetas")
    ap.add_argument("--base", type=Path,
                    default=Path(os.environ.get("BASE", db.DEFAULT_DB)))
    sub = ap.add_subparsers(dest="accion", required=True)

    sub.add_parser("listar", help="muestra los usuarios y cuanto tiene cargado cada uno")

    p = sub.add_parser("crear", help="crea un usuario")
    p.add_argument("usuario")
    p.add_argument("--clave")

    p = sub.add_parser("clave", help="cambia la contraseña (cierra sus sesiones)")
    p.add_argument("usuario")
    p.add_argument("--clave")

    p = sub.add_parser("borrar", help="borra el usuario y TODOS sus movimientos")
    p.add_argument("usuario")
    p.add_argument("--si", action="store_true", help="no preguntar")

    args = ap.parse_args()
    conn = db.connect(args.base)

    if args.accion == "listar":
        filas = conn.execute("""
            SELECT u.id, u.usuario, u.creado,
                   (SELECT COUNT(*) FROM movimientos m WHERE m.usuario_id = u.id) AS movs,
                   (SELECT COUNT(*) FROM resumenes r WHERE r.usuario_id = u.id) AS resus
              FROM usuarios u ORDER BY u.id""").fetchall()
        print(f"{'id':>3}  {'usuario':<20} {'movs':>7} {'resúm.':>7}  creado")
        for f in filas:
            print(f"{f['id']:>3}  {f['usuario']:<20} {f['movs']:>7} {f['resus']:>7}"
                  f"  {f['creado'][:10]}")
        return 0

    if args.accion == "crear":
        try:
            uid = auth.crear_usuario(conn, args.usuario, _pedir_clave(args.clave))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Usuario «{args.usuario}» creado (id {uid}). Ya puede entrar al tablero.")
        return 0

    fila = auth.buscar_usuario(conn, args.usuario)
    if fila is None:
        print(f"No existe el usuario «{args.usuario}».", file=sys.stderr)
        return 1

    if args.accion == "clave":
        try:
            auth.cambiar_clave(conn, fila["id"], _pedir_clave(args.clave))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Contraseña de «{fila['usuario']}» cambiada. Sus sesiones se cerraron.")
        return 0

    if args.accion == "borrar":
        movs = conn.execute("SELECT COUNT(*) FROM movimientos WHERE usuario_id = ?",
                            (fila["id"],)).fetchone()[0]
        if not args.si:
            print(f"Se van a borrar «{fila['usuario']}» y sus {movs} movimientos. "
                  f"Esto no se puede deshacer.")
            if input("Escribí el nombre del usuario para confirmar: ") != fila["usuario"]:
                print("Cancelado.")
                return 1
        # Las tablas tienen ON DELETE CASCADE y db.connect prende foreign_keys,
        # asi que se van tambien movimientos, resumenes, categorias y sesiones.
        conn.execute("DELETE FROM usuarios WHERE id = ?", (fila["id"],))
        conn.commit()
        print(f"Usuario «{fila['usuario']}» borrado. "
              f"Sus PDFs siguen en disco: hay que borrarlos a mano si querés.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

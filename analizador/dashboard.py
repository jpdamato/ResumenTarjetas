"""Genera el tablero HTML a partir de la base SQLite.

Los datos se incrustan dentro del HTML en vez de cargarse con fetch(): asi el
archivo se abre con doble clic (file://), donde fetch esta bloqueado por CORS.

El tablero es de un usuario: se genera con SUS movimientos y nada mas.

Uso:
    python dashboard.py                 # escribe ../tablero.html
    python dashboard.py --usuario jpd
    python dashboard.py --salida x.html
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import auth
import db

TEMPLATE = Path(__file__).with_name("template.html")
DEFAULT_OUT = Path(__file__).parent.parent / "tablero.html"


def build_payload(conn, usuario_id: int) -> dict:
    movimientos = [
        {
            "id": r["id"],
            "p": r["periodo"],
            "f": r["fecha"],
            "d": r["descripcion"],
            "c": r["comercio"],
            "cat": r["categoria"],
            "cm": r["categoria_manual"],
            "imp": round(r["importe"], 2),
            "mon": r["moneda"],
            "t": r["tipo"],
            "cn": r["cuota_nro"],
            "ct": r["cuota_total"],
            "tit": r["titular"],
            "b": r["banco"],
        }
        for r in conn.execute(
            "SELECT * FROM movimientos WHERE usuario_id = ? ORDER BY periodo, orden",
            (usuario_id,),
        )
    ]
    resumenes = [
        {
            "banco": r["banco"],
            "periodo": r["periodo"],
            "control_ok": bool(r["control_ok"]),
        }
        for r in conn.execute(
            "SELECT * FROM resumenes WHERE usuario_id = ? ORDER BY periodo",
            (usuario_id,),
        )
    ]
    # Categorias posibles para el desplegable: las de las reglas mas las que ya
    # existen en los movimientos del usuario (incluidas las corregidas a mano).
    from categorize import Categorizer
    categorias = sorted({
        *Categorizer().categorias(),
        *(r["categoria"] for r in conn.execute(
            "SELECT DISTINCT categoria FROM movimientos WHERE usuario_id = ?",
            (usuario_id,))),
    })

    return {
        "movimientos": movimientos,
        "resumenes": resumenes,
        "categorias": categorias,
        "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera el tablero HTML")
    ap.add_argument("--base", type=Path,
                    default=Path(os.environ.get("BASE", db.DEFAULT_DB)))
    ap.add_argument("--salida", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--usuario", default=os.environ.get("USUARIO"),
                    help="de quien es el tablero (por defecto, el usuario inicial)")
    args = ap.parse_args()

    if not args.base.exists():
        print(f"No existe la base {args.base}. Corre primero: python ingest.py")
        return 1

    conn = db.connect(args.base)
    try:
        usuario_id = auth.resolver_usuario(conn, args.usuario)
    except LookupError as exc:
        print(exc)
        return 1
    nombre = conn.execute("SELECT usuario FROM usuarios WHERE id = ?",
                          (usuario_id,)).fetchone()["usuario"]
    payload = build_payload(conn, usuario_id)

    if not payload["movimientos"]:
        print(f"{nombre} no tiene movimientos cargados. "
              f"Corre primero: python ingest.py --usuario {nombre}")
        return 1

    # "</script>" dentro del JSON cerraria la etiqueta antes de tiempo.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    # __API__ = false: este HTML es un archivo suelto, no tiene servidor detras,
    # asi que la seccion de subir resumenes no se muestra.
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA__", data)
            .replace("__API__", "false")
            .replace("__USUARIO__", json.dumps(nombre)))
    args.salida.write_text(html, encoding="utf-8")

    n_ok = sum(1 for r in payload["resumenes"] if r["control_ok"])
    print(f"Tablero generado: {args.salida}  (usuario {nombre})")
    print(f"  {len(payload['movimientos'])} movimientos, "
          f"{n_ok}/{len(payload['resumenes'])} resumenes cuadran con sus totales")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

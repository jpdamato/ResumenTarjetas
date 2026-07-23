"""Genera el tablero HTML a partir de la base SQLite.

Los datos se incrustan dentro del HTML en vez de cargarse con fetch(): asi el
archivo se abre con doble clic (file://), donde fetch esta bloqueado por CORS.

Uso:
    python dashboard.py                 # escribe ../tablero.html
    python dashboard.py --salida x.html
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import db

TEMPLATE = Path(__file__).with_name("template.html")
DEFAULT_OUT = Path(__file__).parent.parent / "tablero.html"


def build_payload(conn) -> dict:
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
            "SELECT * FROM movimientos ORDER BY periodo, orden"
        )
    ]
    resumenes = [
        {
            "banco": r["banco"],
            "periodo": r["periodo"],
            "control_ok": bool(r["control_ok"]),
        }
        for r in conn.execute("SELECT * FROM resumenes ORDER BY periodo")
    ]
    # Categorias posibles para el desplegable: las de las reglas mas las que ya
    # existen en la base (incluidas las corregidas a mano).
    from categorize import Categorizer
    categorias = sorted({
        *Categorizer().categorias(),
        *(r["categoria"] for r in conn.execute(
            "SELECT DISTINCT categoria FROM movimientos")),
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
    args = ap.parse_args()

    if not args.base.exists():
        print(f"No existe la base {args.base}. Corre primero: python ingest.py")
        return 1

    conn = db.connect(args.base)
    payload = build_payload(conn)

    if not payload["movimientos"]:
        print("La base no tiene movimientos. Corre primero: python ingest.py")
        return 1

    # "</script>" dentro del JSON cerraria la etiqueta antes de tiempo.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    # __API__ = false: este HTML es un archivo suelto, no tiene servidor detras,
    # asi que la seccion de subir resumenes no se muestra.
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA__", data)
            .replace("__API__", "false"))
    args.salida.write_text(html, encoding="utf-8")

    n_ok = sum(1 for r in payload["resumenes"] if r["control_ok"])
    print(f"Tablero generado: {args.salida}")
    print(f"  {len(payload['movimientos'])} movimientos, "
          f"{n_ok}/{len(payload['resumenes'])} resumenes cuadran con sus totales")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

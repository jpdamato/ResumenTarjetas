"""Carga los PDFs de resumenes a la base local.

Uso:
    python ingest.py                    # lee ../bna y ../santander
    python ingest.py --carpeta ruta     # lee otra carpeta (recursivo)
    python ingest.py --recategorizar    # reaplica categories.json, sin releer PDFs
    python ingest.py --reset            # borra la base y vuelve a cargar todo

Cada resumen se controla contra los totales que declara el propio PDF. Si
alguno no cuadra, se avisa: es preferible saber que un mes esta mal leido
antes que mirar un grafico equivocado.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import db
from categorize import Categorizer
from parsers import bna, santander
from reconcile import check_statement, format_report

BASE = Path(__file__).parent.parent

# Como reconocer a que banco pertenece cada PDF
PARSERS = [
    ("santander", santander, ("santander", "visa")),
    ("bna", bna, ("bna", "nativa", "nacion", "mastercard")),
]


def pick_parser(path: Path):
    """Elige parser por carpeta o por nombre de archivo."""
    hint = f"{path.parent.name} {path.name}".lower()
    for _, module, keywords in PARSERS:
        if any(k in hint for k in keywords):
            return module
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Carga resumenes de tarjeta a SQLite")
    # Dentro de Docker las rutas vienen por variables de entorno, asi que
    # `python ingest.py --recategorizar` funciona sin pasar ninguna ruta.
    ap.add_argument("--carpeta", type=Path,
                    default=Path(os.environ["DATOS"]) if os.environ.get("DATOS") else None,
                    help="carpeta con PDFs (por defecto ../bna y ../santander)")
    ap.add_argument("--base", type=Path,
                    default=Path(os.environ.get("BASE", db.DEFAULT_DB)))
    ap.add_argument("--recategorizar", action="store_true",
                    help="reaplica categories.json a lo ya cargado")
    ap.add_argument("--reset", action="store_true", help="borra la base primero")
    args = ap.parse_args()

    categorizer = Categorizer()
    conn = db.connect(args.base)

    if args.reset:
        # Se vacian los movimientos, pero NO las correcciones de categoria: son
        # trabajo manual que no se puede recuperar releyendo los PDFs.
        # Para borrar todo de verdad, borra el archivo .db a mano.
        conn.executescript("DELETE FROM movimientos; DELETE FROM resumenes;")
        conn.commit()
        quedan = conn.execute(
            "SELECT COUNT(*) FROM categorias_manuales").fetchone()[0]
        print("Movimientos borrados, se recargan desde los PDFs.")
        if quedan:
            print(f"  ({quedan} correcciones de categoria se conservan)")

    if args.recategorizar:
        r = db.recategorize(conn, categorizer)
        print(f"Recategorizados {r['total']} movimientos con las reglas actuales.")
        if r["manuales"]:
            print(f"  ({r['manuales']} conservan la categoria que corregiste a mano)")
        return 0

    if args.carpeta:
        pdfs = sorted(args.carpeta.rglob("*.pdf"))
    else:
        pdfs = sorted(BASE.glob("*/*.pdf"))

    if not pdfs:
        print("No se encontraron PDFs.", file=sys.stderr)
        return 1

    total_new = 0
    problemas = []

    for pdf_path in pdfs:
        module = pick_parser(pdf_path)
        if module is None:
            print(f"  ?  {pdf_path.name}: no se reconoce el banco, se saltea")
            continue

        try:
            statement = module.parse(str(pdf_path))
        except Exception as exc:                      # noqa: BLE001
            print(f"  X  {pdf_path.name}: error al parsear -> {exc}")
            problemas.append(pdf_path.name)
            continue

        checks = check_statement(statement)
        ok = all(c.ok for c in checks)
        detail = "\n".join(format_report(statement, checks))

        inserted = db.insert_transactions(conn, statement.transactions, categorizer)
        total_new += inserted

        conn.execute(
            """INSERT OR REPLACE INTO resumenes
               (banco, periodo, archivo, cierre, vencimiento, control_ok, control_txt)
               VALUES (?,?,?,?,?,?,?)""",
            (statement.bank, statement.period, str(pdf_path),
             statement.close_date.isoformat() if statement.close_date else None,
             statement.due_date.isoformat() if statement.due_date else None,
             1 if ok else 0, detail),
        )
        conn.commit()

        flag = "OK " if ok else "REVISAR"
        print(f"  {flag} {statement.bank:<10} {statement.period}  "
              f"{len(statement.transactions):>3} movs, {inserted:>3} nuevos  "
              f"({pdf_path.name[:38]})")
        if not ok:
            print(detail)
            problemas.append(f"{statement.bank} {statement.period}")

    n_total = conn.execute("SELECT COUNT(*) FROM movimientos").fetchone()[0]
    print(f"\nMovimientos nuevos: {total_new}. Total en la base: {n_total}")
    print(f"Base: {args.base}")

    if problemas:
        print("\nAtencion, estos resumenes no cuadran con sus propios totales:")
        for p in problemas:
            print(f"  - {p}")
        print("Los numeros de esos periodos pueden estar incompletos.")
    else:
        print("Todos los resumenes cuadran con los totales que declaran los PDFs.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

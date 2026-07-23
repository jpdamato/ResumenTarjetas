"""Servidor web: muestra el tablero y permite subir resumenes nuevos.

Endpoints:
    GET  /              el tablero, con los datos actuales incrustados
    GET  /api/datos     los movimientos en JSON (para refrescar sin recargar)
    POST /api/subir     recibe un PDF + el banco, lo procesa y lo incorpora

La base NUNCA se publica por HTTP: solo se sirven la pagina y estos endpoints.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pdfplumber
from flask import Flask, jsonify, request

import db
from categorize import Categorizer
from dashboard import build_payload
from parsers import bna, santander
from reconcile import check_statement, format_report

DATOS = Path(os.environ.get("DATOS", "/datos"))
BASE = Path(os.environ.get("BASE", "/salida/tarjetas.db"))
TEMPLATE = Path(__file__).with_name("template.html")

# Cada banco: su parser y como reconocerlo dentro del PDF.
BANCOS = {
    "santander": {
        "nombre": "Santander VISA",
        "parser": santander,
        "senas": ("SANTANDER",),
    },
    "bna": {
        "nombre": "BNA Nativa Mastercard",
        "parser": bna,
        "senas": ("NATIVA", "BANCO NACION", "MASTERCARD INTERNACIONAL"),
    },
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # 32 MB por archivo


@app.errorhandler(413)
def demasiado_grande(_e):
    return jsonify(ok=False, error="El archivo supera los 32 MB."), 413


@app.errorhandler(Exception)
def error_inesperado(e):
    """Cualquier error de /api/* sale como JSON, no como pagina HTML.

    La pagina espera JSON: si recibe el HTML de error de Flask, el fetch falla
    al parsearlo y el usuario ve un mensaje que no explica nada. Asi al menos
    llega el motivo real.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException) and e.code != 500:
        return e
    app.logger.exception("Error atendiendo %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500
    return e


# --------------------------------------------------------------------------
def nombre_seguro(nombre: str) -> str:
    """Se queda solo con el nombre del archivo, sin rutas ni '..'.

    Sin esto, un nombre como "../../etc/algo.pdf" escribiria fuera de la
    carpeta de datos.
    """
    limpio = Path(nombre.replace("\\", "/")).name
    limpio = "".join(c for c in limpio if c.isalnum() or c in " ._-()áéíóúñÁÉÍÓÚÑ")
    return limpio.strip() or "resumen.pdf"


def detectar_banco(pdf_path: Path) -> str | None:
    """Mira el texto del PDF para saber de que banco es."""
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            texto = " ".join(
                (p.extract_text() or "") for p in pdf.pages[:2]
            ).upper()
    except Exception:                                    # noqa: BLE001
        return None
    for clave, cfg in BANCOS.items():
        if any(s in texto for s in cfg["senas"]):
            return clave
    return None


def mismo_archivo(a: Path, b: Path) -> bool:
    return (a.exists() and b.exists()
            and a.stat().st_size == b.stat().st_size
            and a.read_bytes() == b.read_bytes())


def destino_unico(carpeta: Path, nombre: str, origen: Path) -> tuple[Path, bool]:
    """Dónde guardar el PDF y si hace falta copiarlo.

    Devuelve (destino, copiar). `copiar` viene en False cuando el archivo ya
    está guardado y es idéntico: ahí no hay nada que escribir. Ademas de ser
    inutil, sobrescribirlo fallaba — en un bind mount de Windows el usuario del
    contenedor no puede tocar un archivo que pertenece al host.
    """
    destino = carpeta / nombre
    if not destino.exists():
        return destino, True
    if mismo_archivo(destino, origen):
        return destino, False
    tronco, sufijo = destino.stem, destino.suffix
    for n in range(2, 100):
        alterno = carpeta / f"{tronco} ({n}){sufijo}"
        if not alterno.exists():
            return alterno, True
        if mismo_archivo(alterno, origen):
            return alterno, False
    return carpeta / f"{tronco} ({os.getpid()}){sufijo}", True


# --------------------------------------------------------------------------
@app.get("/")
def index():
    conn = db.connect(BASE)
    payload = build_payload(conn)
    conn.close()
    import json
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA__", data)
            .replace("__API__", "true"))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/api/datos")
def api_datos():
    conn = db.connect(BASE)
    payload = build_payload(conn)
    conn.close()
    return jsonify(payload)


@app.post("/api/categoria")
def api_categoria():
    """Corrige la categoria de un movimiento (por defecto, de todo el comercio)."""
    datos = request.get_json(silent=True) or {}
    mov_id = datos.get("id")
    categoria = (datos.get("categoria") or "").strip()
    solo_este = bool(datos.get("solo_este"))

    if not isinstance(mov_id, int):
        return jsonify(ok=False, error="Falta el id del movimiento."), 400
    if not categoria:
        return jsonify(ok=False, error="Falta la categoria."), 400
    if len(categoria) > 60:
        return jsonify(ok=False, error="El nombre de categoria es muy largo."), 400

    conn = db.connect(BASE)
    try:
        r = db.set_categoria(conn, mov_id, categoria, solo_este=solo_este)
    except LookupError as exc:
        return jsonify(ok=False, error=str(exc)), 404
    finally:
        conn.close()

    if solo_este:
        mensaje = f"Categoría cambiada a «{categoria}» en este movimiento."
    else:
        mensaje = (f"«{r['comercio']}» ahora es «{categoria}» "
                   f"({r['afectados']} movimientos, incluidos los que se suban "
                   f"más adelante).")
    return jsonify(ok=True, mensaje=mensaje, **r)


@app.post("/api/subir")
def api_subir():
    archivo = request.files.get("archivo")
    banco = (request.form.get("banco") or "").strip().lower()

    if archivo is None or not archivo.filename:
        return jsonify(ok=False, error="No llego ningun archivo."), 400
    if banco and banco not in BANCOS and banco != "auto":
        return jsonify(ok=False, error=f"Banco desconocido: {banco}"), 400

    nombre = nombre_seguro(archivo.filename)
    if not nombre.lower().endswith(".pdf"):
        return jsonify(ok=False, error=f"'{nombre}' no es un PDF."), 400

    tmp_dir = Path(tempfile.mkdtemp(prefix="subida-"))
    tmp_pdf = tmp_dir / nombre
    try:
        archivo.save(str(tmp_pdf))

        # Un .pdf puede ser cualquier cosa renombrada: lo confirmamos por firma.
        with open(tmp_pdf, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                return jsonify(ok=False,
                               error=f"'{nombre}' no parece un PDF valido."), 400

        detectado = detectar_banco(tmp_pdf)

        if banco in ("", "auto"):
            if detectado is None:
                return jsonify(
                    ok=False,
                    error="No pude reconocer el banco. Elegilo a mano.",
                ), 400
            banco = detectado
        elif detectado and detectado != banco:
            # Parsear un resumen con el parser del otro banco no da error:
            # da numeros mal o vacios. Preferimos frenar y avisar.
            return jsonify(
                ok=False,
                error=(f"Elegiste {BANCOS[banco]['nombre']} pero el PDF parece "
                       f"de {BANCOS[detectado]['nombre']}. Revisá el banco."),
            ), 400

        cfg = BANCOS[banco]

        # Se parsea ANTES de guardarlo: si el PDF no sirve, no queda basura
        # en la carpeta de resumenes.
        try:
            statement = cfg["parser"].parse(str(tmp_pdf))
        except Exception as exc:                          # noqa: BLE001
            return jsonify(ok=False,
                           error=f"No pude leer el resumen: {exc}"), 400

        if not statement.transactions:
            return jsonify(
                ok=False,
                error=("El PDF se leyo pero no tiene movimientos. "
                       "¿Es un resumen de tarjeta?"),
            ), 400

        carpeta = DATOS / banco
        carpeta.mkdir(parents=True, exist_ok=True)
        destino, copiar = destino_unico(carpeta, nombre, tmp_pdf)
        if copiar:
            # copyfile y no copy2: copy2 ademas copia fecha y permisos, y eso
            # falla sobre los volumenes montados de Windows. Solo nos interesa
            # el contenido.
            shutil.copyfile(tmp_pdf, destino)

        # Se reparsea desde su ubicacion final para que quede guardada la ruta
        # definitiva en la base.
        statement = cfg["parser"].parse(str(destino))
        checks = check_statement(statement)
        control_ok = all(c.ok for c in checks)
        detalle = "\n".join(format_report(statement, checks))

        conn = db.connect(BASE)
        nuevos = db.insert_transactions(conn, statement.transactions,
                                        Categorizer())
        conn.execute(
            """INSERT OR REPLACE INTO resumenes
               (banco, periodo, archivo, cierre, vencimiento, control_ok, control_txt)
               VALUES (?,?,?,?,?,?,?)""",
            (statement.bank, statement.period, str(destino),
             statement.close_date.isoformat() if statement.close_date else None,
             statement.due_date.isoformat() if statement.due_date else None,
             1 if control_ok else 0, detalle),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM movimientos").fetchone()[0]
        conn.close()

        if nuevos == 0:
            mensaje = (f"{cfg['nombre']} · {statement.period}: ya estaba cargado, "
                       f"no se agrego nada.")
        else:
            mensaje = (f"{cfg['nombre']} · {statement.period}: "
                       f"{nuevos} movimientos nuevos.")
        if not control_ok:
            mensaje += "  OJO: no cuadra con los totales del PDF."

        return jsonify(ok=True, banco=cfg["nombre"], periodo=statement.period,
                       nuevos=nuevos, total=total, control_ok=control_ok,
                       detalle=detalle, mensaje=mensaje,
                       archivo=destino.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    puerto = int(os.environ.get("PUERTO", "8080"))
    BASE.parent.mkdir(parents=True, exist_ok=True)
    try:
        from waitress import serve
        print(f"==> Tablero disponible en http://localhost:{puerto}", flush=True)
        serve(app, host="0.0.0.0", port=puerto, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=puerto)


if __name__ == "__main__":
    main()

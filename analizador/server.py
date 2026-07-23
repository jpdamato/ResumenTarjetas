"""Servidor web: muestra el tablero y permite subir resumenes nuevos.

Hay usuarios: cada uno entra con su usuario y contraseña y ve unicamente sus
propios movimientos. Todo endpoint que toca datos pasa por @login_requerido, y
todas las consultas van filtradas por `usuario_id` (ver db.py): no hay forma de
llegar a los movimientos de otro cambiando un id en la URL.

Endpoints:
    GET  /              el tablero del usuario logueado, con sus datos
    GET  /login         formulario de acceso
    POST /login         valida y abre la sesion
    GET  /registro      formulario de alta (si REGISTRO_ABIERTO)
    POST /registro      crea el usuario y lo deja adentro
    POST /salir         cierra la sesion
    GET  /api/datos     los movimientos del usuario en JSON
    POST /api/categoria corrige la categoria de un movimiento suyo
    POST /api/subir     recibe un PDF + el banco, lo procesa y lo incorpora

La base NUNCA se publica por HTTP: solo se sirven la pagina y estos endpoints.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from functools import wraps
from pathlib import Path

import pdfplumber
from flask import Flask, g, jsonify, redirect, request, url_for
from markupsafe import escape

import auth
import db
from categorize import Categorizer
from dashboard import build_payload
from parsers import bna, santander
from reconcile import check_statement, format_report

DATOS = Path(os.environ.get("DATOS", "/datos"))
BASE = Path(os.environ.get("BASE", "/salida/tarjetas.db"))
TEMPLATE = Path(__file__).with_name("template.html")
LOGIN_HTML = Path(__file__).with_name("login.html")

COOKIE = "sesion"

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
# sesion
def login_requerido(f):
    """Abre la base, exige sesion valida y deja al usuario en `g`.

    La conexion se abre y se cierra por request: sqlite no comparte conexiones
    entre hilos y waitress atiende con varios.
    """
    @wraps(f)
    def envoltorio(*a, **kw):
        conn = db.connect(BASE)
        usuario = auth.usuario_de_sesion(conn, request.cookies.get(COOKIE))
        if usuario is None:
            conn.close()
            if request.path.startswith("/api/"):
                return jsonify(ok=False, login=True,
                               error="La sesión venció. Volvé a entrar."), 401
            return redirect(url_for("login", proximo=request.full_path.rstrip("?")))
        g.conn, g.usuario = conn, usuario
        try:
            return f(*a, **kw)
        finally:
            conn.close()
    return envoltorio


def _pagina_login(*, registro=False, error="", usuario="", proximo="/"):
    tabs = (f'<div class="tabs">'
            f'<a href="{url_for("login")}" class="{"" if registro else "on"}">Entrar</a>'
            f'<a href="{url_for("registro")}" class="{"on" if registro else ""}">Crear cuenta</a>'
            f'</div>') if auth.REGISTRO_ABIERTO else ""
    nota = (f"Elegí un usuario y una contraseña de al menos "
            f"{auth.CLAVE_MINIMA} caracteres. Vas a arrancar con el tablero "
            f"vacío: subí tus resúmenes desde ahí."
            if registro else
            "Tus resúmenes son solo tuyos: nadie más los ve.")
    return (LOGIN_HTML.read_text(encoding="utf-8")
            .replace("__TABS__", tabs)
            .replace("__ERROR__", f'<div class="error">{escape(error)}</div>' if error else "")
            .replace("__ACCION__", url_for("registro") if registro else url_for("login"))
            .replace("__PROXIMO__", escape(proximo))
            .replace("__USUARIO__", escape(usuario))
            .replace("__AUTOCOMPLETE__", "new-password" if registro else "current-password")
            .replace("__BOTON__", "Crear cuenta" if registro else "Entrar")
            .replace("__NOTA__", nota))


def _destino_seguro(proximo: str) -> str:
    """Solo se redirige dentro de este sitio.

    Sin esto, /login?proximo=https://otro.sitio convierte al login en un trampolin
    comodo para mandar a alguien a una pagina que imita a esta.
    """
    if proximo.startswith("/") and not proximo.startswith("//"):
        return proximo
    return url_for("index")


def _responder_con_sesion(conn, usuario_id: int, proximo: str):
    token = auth.abrir_sesion(conn, usuario_id)
    resp = redirect(_destino_seguro(proximo))
    resp.set_cookie(
        COOKIE, token,
        max_age=auth.DIAS_SESION * 24 * 3600,
        httponly=True,          # que el JS de la pagina no pueda leer el token
        samesite="Lax",         # un POST desde otro sitio no lleva la cookie
        secure=request.is_secure,
    )
    return resp


# Freno simple a la fuerza bruta: se cuentan los intentos fallidos por IP.
# En memoria, porque el servidor es uno solo; se pierde al reiniciar y esta bien.
_FALLOS: dict[str, list] = {}
MAX_FALLOS, VENTANA = 10, 15 * 60


def _bloqueado(ip: str) -> bool:
    n, desde = _FALLOS.get(ip, (0, 0.0))
    if time.monotonic() - desde > VENTANA:
        _FALLOS.pop(ip, None)
        return False
    return n >= MAX_FALLOS


def _fallo(ip: str) -> None:
    n, desde = _FALLOS.get(ip, (0, time.monotonic()))
    if time.monotonic() - desde > VENTANA:
        n, desde = 0, time.monotonic()
    _FALLOS[ip] = [n + 1, desde]


@app.get("/login")
def login():
    conn = db.connect(BASE)
    try:
        if auth.usuario_de_sesion(conn, request.cookies.get(COOKIE)):
            return redirect(url_for("index"))
    finally:
        conn.close()
    return _pagina_login(proximo=request.args.get("proximo", "/"))


@app.post("/login")
def login_post():
    usuario = (request.form.get("usuario") or "").strip()
    clave = request.form.get("clave") or ""
    proximo = request.form.get("proximo") or "/"
    ip = request.remote_addr or "?"

    if _bloqueado(ip):
        return _pagina_login(
            error="Demasiados intentos fallidos. Esperá unos minutos.",
            usuario=usuario, proximo=proximo), 429

    conn = db.connect(BASE)
    try:
        fila = auth.autenticar(conn, usuario, clave)
        if fila is None:
            _fallo(ip)
            # Un solo mensaje para los dos casos: decir "ese usuario no existe"
            # le confirma a cualquiera que prueba nombres cuales si existen.
            return _pagina_login(error="Usuario o contraseña incorrectos.",
                                 usuario=usuario, proximo=proximo), 401
        _FALLOS.pop(ip, None)
        return _responder_con_sesion(conn, fila["id"], proximo)
    finally:
        conn.close()


@app.get("/registro")
def registro():
    if not auth.REGISTRO_ABIERTO:
        return _pagina_login(error="El registro está cerrado. "
                                   "Pedí que te creen el usuario."), 403
    return _pagina_login(registro=True, proximo=request.args.get("proximo", "/"))


@app.post("/registro")
def registro_post():
    if not auth.REGISTRO_ABIERTO:
        return _pagina_login(error="El registro está cerrado."), 403
    usuario = (request.form.get("usuario") or "").strip()
    clave = request.form.get("clave") or ""
    proximo = request.form.get("proximo") or "/"

    conn = db.connect(BASE)
    try:
        try:
            uid = auth.crear_usuario(conn, usuario, clave)
        except ValueError as exc:
            return _pagina_login(registro=True, error=str(exc),
                                 usuario=usuario, proximo=proximo), 400
        return _responder_con_sesion(conn, uid, proximo)
    finally:
        conn.close()


@app.post("/salir")
def salir():
    token = request.cookies.get(COOKIE)
    conn = db.connect(BASE)
    try:
        auth.cerrar_sesion(conn, token)
    finally:
        conn.close()
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE)
    return resp


# --------------------------------------------------------------------------
def nombre_seguro(nombre: str) -> str:
    """Se queda solo con el nombre del archivo, sin rutas ni '..'.

    Sin esto, un nombre como "../../etc/algo.pdf" escribiria fuera de la
    carpeta de datos.
    """
    limpio = Path(nombre.replace("\\", "/")).name
    limpio = "".join(c for c in limpio if c.isalnum() or c in " ._-()áéíóúñÁÉÍÓÚÑ")
    return limpio.strip() or "resumen.pdf"


def carpeta_pdfs(conn, usuario_id: int, banco: str) -> Path:
    """Donde se guardan los PDFs de ese usuario.

    Cada usuario tiene su propia carpeta, para que los resumenes de uno no
    queden mezclados con los de otro ni en la base ni en el disco.

    El usuario mas antiguo es la excepcion: sigue usando /datos/<banco>, que es
    donde ya estaban sus PDFs desde antes de que existieran los usuarios y lo
    que montan los volumenes de siempre. Moverselos ahi habria significado
    tocar el docker-compose de una instalacion que ya funciona.
    """
    if usuario_id == auth.usuario_legado(conn):
        return DATOS / banco
    return DATOS / "usuarios" / str(usuario_id) / banco


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
@login_requerido
def index():
    payload = build_payload(g.conn, g.usuario["id"])
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DATA__", data)
            .replace("__API__", "true")
            .replace("__USUARIO__", json.dumps(g.usuario["usuario"])))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/api/datos")
@login_requerido
def api_datos():
    return jsonify(build_payload(g.conn, g.usuario["id"]))


@app.post("/api/categoria")
@login_requerido
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

    try:
        r = db.set_categoria(g.conn, g.usuario["id"], mov_id, categoria,
                             solo_este=solo_este)
    except LookupError as exc:
        return jsonify(ok=False, error=str(exc)), 404

    if solo_este:
        mensaje = f"Categoría cambiada a «{categoria}» en este movimiento."
    else:
        mensaje = (f"«{r['comercio']}» ahora es «{categoria}» "
                   f"({r['afectados']} movimientos, incluidos los que se suban "
                   f"más adelante).")
    return jsonify(ok=True, mensaje=mensaje, **r)


@app.post("/api/subir")
@login_requerido
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

        carpeta = carpeta_pdfs(g.conn, g.usuario["id"], banco)
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

        nuevos = db.insert_transactions(g.conn, g.usuario["id"],
                                        statement.transactions, Categorizer())
        db.guardar_resumen(g.conn, g.usuario["id"], statement, control_ok,
                           detalle, str(destino))
        total = g.conn.execute(
            "SELECT COUNT(*) FROM movimientos WHERE usuario_id = ?",
            (g.usuario["id"],)).fetchone()[0]

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
    # Crea la base y el usuario inicial antes de atender el primer pedido.
    db.connect(BASE).close()
    try:
        from waitress import serve
        print(f"==> Tablero disponible en http://localhost:{puerto}", flush=True)
        serve(app, host="0.0.0.0", port=puerto, threads=4)
    except ImportError:
        app.run(host="0.0.0.0", port=puerto)


if __name__ == "__main__":
    main()

#!/bin/sh
# Arranca todo: carga los PDFs, genera el tablero y lo sirve.
set -e

DATOS="${DATOS:-/datos}"
BASE="${BASE:-/salida/tarjetas.db}"
WEB="${WEB:-/web}"
PUERTO="${PUERTO:-8080}"

mkdir -p "$(dirname "$BASE")" "$WEB" "$DATOS/usuarios"

# Solo las carpetas por banco: /datos/usuarios es de los demas usuarios y cada
# uno carga las suyas al entrar, no se le pueden atribuir al usuario inicial.
echo "==> Cargando resumenes del usuario inicial desde $DATOS"
if ! python ingest.py --carpeta "$DATOS" --base "$BASE" --excluir "$DATOS/usuarios"; then
    if [ -f "$BASE" ]; then
        echo "!!  No se pudieron cargar PDFs nuevos. Sigo con la base ya cargada."
    else
        echo "!!  No hay PDFs en $DATOS ni una base previa."
        echo "    Revisa que las carpetas esten montadas (ver docker-compose.yml)."
        exit 1
    fi
fi

echo
echo "==> Generando copia offline del tablero"
# Version autocontenida, para abrir sin servidor. Va afuera de lo que se
# publica: la base nunca se sirve por HTTP.
python dashboard.py --base "$BASE" --salida "$(dirname "$BASE")/tablero.html" || true

echo
echo "==> Levantando el servidor (permite subir resumenes nuevos)"
echo "    Ctrl+C para parar."
echo
exec python server.py

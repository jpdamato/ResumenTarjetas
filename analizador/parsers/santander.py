"""Parser de resumenes Santander Rio VISA.

Layout (coordenadas medidas sobre los PDFs reales, pagina de 612pt):

    anio  mes    dia comprob. flag descripcion        cuota   orig.    $        U$S
    x0=21 x0=36  x0=74 x0=88  x0=122 x0=136           x0=271  x1=334  x1=458   x1=550

Detalles que importan:

* anio y mes son "pegajosos": aparecen solo cuando cambian, las filas
  siguientes los heredan.
* Las columnas se corren algunos puntos entre filas (el dia aparece en x0=67
  o x0=74), asi que NO usamos posiciones exactas sino bandas tolerantes, y
  para los importes usamos el borde DERECHO (estan alineados a la derecha).
* La fila "LIMITES: ... FINANCIACION U$S 17.459,10" tiene un importe en la
  misma banda que la columna U$S. Por eso solo leemos filas que esten dentro
  de la seccion de movimientos (entre el encabezado "Fecha Comprobante
  Referencia" y "SALDO ACTUAL").
* En las compras en cuotas la FECHA es la de la compra original, pero el
  IMPORTE es el de la cuota de este mes.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pdfplumber

from .base import extract_rows, full_year, month_from_name, strip_accents
from .model import Statement, Transaction, classify_kind, parse_cuota

BANK = "Santander"

# Bandas de columnas (borde derecho para importes)
COL_ORIGIN = (300.0, 360.0)   # importe en moneda de origen (informativo)
COL_ARS = (430.0, 490.0)
COL_USD = (500.0, 580.0)

# Bandas de texto (borde izquierdo)
X_LEFT_BLOCK = 126.0          # anio/mes/dia/comprobante/flag
X_DESC_END = 300.0            # fin de la descripcion

RE_YEAR = re.compile(r"^\d{2}$")
RE_DAY = re.compile(r"^\d{1,2}$")
RE_RECEIPT = re.compile(r"^\d{4,8}$")

RE_CIERRE = re.compile(r"CIERRE\s+(\d{1,2})\s+([A-Za-z]{3,10})\.?\s+(\d{2})", re.I)
RE_VTO = re.compile(r"VENCIMIENTO\s+(\d{1,2})\s+([A-Za-z]{3,10})\.?\s+(\d{2})", re.I)

RE_HEADER = re.compile(r"Fecha\s+Comprobante\s+Referencia", re.I)
RE_SECTION_END = re.compile(
    # "Cuotas a vencer" abre una tabla de PROYECCION de cuotas futuras
    # ("$1.503.672,65 $1.094.332,09 ...") en hojas posteriores. Como el
    # encabezado "Fecha Comprobante Referencia" se repite en cada hoja, sin
    # este corte esas proyecciones se colarian como consumos.
    r"SALDO ACTUAL|PAGO MINIMO|PRESENTE ES COPIA|Cuotas a vencer",
    re.I,
)

# "Tarjeta 8668 Total Consumos de MARIA BE VILLARREAL 883.539,05 * 0,00 *"
# Cierra el bloque de consumos de una tarjeta: todo lo de arriba es de ella.
RE_TOTAL_TARJETA = re.compile(
    r"Total\s+Consumos\s+de\s+(.+?)(?=\s+[\d.]*\d,\d{2})", re.I
)


def _tidy_cardholder(raw: str) -> str:
    """'4660 5700 9473 8650' -> 'Tarjeta ...8650'; nombres se dejan igual."""
    compact = raw.replace(" ", "")
    if compact.isdigit():
        return f"Tarjeta ...{compact[-4:]}"
    return " ".join(raw.split())


def _parse_date_match(m: re.Match) -> date | None:
    month = month_from_name(m.group(2))
    if not month:
        return None
    try:
        return date(full_year(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _parse_left_block(words) -> dict:
    """Interpreta el bloque izquierdo: anio, mes, dia, comprobante, flag."""
    out: dict = {}
    for w in words:
        t = w.text
        if RE_YEAR.match(t) and w.x0 < 32 and "year" not in out:
            out["year"] = full_year(t)
        elif t.isalpha() or t.rstrip(".").isalpha():
            month = month_from_name(t)
            if month and "month" not in out:
                out["month"] = month
        elif RE_RECEIPT.match(t) and len(t) >= 4 and "receipt" not in out:
            out["receipt"] = t
        elif RE_DAY.match(t) and "day" not in out:
            out["day"] = int(t)
        elif len(t) == 1 and "flag" not in out:
            out["flag"] = t
    return out


def parse(path: str) -> Statement:
    pdf = pdfplumber.open(path)

    full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    close_date = due_date = None
    if (m := RE_CIERRE.search(full_text)):
        close_date = _parse_date_match(m)
    if (m := RE_VTO.search(full_text)):
        due_date = _parse_date_match(m)

    period = close_date.strftime("%Y-%m") if close_date else "desconocido"

    transactions: list[Transaction] = []
    stated: dict[str, Decimal] = {}

    # anio/mes/dia se arrastran entre filas y entre paginas: el resumen solo
    # los imprime cuando cambian. Varias compras del mismo dia aparecen con la
    # fecha en blanco y heredan la de la fila anterior.
    cur_year: int | None = None
    cur_month: int | None = None
    cur_day: int | None = None

    # Los consumos se agrupan por tarjeta y el nombre del titular recien
    # aparece en la fila "Total Consumos de X" que CIERRA el bloque. Por eso
    # vamos acumulando y asignamos hacia atras al encontrarla.
    pending: list[Transaction] = []

    # El resumen tiene dos bloques separados por una linea de guiones bajos:
    # arriba lo que es de la CUENTA (pagos, creditos, comisiones del paquete)
    # y abajo los CONSUMOS, agrupados por tarjeta. Los totales "Total Consumos
    # de X" solo cubren el bloque de abajo, asi que solo esas filas se
    # atribuyen a una tarjeta.
    in_consumos = False

    for page_no, page in enumerate(pdf.pages, start=1):
        in_section = False
        if page_no > 1:
            in_consumos = True   # el bloque de cuenta solo esta en la hoja 1
        for row in extract_rows(page):
            text = row.text

            if RE_HEADER.search(text):
                in_section = True
                continue
            if in_section and RE_SECTION_END.search(text):
                in_section = False
                continue
            if not in_section:
                continue
            if set(text.strip()) <= {"_", " "}:      # separador de seccion
                in_consumos = True
                continue

            # Cierre del bloque de una tarjeta: bautiza los consumos previos y
            # deja anotado el total declarado para poder reconciliar despues.
            if (m := RE_TOTAL_TARJETA.search(text)):
                holder = _tidy_cardholder(m.group(1))
                for t in pending:
                    if t.cardholder is None:
                        t.cardholder = holder
                declared = [v for _, v in row.amounts()]
                if declared:
                    stated[f"Total Consumos {holder} ARS"] = declared[0]
                if len(declared) > 1:
                    stated[f"Total Consumos {holder} USD"] = declared[1]
                pending.clear()
                continue

            left = _parse_left_block([w for w in row.words if w.x0 < X_LEFT_BLOCK])
            if "year" in left:
                cur_year = left["year"]
            if "month" in left:
                cur_month = left["month"]
            if "day" in left:
                cur_day = left["day"]

            # Descripcion: texto entre el bloque izquierdo y las columnas de importe
            desc_words = [
                w for w in row.words if X_LEFT_BLOCK <= w.x0 < X_DESC_END
            ]

            cuota_nro = cuota_total = None
            desc_parts: list[str] = []
            for w in desc_words:
                if (c := parse_cuota(w.text)):
                    cuota_nro, cuota_total = c
                    continue
                desc_parts.append(w.text)

            # "Spotify USD 2,14" -> la etiqueta USD y el importe de origen no
            # son parte del nombre del comercio. Solo los sacamos cuando la
            # fila realmente tiene importe en moneda de origen, para no
            # mutilar descripciones como "SU PAGO EN USD".
            if row.amount_in_column(*COL_ORIGIN) is not None:
                while desc_parts and (
                    desc_parts[-1].upper() == "USD"
                    or desc_parts[-1].replace(",", "").replace(".", "").isdigit()
                ):
                    desc_parts.pop()

            description = " ".join(desc_parts).strip()
            # Un consumo real siempre tiene nombre de comercio. Si la
            # "descripcion" son solo numeros y simbolos, es una fila de alguna
            # tabla informativa, no un movimiento.
            if not any(ch.isalpha() for ch in description):
                continue

            ars = row.amount_in_column(*COL_ARS)
            usd = row.amount_in_column(*COL_USD)
            if ars is None and usd is None:
                continue

            # Clasificamos con el TEXTO COMPLETO de la fila, no solo con la
            # descripcion: en "Total Consumos de MARIA BE VILLARREAL" las
            # palabras "Total Consumos" caen fuera de la banda de descripcion,
            # y mirando solo la descripcion parecerian un comercio mas.
            kind = classify_kind(text)

            tx_date = None
            if cur_year and cur_month and cur_day:
                try:
                    tx_date = date(cur_year, cur_month, cur_day)
                except ValueError:
                    tx_date = None

            if kind in ("balance",):
                if ars is not None:
                    stated[f"{strip_accents(description)} ARS"] = ars
                if usd is not None:
                    stated[f"{strip_accents(description)} USD"] = usd
                continue

            for amount, currency in ((ars, "ARS"), (usd, "USD")):
                if amount is None:
                    continue
                tx = Transaction(
                    bank=BANK,
                    period=period,
                    date=tx_date,
                    description=description,
                    amount=amount,
                    currency=currency,
                    kind=kind,
                    cuota_nro=cuota_nro,
                    cuota_total=cuota_total,
                    receipt=left.get("receipt"),
                    source_file=path,
                    page=page_no,
                )
                transactions.append(tx)
                if in_consumos:
                    pending.append(tx)

    pdf.close()
    return Statement(
        bank=BANK,
        period=period,
        source_file=path,
        transactions=transactions,
        stated_totals=stated,
        close_date=close_date,
        due_date=due_date,
    )

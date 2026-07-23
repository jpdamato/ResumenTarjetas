"""Parser de resumenes Banco Nacion - Visa.

Es otro producto del mismo banco que Nativa Mastercard (bna.py), pero el PDF no
se parece: Visa usa un layout de una sola seccion, mas cercano al de Santander.

    FECHA      COMPROBANTE  DETALLE DE TRANSACCION   ...   PESOS      DOLARES
    x0~71      x0~124       x0~168                         x1~457     x1~530

    26.08.25   ...          SU PAGO EN PESOS                525.705,99-
    13.07.24   580970*      NSS*BROOKSFIELD   Cuota 15/18   11.377,50
    ...
    Tarjeta 5655 Total Consumos de JUAN PABLO DAMATO        740.357,09  0,00
    18.09.25   IMPUESTO DE SELLOS $                          9.509,76
    18.09.25   INTERESES FINANCIACION $                     43.076,68
    18.09.25   DB IVA $ 21%                    43.076,68      9.046,10
    SALDO ACTUAL $                                        1.301.989,63

Detalles que importan:

* Los importes se clasifican por su borde DERECHO, no por su posicion exacta:
  las compras cierran en x1~457 y los pagos en x1~461. La columna PESOS y la
  DOLARES estan bien separadas (x1~457 vs x1~530).

* La fila "DB IVA $ 21% 43.076,68 9.046,10" trae DOS numeros: la base (el
  interes, x1~365) y el IVA efectivamente cobrado (x1~457). La base cae fuera
  de las dos bandas de importe, asi que se ignora sola y solo se toma el IVA.

* Los COSTOS (sellos, intereses, IVA) van DESPUES de "Total Consumos", asi que
  ese total abarca solo los consumos. La reconciliacion fuerte no se apoya en
  el, sino en el saldo: SALDO ACTUAL = SALDO ANTERIOR - pagos + consumos +
  costos (ver reconcile.py, que usa las claves SALDO INICIAL/FINAL que emite
  este parser). Eso controla TODO el resumen, costos incluidos.

* En las compras en cuotas la FECHA es la de la compra original y el IMPORTE es
  el de la cuota de este mes, igual que en los otros bancos.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pdfplumber

from .base import extract_rows, full_year, month_from_name, strip_accents
from .model import Statement, Transaction, classify_kind

BANK = "BNA Visa"

# Bandas por borde derecho del importe.
COL_ARS = (400.0, 480.0)
COL_USD = (485.0, 550.0)

# Bandas de texto por borde izquierdo.
X_COMPROB = (118.0, 160.0)     # comprobante: 580970*, 004652K
X_DESC = (160.0, 283.0)        # nombre del comercio (antes de "Cuota" y montos)
X_CUOTA = (283.0, 345.0)       # "Cuota 15/18"

RE_FECHA_MOV = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})$")   # DD.MM.YY
RE_COMPROB = re.compile(r"^\d{5,7}[A-Za-z*]$")
RE_CUOTA = re.compile(r"^(\d{1,2})/(\d{1,2})$")
RE_PORCENTAJE = re.compile(r"^\d+(?:[.,]\d+)?%$")

RE_CIERRE = re.compile(r"CIERRE\s+ACTUAL\s*:?\s*(\d{1,2})\s+([A-Za-z]{3,})\.?\s+(\d{2})", re.I)
# Vencimiento del propio resumen: en la fila del encabezado el ano queda pegado
# al saldo ("01 Oct 251.301.989,63"). El lookahead a "d.ddd" ancla justo ese
# caso y evita confundirlo con "CIERRE ANTERIOR" o "PROXIMO VTO.".
RE_VTO = re.compile(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{2})(?=\d\.\d{3})")

RE_HEADER = re.compile(r"DETALLE\s+DE\s+TRANSACCION", re.I)
# Lo que corta la seccion viene DESPUES de SALDO ACTUAL y PAGO MINIMO, para
# alcanzar a leerlos como totales declarados antes de frenar.
RE_FIN = re.compile(r"DEBITAREMOS|Plan V\s*:|Programa de Lealtad", re.I)

RE_TOTAL_TARJETA = re.compile(
    r"Total\s+Consumos\s+de\s+(.+?)(?=\s+[\d.]*\d,\d{2})", re.I
)


def _mk_date(day: str, mon: str, yr: str) -> date | None:
    month = month_from_name(mon)
    if not month:
        return None
    try:
        return date(full_year(yr), month, int(day))
    except ValueError:
        return None


def _tidy_holder(raw: str) -> str:
    return " ".join(raw.split())


def parse(path: str) -> Statement:
    pdf = pdfplumber.open(path)

    full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    close_date = due_date = None
    if (m := RE_CIERRE.search(full_text)):
        close_date = _mk_date(*m.groups())
    if (m := RE_VTO.search(full_text)):
        due_date = _mk_date(*m.groups())

    period = close_date.strftime("%Y-%m") if close_date else "desconocido"

    transactions: list[Transaction] = []
    stated: dict[str, Decimal] = {}

    # Los consumos de una tarjeta se cierran con "Total Consumos de X": recien
    # ahi aparece el titular. Se acumulan y se bautizan hacia atras.
    pending: list[Transaction] = []

    for page_no, page in enumerate(pdf.pages, start=1):
        in_section = False
        for row in extract_rows(page):
            text = row.text.strip()

            if RE_HEADER.search(text):
                in_section = True
                continue
            if not in_section:
                continue
            if RE_FIN.search(text):
                in_section = False
                continue
            if set(text) <= {"_", " "}:            # separador de guiones bajos
                continue

            ars = row.amount_in_column(*COL_ARS)
            usd = row.amount_in_column(*COL_USD)
            if ars is None and usd is None:
                continue

            # Cierre del bloque de una tarjeta: nombra los consumos previos y
            # deja anotado el total declarado para reconciliar.
            if (m := RE_TOTAL_TARJETA.search(text)):
                holder = _tidy_holder(m.group(1))
                for t in pending:
                    if t.cardholder is None:
                        t.cardholder = holder
                if ars is not None:
                    stated[f"Total Consumos {holder} ARS"] = ars
                if usd is not None:
                    stated[f"Total Consumos {holder} USD"] = usd
                pending.clear()
                continue

            kind = classify_kind(text)

            if kind == "balance":
                # SALDO ANTERIOR / SALDO ACTUAL alimentan el control de saldo.
                # El resto (PAGO MINIMO, subtotales) se guarda pero no se usa.
                etiqueta = strip_accents(text)
                if "SALDO ANTERIOR" in etiqueta:
                    if ars is not None: stated["SALDO INICIAL ARS"] = ars
                    if usd is not None: stated["SALDO INICIAL USD"] = usd
                elif "SALDO ACTUAL" in etiqueta:
                    if ars is not None: stated["SALDO FINAL ARS"] = ars
                    if usd is not None: stated["SALDO FINAL USD"] = usd
                continue

            # Fecha (primer token DD.MM.YY), comprobante, cuota y descripcion,
            # cada cosa por su posicion horizontal.
            tx_date = None
            comprobante = None
            cuota_nro = cuota_total = None
            desc_parts: list[str] = []
            for w in row.words:
                if tx_date is None and (mm := RE_FECHA_MOV.match(w.text)):
                    tx_date = _mk_date(*mm.groups())
                    continue
                if comprobante is None and X_COMPROB[0] <= w.x0 < X_COMPROB[1] \
                        and RE_COMPROB.match(w.text):
                    comprobante = w.text
                    continue
                if X_CUOTA[0] <= w.x0 < X_CUOTA[1] and (cm := RE_CUOTA.match(w.text)):
                    cuota_nro, cuota_total = int(cm.group(1)), int(cm.group(2))
                    continue
                if X_DESC[0] <= w.x0 < X_DESC[1]:
                    # "$", "21%" y montos no son parte del nombre del comercio.
                    if w.text == "$" or RE_PORCENTAJE.match(w.text):
                        continue
                    if w.text.replace(".", "").replace(",", "").replace("-", "").isdigit():
                        continue
                    desc_parts.append(w.text)

            description = " ".join(desc_parts).strip()
            if not any(ch.isalpha() for ch in description):
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
                    receipt=comprobante,
                    source_file=path,
                    page=page_no,
                )
                transactions.append(tx)
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

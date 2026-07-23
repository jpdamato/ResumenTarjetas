"""Parser de resumenes Banco Nacion - Nativa Mastercard.

El PDF tiene dos secciones con movimientos, y CADA UNA usa columnas distintas:

  RESUMEN CONSOLIDADO   (pagos, intereses, IVA, sellos, comisiones)
      PESOS  x1~369      DOLAR  x1~446-460
  DETALLE DEL MES       (consumos del mes, hasta "TOTAL TITULAR")
      PESOS  x1~417      DOLAR  x1~494

Ojo: la banda DOLAR del resumen consolidado se solapa con la banda PESOS del
detalle. Si se usaran bandas unicas para todo el PDF, los intereses en pesos
se leerian como dolares. Por eso las bandas son por seccion.

Ademas el encabezado de cuenta (Saldo actual, Limites, Cuotas a Vencer) se
repite en todas las paginas y tiene importes: solo leemos dentro de las
secciones reconocidas.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pdfplumber

from .base import extract_rows, full_year, month_from_name, strip_accents
from .model import Statement, Transaction, classify_kind

BANK = "BNA"

# (ars_desde, ars_hasta, usd_desde, usd_hasta) por borde derecho
BANDS_CONSOLIDADO = (350.0, 390.0, 415.0, 470.0)
BANDS_DETALLE = (395.0, 440.0, 470.0, 515.0)

X_DESC_FROM = 60.0
X_DESC_TO = 290.0

RE_FECHA = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2})$")
RE_ESTADO = re.compile(r"Estado de cuenta al\s*:?\s*(\d{1,2})-([A-Za-z]{3})-(\d{2})", re.I)
RE_VTO = re.compile(r"Vencimiento actual\s*:?\s*(\d{1,2})-([A-Za-z]{3})-(\d{2})", re.I)

# "ASISTLEY16020176 12/25" / "PRO MKT 16020215 01/26" -> nombre del comercio
RE_LIMPIAR = re.compile(r"^(.*?)\s*(\d{7,9})\s*(\d{2}/\d{2})?\s*$")


def _norm(text: str) -> str:
    """Normaliza titulos para poder reconocerlos.

    El PDF 'dibuja' los titulos de seccion intercalando guiones bajos:
    "R__ES_U_M_E__N_C__ON__SO_L_I__DA__DO". Por eso sacamos guiones bajos Y
    espacios, y comparamos contra el titulo tambien sin espacios.
    """
    return strip_accents(text).replace("_", "").replace(" ", "")


def _mk_date(day: str, mon: str, yr: str) -> date | None:
    month = month_from_name(mon)
    if not month:
        return None
    try:
        return date(full_year(yr), month, int(day))
    except ValueError:
        return None


def _clean_description(text: str) -> str:
    m = RE_LIMPIAR.match(text.strip())
    if m and m.group(1).strip():
        return m.group(1).strip()
    return text.strip()


def parse(path: str) -> Statement:
    pdf = pdfplumber.open(path)

    full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    close_date = due_date = None
    if (m := RE_ESTADO.search(full_text)):
        close_date = _mk_date(*m.groups())
    if (m := RE_VTO.search(full_text)):
        due_date = _mk_date(*m.groups())

    period = close_date.strftime("%Y-%m") if close_date else "desconocido"

    transactions: list[Transaction] = []
    stated: dict[str, Decimal] = {}
    seen_consolidado = False   # el encabezado se repite; la seccion no debe

    for page_no, page in enumerate(pdf.pages, start=1):
        section: str | None = None

        for row in extract_rows(page):
            norm = _norm(row.text)

            if "RESUMENCONSOLIDADO" in norm:
                section = None if seen_consolidado else "consolidado"
                seen_consolidado = True
                continue
            if "DETALLEDELMES" in norm:
                section = "detalle"
                continue
            if section == "detalle" and "TOTALTITULAR" in norm:
                # es el total declarado: sirve para reconciliar
                a, b, c, d = BANDS_DETALLE
                if (v := row.amount_in_column(a, b)) is not None:
                    stated["TOTAL TITULAR ARS"] = v
                if (v := row.amount_in_column(c, d)) is not None:
                    stated["TOTAL TITULAR USD"] = v
                section = None
                continue
            if section is None:
                continue
            if set(row.text.strip()) <= {"_", " "}:
                continue

            bands = BANDS_CONSOLIDADO if section == "consolidado" else BANDS_DETALLE
            ars = row.amount_in_column(bands[0], bands[1])
            usd = row.amount_in_column(bands[2], bands[3])
            if ars is None and usd is None:
                continue

            tx_date = None
            desc_words = []
            for w in row.words:
                if (m := RE_FECHA.match(w.text)) and w.x0 < X_DESC_FROM:
                    tx_date = _mk_date(*m.groups())
                elif X_DESC_FROM <= w.x0 < X_DESC_TO:
                    desc_words.append(w.text)

            description = _clean_description(" ".join(desc_words))
            if not description:
                continue

            kind = classify_kind(description)
            if kind == "balance":
                if ars is not None:
                    stated[f"{strip_accents(description)} ARS"] = ars
                if usd is not None:
                    stated[f"{strip_accents(description)} USD"] = usd
                continue

            for amount, currency in ((ars, "ARS"), (usd, "USD")):
                if amount is None or amount == 0:
                    continue
                transactions.append(
                    Transaction(
                        bank=BANK,
                        period=period,
                        date=tx_date,
                        description=description,
                        amount=amount,
                        currency=currency,
                        kind=kind,
                        source_file=path,
                        page=page_no,
                    )
                )

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

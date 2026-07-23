"""Utilidades compartidas para leer PDFs de resumenes de tarjeta.

La idea central: los PDFs de resumen NO se pueden leer como texto plano.
La moneda de un importe esta determinada por la COLUMNA en la que aparece,
y la extraccion de texto plana (pdftotext -layout) pierde esa informacion y
llega a asociar importes a la descripcion equivocada.

Por eso trabajamos siempre con coordenadas: agrupamos palabras en filas por su
posicion vertical, y clasificamos los importes por su borde derecho (los numeros
estan alineados a la derecha, asi que el borde derecho es mucho mas estable que
el izquierdo).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Un importe: coma decimal con exactamente 2 decimales, puntos de miles.
#
# OJO con el agrupamiento: Santander imprime los millones como "2604.596,29"
# y "1500.000,00", es decir con el primer grupo de CUATRO digitos y sin el
# separador de millon. Una regex del tipo \d{1,3}(\.\d{3})* NO los reconoce y
# los descarta en silencio, perdiendo justo los importes mas grandes.
# Por eso aceptamos cualquier cantidad de digitos y puntos antes de la coma.
AMOUNT_RE = re.compile(r"^-?\$?\d[\d.]*,\d{2}-?$")


def parse_amount(token: str) -> Decimal | None:
    """Convierte un token de importe argentino a Decimal.

    Formato: punto = separador de miles, coma = decimal.
    Un "-" al final (o al principio) indica importe negativo (credito/pago).
    Devuelve None si el token no es un importe.

    Los 2 decimales son obligatorios: asi "TC1430,000" (tipo de cambio, 3
    decimales) no se confunde con un importe.
    """
    t = token.strip().replace("$", "").replace(" ", "").replace("\xa0", "")
    if not t or not AMOUNT_RE.match(t):
        return None
    negative = t.endswith("-") or t.startswith("-")
    t = t.strip("-")
    # "2604.596,29" -> "2604596.29"; "23990,00" -> "23990.00"
    t = t.replace(".", "").replace(",", ".")
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    return -value if negative else value


def strip_accents(text: str) -> str:
    """Normaliza para comparar: sin acentos, mayusculas, espacios colapsados."""
    nfkd = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).upper().strip()


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float

    @property
    def xmid(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Row:
    """Una linea visual del PDF: las palabras que comparten posicion vertical."""

    top: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def words_between(self, x_from: float, x_to: float) -> list[Word]:
        """Palabras cuyo borde IZQUIERDO cae en la banda (para texto)."""
        return [w for w in self.words if x_from <= w.x0 < x_to]

    def text_between(self, x_from: float, x_to: float) -> str:
        return " ".join(w.text for w in self.words_between(x_from, x_to))

    def amounts(self) -> list[tuple[Word, Decimal]]:
        """Todos los tokens de la fila que son importes, con su palabra."""
        out = []
        for w in self.words:
            value = parse_amount(w.text)
            if value is not None:
                out.append((w, value))
        return out

    def amount_in_column(self, x_from: float, x_to: float) -> Decimal | None:
        """Importe cuyo BORDE DERECHO cae en la banda dada.

        Usamos el borde derecho porque los importes estan alineados a la
        derecha: el borde izquierdo se corre segun la cantidad de digitos
        (129.999,00 empieza mucho antes que 7,00) mientras que el derecho
        se mantiene fijo en la columna.
        """
        for w, value in self.amounts():
            if x_from <= w.x1 < x_to:
                return value
        return None


def extract_rows(page, tolerance: float = 2.5) -> list[Row]:
    """Agrupa las palabras de una pagina en filas por posicion vertical.

    `tolerance` es cuanto pueden diferir los `top` de dos palabras para que
    se consideren de la misma linea (los renglones no estan perfectamente
    alineados al pixel).
    """
    words = [
        Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]))
        for w in page.extract_words(use_text_flow=False, keep_blank_chars=False)
    ]
    words.sort(key=lambda w: (w.top, w.x0))

    rows: list[Row] = []
    for w in words:
        if rows and abs(w.top - rows[-1].top) <= tolerance:
            rows[-1].words.append(w)
        else:
            rows.append(Row(top=w.top, words=[w]))

    for r in rows:
        r.words.sort(key=lambda w: w.x0)
    return rows


MONTHS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}


def month_from_name(name: str) -> int | None:
    """'Setiem.' -> 9, 'Noviem.' -> 11, 'Jun' -> 6. Usa los 3 primeros chars."""
    key = strip_accents(name).strip(". ")[:3]
    return MONTHS.get(key)


def full_year(two_digit: str) -> int:
    return 2000 + int(two_digit)

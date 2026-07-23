"""Modelo comun a todos los bancos + clasificacion de tipo de linea."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal

from .base import strip_accents

# --- Tipos de linea -------------------------------------------------------
# purchase : consumo real en un comercio -> entra en los graficos de gastos
# cost     : lo que cuesta la tarjeta (intereses, IVA, sellos, comisiones,
#            planes de financiacion) -> se totaliza aparte
# payment  : pagos y creditos (SU PAGO, CR.$ PLAN V) -> NO es gasto, se ignora
# balance  : saldos y totales informativos -> solo para reconciliar

KIND_PURCHASE = "purchase"
KIND_COST = "cost"
KIND_PAYMENT = "payment"
KIND_BALANCE = "balance"

_PAYMENT_PATTERNS = [
    r"\bSU PAGO\b",
    r"\bPAGO RECIBIDO\b",
    # CR.$ PLAN V, CR.RG 5617 (devoluciones/creditos). Sin anclar al inicio:
    # clasificamos sobre el texto completo de la fila, que empieza con la fecha.
    r"\bCR\.",
    r"\bDEVOLUCION\b",
    # Pasaje de deuda de dolares a pesos ("TRANSFERENCIA DEUDA ... TC1430,000").
    # No es un consumo: es la misma deuda expresada en otra moneda. Si se
    # contara como compra, inflaria el gasto del mes por cientos de miles.
    r"\bTRANSFERENCIA DEUDA\b",
]

_BALANCE_PATTERNS = [
    r"\bSALDO ANTERIOR\b",
    r"\bSALDO ACTUAL\b",
    r"\bSALDO PENDIENTE\b",
    r"\bPAGO MINIMO\b",
    r"\bSUBTOTAL\b",
    # "Total Consumos de MARIA BE VILLARREAL 883.539,05" es el subtotal por
    # tarjeta. Sumarlo duplicaria todos los consumos de esa tarjeta.
    r"\bTOTAL CONSUMOS\b",
    r"\bTOTAL TITULAR\b",
    r"\bTOTAL\b$",
    # Pie de pagina del ultimo resumen, con el importe a debitar.
    r"\bDEBITAREMOS\b",
]

_COST_PATTERNS = [
    r"\bI\.?V\.?A\.?\b",
    r"\bIMPUESTO\b",
    r"\bSELLOS\b",
    r"\bCOM\.",              # COM.ADM.DE.CUENTA
    r"\bCOMISION\b",
    r"\bINTERES",
    r"\bPLAN V\b",           # VISA PLAN V (financiacion)
    r"\bCARGO\b",
    r"\bSEGURO\b",
    r"\bRENOVACION\b",
    r"\bPERCEPCION\b",
    r"\bR\.?G\.?\s*\d+",     # RG 5617 (percepciones AFIP)
    r"\bADELANTO\b",
    r"\bMANTENIMIENTO\b",
    # Percepciones impositivas: "IIBB PERCEP-BSAS 2,00%( 71212,44)".
    # Son impuestos, no consumos, aunque figuren entre los movimientos.
    r"\bIIBB\b",
    r"\bPERCEP",
]


def classify_kind(description: str) -> str:
    """Decide si una linea es consumo, costo, pago o saldo."""
    d = strip_accents(description)
    for pattern in _BALANCE_PATTERNS:
        if re.search(pattern, d):
            return KIND_BALANCE
    for pattern in _PAYMENT_PATTERNS:
        if re.search(pattern, d):
            return KIND_PAYMENT
    for pattern in _COST_PATTERNS:
        if re.search(pattern, d):
            return KIND_COST
    return KIND_PURCHASE


# Marca de cuota en Santander: "C.10/18" = cuota 10 de 18
CUOTA_RE = re.compile(r"^C\.?\s*(\d{1,2})\s*/\s*(\d{1,2})$", re.IGNORECASE)


def parse_cuota(token: str) -> tuple[int, int] | None:
    m = CUOTA_RE.match(token.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


@dataclass
class Transaction:
    """Un movimiento del resumen.

    `fecha` es la fecha de ORIGEN del consumo (cuando compraste).
    `periodo` es el mes del RESUMEN en el que se cobra (YYYY-MM).
    Para una compra en cuotas ambos difieren: comprar en Set-24 en 18 cuotas
    genera un cargo en el resumen de Jul-25. Los graficos mensuales usan
    `periodo` (vista de flujo de caja: lo que efectivamente pagaste ese mes).
    """

    bank: str
    period: str                  # YYYY-MM del resumen
    date: date | None            # fecha de origen del consumo
    description: str
    amount: Decimal
    currency: str                # ARS | USD
    kind: str
    cuota_nro: int | None = None
    cuota_total: int | None = None
    receipt: str | None = None   # nro de comprobante/cupon
    category: str | None = None
    cardholder: str | None = None   # titular o adicional que hizo el consumo
    source_file: str = ""
    page: int = 0

    def key(self) -> tuple:
        """Clave para deduplicar si un mismo resumen se carga dos veces."""
        return (
            self.bank,
            self.period,
            self.date.isoformat() if self.date else "",
            self.description,
            str(self.amount),
            self.currency,
            self.receipt or "",
        )

    def as_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat() if self.date else None
        d["amount"] = str(self.amount)
        return d


@dataclass
class Statement:
    """Un resumen completo: sus movimientos + los totales que declara el PDF.

    Guardamos los totales declarados para poder RECONCILIAR: si la suma de lo
    que parseamos no coincide con lo que el PDF dice, el parseo esta mal y hay
    que avisar en vez de mostrar numeros silenciosamente incorrectos.
    """

    bank: str
    period: str
    source_file: str
    transactions: list[Transaction]
    stated_totals: dict[str, Decimal]   # p.ej. {"SALDO ACTUAL ARS": Decimal}
    close_date: date | None = None
    due_date: date | None = None

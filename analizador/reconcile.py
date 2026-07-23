"""Verificacion de que lo parseado coincide con lo que declara el PDF.

Un parser de PDFs puede fallar en silencio: leer un importe de la columna
equivocada, saltear una fila o contar dos veces un subtotal. Los numeros
igual "se ven bien". La unica defensa real es comparar contra los totales
que el propio resumen imprime.

Cada resumen declara sus totales (SALDO ACTUAL, TOTAL CONSUMOS, Total
Consumos por tarjeta). Si la suma de lo que extrajimos no coincide, algo
esta mal y hay que avisar en vez de mostrar un grafico incorrecto.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from parsers.model import KIND_COST, KIND_PURCHASE, Statement

TOLERANCE = Decimal("0.05")   # centavos de redondeo


@dataclass
class Check:
    label: str
    expected: Decimal
    actual: Decimal

    @property
    def diff(self) -> Decimal:
        return self.actual - self.expected

    @property
    def ok(self) -> bool:
        return abs(self.diff) <= TOLERANCE


def _sum(statement: Statement, currency: str, kinds: tuple[str, ...],
         cardholder: str | None = None) -> Decimal:
    total = Decimal("0")
    for t in statement.transactions:
        if t.currency != currency or t.kind not in kinds:
            continue
        if cardholder is not None and t.cardholder != cardholder:
            continue
        total += t.amount
    return total


def check_statement(statement: Statement) -> list[Check]:
    """Compara los totales declarados en el PDF contra los calculados."""
    checks: list[Check] = []
    stated = statement.stated_totals

    for currency in ("ARS", "USD"):
        # BNA: "TOTAL CONSUMOS DEL MES" = solo los consumos del periodo.
        # Ojo: excluye los consumos en cuotas de meses anteriores, asi que
        # solo aplica cuando el resumen no arrastra cuotas.
        key = f"TOTAL TITULAR {currency}"
        if key in stated:
            checks.append(Check(
                label=f"BNA total titular {currency}",
                expected=stated[key],
                actual=_sum(statement, currency, (KIND_PURCHASE,)),
            ))

        # Santander: un subtotal por tarjeta, que incluye consumos y costos
        # (planes de financiacion) de esa tarjeta.
        for skey, value in stated.items():
            if skey.startswith("Total Consumos ") and skey.endswith(currency):
                holder = skey[len("Total Consumos "):-len(currency)].strip()
                checks.append(Check(
                    label=f"Santander {holder} {currency}",
                    expected=value,
                    actual=_sum(statement, currency,
                                (KIND_PURCHASE, KIND_COST), cardholder=holder),
                ))

    return checks


def format_report(statement: Statement, checks: list[Check]) -> list[str]:
    lines = []
    for c in checks:
        mark = "OK " if c.ok else "MAL"
        lines.append(
            f"  [{mark}] {c.label:<34} declarado={c.expected:>14,.2f} "
            f"calculado={c.actual:>14,.2f} dif={c.diff:>12,.2f}"
        )
    return lines

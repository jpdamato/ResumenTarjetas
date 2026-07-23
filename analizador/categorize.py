"""Normalizacion de comercios y asignacion de categorias."""

from __future__ import annotations

import json
import re
from pathlib import Path

from parsers.base import strip_accents

RULES_FILE = Path(__file__).with_name("categories.json")

# Pasarelas de pago que anteponen su marca al nombre real del comercio:
# "MERPAGO*MERCADOLIBRE" -> "MERCADOLIBRE", "GMRA - SAMSUNG.COM" -> "SAMSUNG.COM".
# Sin esto, todo Mercado Pago quedaria agrupado como un unico "comercio".
GATEWAYS = [
    r"^MERPAGO\s*\*", r"^MP\s*\*", r"^DLOCAL\s*\*", r"^DLO\s*\*",
    r"^NSS\s*\*", r"^GETNET\s*\*", r"^PAYU\s*\*\s*AR\s*\*", r"^PAYU\s*\*",
    r"^GMRA\s*-\s*", r"^DINERS\s*\*", r"^RAPIPAGO\s*\*", r"^PAGOFACIL\s*\*",
]
GATEWAY_RE = re.compile("|".join(GATEWAYS))


def normalize_merchant(description: str) -> str:
    """Nombre de comercio limpio, para agrupar consumos equivalentes."""
    name = strip_accents(description)
    name = GATEWAY_RE.sub("", name).strip()
    name = re.sub(r"\s+C\.\d{1,2}/\d{1,2}\b", "", name)   # marca de cuota
    name = re.sub(r"\s{2,}", " ", name).strip(" *-")
    return name or strip_accents(description)


class Categorizer:
    def __init__(self, rules_file: Path | str = RULES_FILE):
        data = json.loads(Path(rules_file).read_text(encoding="utf-8"))
        self.default = data.get("categoria_por_defecto", "Otros")
        self.rules: list[tuple[str, re.Pattern]] = []
        for rule in data["reglas"]:
            combined = "|".join(f"(?:{p})" for p in rule["patrones"])
            self.rules.append((rule["categoria"], re.compile(combined)))

    def categorias(self) -> list[str]:
        """Todas las categorias definidas en las reglas, mas la de descarte."""
        return [c for c, _ in self.rules] + [self.default]

    def categorize(self, description: str) -> str:
        """Primera regla que coincide; el orden del archivo define prioridad."""
        text = strip_accents(description)
        for category, pattern in self.rules:
            if pattern.search(text):
                return category
        return self.default

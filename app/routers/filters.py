# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import get_settings


def date_range(column, date_from: date | None, date_to: date | None) -> list:
    """Filtro por dia no fuso local da operação (date_to inclusivo)."""
    tz = ZoneInfo(get_settings().timezone)
    conds = []
    if date_from:
        conds.append(column >= datetime.combine(date_from, time.min, tz))
    if date_to:
        conds.append(column < datetime.combine(date_to + timedelta(days=1), time.min, tz))
    return conds

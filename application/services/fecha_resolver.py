# application/services/fecha_resolver.py
"""Resuelve la FECHA real de un parte de forma DETERMINISTA (sv3).

La IA suele fallar el anio (lee 2016 por 2026) o la conversion a ISO. Por
eso aqui NO nos fiamos de la IA: tomamos el DIA (lo mas fiable del parte)
y resolvemos mes/anio con dos reglas:

  - El formato del parte es dd/mm/aaaa (o dd/mm/aa). Los partes son SIEMPRE
    de 2026 en adelante: cualquier anio < 2026 se corrige a 2026.

  - Si el EMAIL nombra el mes en texto (p.ej. "abril"), ese mes MANDA
    sobre la fecha leida y se toma como MES NATURAL: la fecha es ese mes +
    el DIA del parte (abril + dia 20 = 20/04). El periodo de nomina 16->15
    es solo para la vista mensual del portal, no para fechar el parte.

Prioridad: si hay mes en el email, manda (mes nombrado + dia del parte).
Si no, se usa la fecha literal del parte (con el anio forzado a 2026+).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_MIN_YEAR = 2026

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}

_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_YMD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DMY_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


@dataclass(frozen=True)
class FechaResuelta:
    iso: str | None
    fecha_int: int | None
    method: str   # email_mes | parte_fecha | parte_fecha_anio_corregido | none


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _norm_text(text: str | None) -> str:
    if not text:
        return ""
    return _strip_accents(str(text)).lower()


def parse_month_year_from_text(text: str | None) -> tuple[int | None, int | None]:
    """Detecta (mes 1..12, anio 20xx|None) a partir de un texto de email."""
    t = _norm_text(text)
    if not t:
        return (None, None)
    month: int | None = None
    for name, num in _MESES.items():
        if re.search(rf"\b{name}\b", t):
            month = num
            break
    year: int | None = None
    m = _YEAR_RE.search(t)
    if m:
        year = int(m.group(1))
    return (month, year)


def _parse_dmy(raw: str | None) -> tuple[int | None, int | None, int | None]:
    """Devuelve (dia, mes, anio) de un texto de fecha. Acepta dd/mm/aaaa,
    dd/mm/aa, ISO y YYYYMMDD. anio de 2 digitos -> 20aa."""
    if not raw:
        return (None, None, None)
    s = str(raw).strip()

    m = _ISO_RE.search(s)
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = _YMD_RE.match(s)
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = _DMY_RE.search(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return (d, mo, y)

    # Mes EN LETRA: "6-abril-26", "6 de abril de 2026", "6 abril 26", etc.
    t = _norm_text(s)
    t = re.sub(r"\bde\b", " ", t)                       # quita 'de'
    mt = re.search(r"(\d{1,2})[\s./-]+([a-zñ]+)[\s./-]+(\d{2,4})", t)
    if mt:
        name = mt.group(2)
        mo = _MESES.get(name)
        if mo:
            d, y = int(mt.group(1)), int(mt.group(3))
            if y < 100:
                y += 2000
            return (d, mo, y)

    return (None, None, None)


def _valid(year: int, month: int, day: int) -> bool:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    dim = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
           else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return day <= dim[month - 1]


def _build(year: int, month: int, day: int, method: str) -> FechaResuelta:
    if not _valid(year, month, day):
        return FechaResuelta(None, None, "none")
    return FechaResuelta(
        iso=f"{year:04d}-{month:02d}-{day:02d}",
        fecha_int=year * 10000 + month * 100 + day,
        method=method,
    )


class FechaParteResolver:
    def __init__(self, *, min_year: int = DEFAULT_MIN_YEAR) -> None:
        self._min_year = int(min_year)

    def resolve(
        self,
        *,
        raw_fecha: str | None,
        email_text: str | None = None,
    ) -> FechaResuelta:
        day, month, year = _parse_dmy(raw_fecha)
        em_month, em_year = parse_month_year_from_text(email_text)

        # (A) El email nombra el mes: ese mes manda sobre la fecha leida, y se
        # toma como MES NATURAL (abril = dia tal cual en abril), NO como
        # periodo 16->15. El periodo 16->15 es solo para la vista del portal.
        if em_month is not None and day is not None:
            base_year = em_year or (
                year if (year and year >= self._min_year) else self._min_year
            )
            if base_year < self._min_year:
                base_year = self._min_year
            res = _build(base_year, em_month, day, "email_mes")
            if res.iso:
                logger.info(
                    "[fecha-resolver] email mes natural=%s -> %s (dia=%s)",
                    em_month, res.iso, day,
                )
                return res

        # (B) Fecha literal del parte, anio forzado a 2026+.
        if day is not None and month is not None:
            ry = year if year else (em_year or self._min_year)
            method = "parte_fecha"
            if ry < self._min_year:
                ry = self._min_year
                method = "parte_fecha_anio_corregido"
            res = _build(ry, month, day, method)
            if res.iso:
                return res

        logger.warning(
            "[fecha-resolver] no se pudo resolver fecha raw=%r email_mes=%s",
            raw_fecha, em_month,
        )
        return FechaResuelta(None, None, "none")

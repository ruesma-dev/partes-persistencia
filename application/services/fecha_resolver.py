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

import datetime as _dt
import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Suelo ABSOLUTO solo para datos corruptos (anios disparatados). El anio
# real se confia de la IA si es plausible o se infiere respecto a hoy.
DEFAULT_MIN_YEAR = 2000

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
    method: str   # leido | leido_pasado | futuro_corregido | inferido | none (+ '+email_mes')


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
    # La IA a veces escribe la fecha con espacios alrededor de los
    # separadores ("1 / 8 /2024"). Se quitan para que los patrones casen.
    s = re.sub(r"\s*([/\-.])\s*", r"\1", s)

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
        today: "_dt.date | None" = None,
    ) -> FechaResuelta:
        today = today or _dt.date.today()
        day, month, year = _parse_dmy(raw_fecha)
        em_month, em_year = parse_month_year_from_text(email_text)

        # El DIA es lo mas fiable del parte. El MES: si el email lo nombra,
        # manda (mes natural) sobre el leido; si no, el del parte.
        eff_month = em_month or month
        if day is None or eff_month is None:
            logger.warning(
                "[fecha-resolver] sin dia/mes raw=%r email_mes=%s",
                raw_fecha, em_month,
            )
            return FechaResuelta(None, None, "none")

        # ANIO: se CONFIA en el leido (email o parte) si forma una fecha
        # VALIDA en el rango [min_year .. hoy]. Casos especiales:
        #
        #   - Anio FUTURO (> hoy): un parte no puede ser del futuro. Se
        #     corrige al anio en curso (misma ocurrencia del mes/dia), pero
        #     se registra como ERROR: casi siempre es un error de fecha del
        #     parte que hay que revisar.
        #   - Anio PASADO (< hoy pero valido y >= min_year): se RESPETA tal
        #     cual (el parte puede ser de un cierre anterior); solo se avisa
        #     con un WARNING para que quede rastro.
        #   - Sin anio / anio corrupto (< min_year) / fecha invalida: se
        #     infiere la ocurrencia mas reciente del mes (mes posterior al de
        #     hoy => anio anterior).
        eff_year: int | None = None
        method = "leido"

        # 1) Anio leido valido y NO futuro -> se confia (respetando pasados).
        for cand in (em_year, year):
            if (cand and self._min_year <= cand <= today.year
                    and _valid(cand, eff_month, day)):
                eff_year = cand
                if cand < today.year:
                    method = "leido_pasado"
                    logger.warning(
                        "[fecha-resolver] anio PASADO respetado: %s "
                        "(raw=%r email_mes=%s). Se mantiene tal cual.",
                        cand, raw_fecha, em_year,
                    )
                break

        # 2) Anio FUTURO plausible leido -> corregir a hoy, pero ERROR.
        if eff_year is None:
            for cand in (em_year, year):
                if (cand and cand > today.year
                        and _valid(today.year, eff_month, day)):
                    eff_year = today.year
                    method = "futuro_corregido"
                    logger.error(
                        "[fecha-resolver] anio FUTURO %s corregido a %s "
                        "(raw=%r email_mes=%s). REVISAR: un parte no puede "
                        "ser del futuro.",
                        cand, today.year, raw_fecha, em_year,
                    )
                    break

        # 3) Sin anio utilizable -> inferir la ocurrencia mas reciente.
        if eff_year is None:
            eff_year = today.year if eff_month <= today.month else today.year - 1
            method = "inferido"

        if em_month is not None and em_month != month:
            method = method + "+email_mes"

        res = _build(eff_year, eff_month, day, method)
        if res.iso:
            logger.info(
                "[fecha-resolver] %s (raw=%r email_mes=%s anio_leido=%s "
                "method=%s)",
                res.iso, raw_fecha, em_month, year, res.method,
            )
            return res

        logger.warning(
            "[fecha-resolver] fecha invalida d=%s m=%s y=%s raw=%r",
            day, eff_month, eff_year, raw_fecha,
        )
        return FechaResuelta(None, None, "none")

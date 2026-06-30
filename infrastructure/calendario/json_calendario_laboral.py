# infrastructure/calendario/json_calendario_laboral.py
"""Adaptador del calendario laboral leido de un JSON local.

Es la implementacion "de mientras" del ``CalendarioLaboralPort``:

  - El FIN DE SEMANA (sabado/domingo) lo calcula de la propia fecha.
  - Los FESTIVOS NACIONALES de Espana se aplican POR DEFECTO sin tener que
    listarlos (fijos + Viernes Santo, calculado por ano).
  - Los festivos del JSON son dias "a mayores" (autonomicos / locales /
    convenio) que se suman a lo anterior.

El dia que llegue Sesame (que tiene API), se escribe un
``SesameCalendarioLaboral`` con el mismo puerto y se cambia solo el wiring.

Estructura del JSON (``config/calendario_laboral.json``)::

    {
      "sabado_no_laborable": true,
      "domingo_no_laborable": true,
      "festivos_nacionales": true,
      "festivos": [
        {"fecha": "2024-02-28", "descripcion": "Dia de Andalucia"},
        "2024-12-24"
      ],
      "festivos_por_localizacion": { "sevilla": ["2024-08-05"] },
      "festivos_por_convenio":     { "construccion_sevilla": ["..."] }
    }

De momento se usan ``festivos`` (globales) + fin de semana + nacionales. Las
secciones por localizacion/convenio se consultan solo si se pasan esos
argumentos, para dejar hecho el camino hacia Sesame.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from domain.ports.calendario_laboral_port import CalendarioLaboralPort

logger = logging.getLogger(__name__)


class JsonCalendarioLaboral(CalendarioLaboralPort):
    def __init__(self, *, path: str | Path) -> None:
        self._path = Path(path)
        self._sabado = True
        self._domingo = True
        self._festivos_nacionales = True
        self._festivos: set[str] = set()
        self._por_loc: dict[str, set[str]] = {}
        self._por_conv: dict[str, set[str]] = {}
        self._nac_cache: dict[int, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            logger.warning(
                "[calendario] no existe %s; se aplicaran fin de semana y "
                "festivos nacionales por defecto.",
                self._path,
            )
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[calendario] error leyendo %s: %r; se usan fin de semana y "
                "nacionales por defecto.",
                self._path, exc,
            )
            return
        if not isinstance(raw, dict):
            logger.warning(
                "[calendario] %s no es un objeto JSON; se ignora.", self._path
            )
            return
        self._sabado = bool(raw.get("sabado_no_laborable", True))
        self._domingo = bool(raw.get("domingo_no_laborable", True))
        self._festivos_nacionales = bool(raw.get("festivos_nacionales", True))
        self._festivos = self._parse_fechas(raw.get("festivos"))
        self._por_loc = {
            str(k).strip().lower(): self._parse_fechas(v)
            for k, v in (raw.get("festivos_por_localizacion") or {}).items()
        }
        self._por_conv = {
            str(k).strip().lower(): self._parse_fechas(v)
            for k, v in (raw.get("festivos_por_convenio") or {}).items()
        }
        logger.info(
            "[calendario] nacionales=%s, %s festivos extra, %s localizaciones, "
            "%s convenios (sabado=%s domingo=%s).",
            self._festivos_nacionales, len(self._festivos),
            len(self._por_loc), len(self._por_conv),
            self._sabado, self._domingo,
        )

    @staticmethod
    def _parse_fechas(value: object) -> set[str]:
        """Acepta una lista de strings 'YYYY-MM-DD' o de objetos con 'fecha'."""
        out: set[str] = set()
        if not isinstance(value, list):
            return out
        for item in value:
            if isinstance(item, str):
                f = item.strip()
            elif isinstance(item, dict):
                f = str(item.get("fecha") or "").strip()
            else:
                f = ""
            if f:
                out.add(f)
        return out

    # ----- festivos nacionales de Espana (fijos + Viernes Santo) ----- #
    @staticmethod
    def _domingo_pascua(year: int) -> _dt.date:
        """Domingo de Pascua (algoritmo de Computus gregoriano)."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * ll) // 451
        month = (h + ll - 7 * m + 114) // 31
        day = ((h + ll - 7 * m + 114) % 31) + 1
        return _dt.date(year, month, day)

    def _nacionales(self, year: int) -> set[str]:
        cached = self._nac_cache.get(year)
        if cached is not None:
            return cached
        fijos = {
            f"{year:04d}-01-01",  # Ano Nuevo
            f"{year:04d}-01-06",  # Reyes
            f"{year:04d}-05-01",  # Dia del Trabajo
            f"{year:04d}-08-15",  # Asuncion
            f"{year:04d}-10-12",  # Fiesta Nacional
            f"{year:04d}-11-01",  # Todos los Santos
            f"{year:04d}-12-06",  # Constitucion
            f"{year:04d}-12-08",  # Inmaculada
            f"{year:04d}-12-25",  # Navidad
        }
        # Viernes Santo (movil, nacional en toda Espana).
        viernes_santo = self._domingo_pascua(year) - _dt.timedelta(days=2)
        fijos.add(viernes_santo.isoformat())
        self._nac_cache[year] = fijos
        return fijos

    def es_no_laborable(
        self,
        fecha_iso: str,
        *,
        dni: str | None = None,
        localizacion: str | None = None,
        convenio: str | None = None,
    ) -> bool:
        # 1) Fin de semana (determinista, de la propia fecha).
        try:
            d = _dt.date.fromisoformat(fecha_iso)
        except (TypeError, ValueError):
            return False
        wd = d.weekday()  # 0=lunes ... 5=sabado, 6=domingo
        if wd == 5 and self._sabado:
            return True
        if wd == 6 and self._domingo:
            return True
        # 2) Festivos nacionales (por defecto, sin listarlos).
        if self._festivos_nacionales and fecha_iso in self._nacionales(d.year):
            return True
        # 3) Festivos del JSON (a mayores: autonomicos / locales).
        if fecha_iso in self._festivos:
            return True
        # 4) Festivos por localizacion / convenio (preparado para Sesame).
        if localizacion and fecha_iso in self._por_loc.get(
            localizacion.strip().lower(), set()
        ):
            return True
        if convenio and fecha_iso in self._por_conv.get(
            convenio.strip().lower(), set()
        ):
            return True
        return False

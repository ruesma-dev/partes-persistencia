# domain/ports/calendario_laboral_port.py
"""Puerto del CALENDARIO LABORAL.

Decide si una fecha es NO laborable (fin de semana o festivo). En fin de
semana/festivo no hay horas ordinarias: el reparto las manda todas a extra.

La firma admite ``dni`` / ``localizacion`` / ``convenio`` pensando en el dia
que la fuente sea Sesame (festivos por convenio y centro, y trabajadores a los
que aplica cada uno). La implementacion JSON actual solo usa la fecha (fin de
semana + festivos globales); el resto de argumentos quedan disponibles para el
adaptador de Sesame sin tener que cambiar ni la regla ni los llamantes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CalendarioLaboralPort(ABC):
    @abstractmethod
    def es_no_laborable(
        self,
        fecha_iso: str,
        *,
        dni: str | None = None,
        localizacion: str | None = None,
        convenio: str | None = None,
    ) -> bool:
        """True si ``fecha_iso`` (YYYY-MM-DD) es fin de semana o festivo."""
        raise NotImplementedError

# domain/ports/sigrid_lookup_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.sigrid_models import EmpleadoRow, ObraRow, TipoHoraRow


class SigridLookupPort(Protocol):
    """Lookups de SOLO LECTURA contra Sigrid para el casado del parte."""

    def fetch_empleados(self) -> list[EmpleadoRow]:
        ...

    def fetch_obras(self) -> list[ObraRow]:
        ...

    def fetch_tipos_hora(self) -> list[TipoHoraRow]:
        ...

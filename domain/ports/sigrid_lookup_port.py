# domain/ports/sigrid_lookup_port.py
from __future__ import annotations

from typing import Protocol

from domain.models.sigrid_models import (
    EmpleadoRow, HmoRow, ObraRow, PartidaRow, RecursoRow, ReshorRow,
    TipoHoraRow,
)


class SigridLookupPort(Protocol):
    """Lookups de SOLO LECTURA contra Sigrid para el casado del parte."""

    def fetch_empleados(self) -> list[EmpleadoRow]:
        ...

    def fetch_obras(self) -> list[ObraRow]:
        ...

    def fetch_tipos_hora(self) -> list[TipoHoraRow]:
        ...

    def fetch_partidas_obra(self, obra_ide: int) -> list[PartidaRow]:
        ...

    def fetch_recursos(self) -> list[RecursoRow]:
        ...

    def fetch_reshor(self) -> list[ReshorRow]:
        ...

    def fetch_hmo_obra(self, obra_ide: int) -> list[HmoRow]:
        ...

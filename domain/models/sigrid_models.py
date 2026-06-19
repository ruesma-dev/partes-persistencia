# domain/models/sigrid_models.py
"""Filas de Sigrid relevantes para el casado de un parte de trabajo.

Son DTOs planos (dataclasses) que devuelven los lookups de Sigrid y que
consumen los matchers/resolver. NO contienen logica.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmpleadoRow:
    """Empleado de Sigrid (``emp`` extiende ``con``).

    ``ide`` es ``con.ide`` (= ``emp.ide``). ``codigo`` es ``con.cod``.
    ``reside`` es el Recurso relacionado (``emp.reside`` -> ``res``), que
    es lo que referencian ``hmo.reside`` / ``hmores.reside`` al imputar.
    """

    ide: int
    codigo: str | None
    nombre: str | None       # emp.res (nombre completo)
    dni: str | None          # emp.dni
    reside: int | None       # emp.reside (Recurso)


@dataclass(frozen=True)
class ObraRow:
    ide: int
    codigo: str | None       # con.cod
    nombre: str | None       # obr.res


@dataclass(frozen=True)
class TipoHoraRow:
    """Tipo de hora de Sigrid (``auxhor``). ``ext`` distingue
    normal(0)/extra(1). ``pre``/``prenom`` son precios de coste/nomina.
    """

    ide: int
    codigo: str | None       # auxhor.cod
    descripcion: str | None  # auxhor.res
    ext: int                 # 0 normal | 1 extra
    pre: float | None        # precio coste
    prenom: float | None     # precio nomina

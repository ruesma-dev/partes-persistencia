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


@dataclass(frozen=True)
class PartidaRow:
    """Fila cruda de ``obrparpar`` (linea/capitulo del presupuesto de obra).

    ``padide`` es el capitulo padre (raices ``padide=0``); el arbol y la
    clasificacion CD/CI/CP (por capitulo raiz) y la deteccion de hoja se
    hacen en ``application/services/partida_catalog.py``.
    """

    ide: int
    padide: int | None
    cod: str | None
    res: str | None
    tex: str | None
    tipdes: int
    cosindide: int | None
    unimed: str | None


@dataclass(frozen=True)
class RecursoRow:
    """Recurso de Sigrid (``res`` extiende ``con``). ``cif`` es el DNI/NIF;
    ``conide`` es el empleado asociado (``emp``). Es lo que referencian
    ``hmo.reside`` / ``emp.reside`` al imputar mano de obra.

    ``restipide`` es la CLASIFICACION del recurso (``res.restipide`` ->
    ``auxrestip``); ``restip_cod`` / ``restip_res`` son su codigo y
    descripcion (la CATEGORIA, p.ej. "OFIC.1a GRUISTA"). ``horide_def`` es
    el tipo de hora por defecto del recurso (``res.horide`` -> ``auxhor``),
    que identifica su hora LABORABLE ordinaria.
    """

    ide: int
    cif: str | None          # res.cif (DNI/NIF)
    conide: int | None       # res.conide (empleado asociado)
    restipide: int | None = None   # res.restipide -> auxrestip
    restip_cod: str | None = None  # auxrestip.cod (categoria, codigo)
    restip_res: str | None = None  # auxrestip.res (categoria, descripcion)
    horide_def: int | None = None  # res.horide (tipo de hora por defecto)


@dataclass(frozen=True)
class ReshorRow:
    """Coste de horas de un recurso (``reshor`` x ``auxhor``): por cada
    (recurso, tipo de hora) su codigo/descripcion, el flag extra y la
    cantidad por defecto. Es la pantalla "Costes de horas del recurso".

    ``candef`` (``reshor.candef``) es la CANTIDAD POR DEFECTO; en la hora
    laborable es la jornada por defecto (p.ej. 8) que sirve para contar
    extras.
    """

    reside: int
    horide: int
    cod: str | None          # auxhor.cod
    res: str | None          # auxhor.res
    ext: int                 # 0 normal | 1 extra
    candef: float | None     # reshor.candef (cantidad/jornada por defecto)
    pre: float | None        # reshor.pre (precio coste)


@dataclass(frozen=True)
class HmoRow:
    """Parte de trabajo de Sigrid (``hmo``). Se keyea por recurso + obra +
    ano + mes; las horas del dia estan en ``hmomed`` (no se leen aqui)."""

    ide: int
    reside: int | None       # recurso
    ano: int | None
    mes: int | None

# domain/models/parte_records.py
"""Modelos internos del sv3: el parte diario ya normalizado + casado,
listo para persistir, y el resultado del persist.

Modelo real: un documento = un PARTE DIARIO de una obra (una fecha, un
encargado, una firma) con VARIOS empleados. Cada empleado puede generar
varios REGISTROS (uno de horas normales, otro de extra, y/o uno de
incidencia). El registro lleva la identidad del empleado (leida + casada
contra Sigrid), su categoria y, si aplica, la incidencia.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ------------------------------------------------------------------ #
# Resultado del casado de cada entidad.
# ------------------------------------------------------------------ #
@dataclass
class EmpleadoMatch:
    ide: Optional[int] = None
    codigo: Optional[str] = None
    nombre: Optional[str] = None
    dni: Optional[str] = None
    reside: Optional[int] = None
    score: float = 0.0
    method: str = "none"   # dni | codigo | nombre | none


@dataclass
class ObraMatch:
    ide: Optional[int] = None
    codigo: Optional[str] = None
    nombre: Optional[str] = None
    score: float = 0.0
    method: str = "none"   # codigo | codigo_padded | nombre | none


@dataclass
class TipoHoraMatch:
    ide: Optional[int] = None
    codigo: Optional[str] = None
    descripcion: Optional[str] = None
    ext: Optional[int] = None
    pre: Optional[float] = None
    prenom: Optional[float] = None
    method: str = "none"   # codigo_leido | incidencia | ext_default | ext_first | none


# ------------------------------------------------------------------ #
# Incidencia.
# ------------------------------------------------------------------ #
@dataclass
class IncidenciaInfo:
    codigo: Optional[str] = None       # V|B|AT|FJ|F|H|M
    texto_leido: Optional[str] = None
    dias: Optional[float] = None


# ------------------------------------------------------------------ #
# Registro horario (empleado x tipo de hora dentro del parte).
# ------------------------------------------------------------------ #
@dataclass
class RegistroNormalizado:
    line_index: int                              # indice secuencial
    empleado_line_no: Optional[int] = None       # Nº de fila del parte
    categoria: Optional[str] = None
    trabajador_nombre_leido: Optional[str] = None
    # DNI leido del parte (columna DNI de J.310 rev. 1+); None en rev. 0.
    trabajador_dni_leido: Optional[str] = None
    empleado: EmpleadoMatch = field(default_factory=EmpleadoMatch)

    tipo_hora: Optional[str] = None              # normal | extra | V|B|AT|...
    es_incidencia: bool = False
    incidencia: IncidenciaInfo = field(default_factory=IncidenciaInfo)

    horas: Optional[float] = None
    partida: Optional[str] = None                # reparto por partida (opcional)

    # Codigo de hora de Sigrid PROPUESTO por la IA (del catalogo). El
    # resolver lo prioriza si existe en el maestro auxhor.
    codigo_hora_propuesto: Optional[str] = None

    hora: TipoHoraMatch = field(default_factory=TipoHoraMatch)
    confianza_pct: Optional[float] = None


# ------------------------------------------------------------------ #
# Parte diario normalizado.
# ------------------------------------------------------------------ #
@dataclass
class ParteDocumento:
    # Dia / obra.
    fecha_iso: Optional[str] = None
    fecha_int: Optional[int] = None
    obra_numero_leido: Optional[str] = None
    obra_nombre_leido: Optional[str] = None
    obra: ObraMatch = field(default_factory=ObraMatch)

    encargado_nombre: Optional[str] = None
    jefe_obra_nombre: Optional[str] = None

    # Firma.
    firmado: bool = False
    firma_encargado: bool = False
    firma_jefe_obra: bool = False
    firma_administracion: bool = False
    firmante_rol: Optional[str] = None
    firmante_nombre: Optional[str] = None
    firma_confianza_pct: Optional[float] = None

    # SharePoint (archivado del PDF). None si no se subio.
    sharepoint_url: Optional[str] = None
    sharepoint_item_id: Optional[str] = None
    sharepoint_drive_id: Optional[str] = None

    registros: list[RegistroNormalizado] = field(default_factory=list)


# ------------------------------------------------------------------ #
# Resultado del persist / dedup.
# ------------------------------------------------------------------ #
@dataclass
class PersistParteResult:
    ok: bool
    document_id: str
    already_existed: bool
    fecha: Optional[str]
    obra_codigo: Optional[str]
    empleados_distintos: int
    registros_persistidos: int
    registros_con_hora: int
    firmado: bool


@dataclass(frozen=True)
class ExistingParte:
    document_id: str
    source_sha256: str
    registros: int

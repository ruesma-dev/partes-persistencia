# infrastructure/database/orm_models.py
"""ORM de partes de trabajo (SQLAlchemy 2.0).

Modelo real (un documento = un parte DIARIO de una obra, con varios
empleados):

  - ``parte_documents``: cabecera del parte diario (fecha, obra leida +
    casada, encargado, jefe de obra, FIRMA) + metadatos de email/IA +
    estado de revision. Soft-delete.
  - ``parte_registros``: una fila por (empleado x tipo de hora) del parte:
    horas normales, extra o una incidencia. Cada registro lleva la
    IDENTIDAD del empleado (leida + casada contra Sigrid ``emp``), su
    categoria, y el CODIGO DE HORA de Sigrid (``auxhor``) resuelto. La
    fecha y la obra se desnormalizan desde el documento para agregar por
    trabajador sin joins.

La unicidad por ``source_sha256`` es un INDICE UNICO PARCIAL
``WHERE is_active`` (creado en el repositorio): un parte borrado no ocupa
el slot y el mismo PDF puede reingerirse.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class ParteDocumentOrm(Base):
    __tablename__ = "parte_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # --- Origen del documento --- #
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)

    # --- Procedencia IA --- #
    provider: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_key: Mapped[str | None] = mapped_column(String(100))
    schema_name: Mapped[str | None] = mapped_column(String(100))

    # --- Dia del parte --- #
    fecha: Mapped[str | None] = mapped_column(String(16))    # ISO
    fecha_int: Mapped[int | None] = mapped_column(Integer)   # YYYYMMDD

    # --- Obra LEIDA + CASADA --- #
    obra_numero_leido: Mapped[str | None] = mapped_column(String(64))
    obra_nombre_leido: Mapped[str | None] = mapped_column(String(255))
    obra_ide: Mapped[int | None] = mapped_column(Integer)
    obra_codigo: Mapped[str | None] = mapped_column(String(64))
    obra_nombre: Mapped[str | None] = mapped_column(String(255))
    obra_match_score: Mapped[float | None] = mapped_column(Float)
    obra_match_method: Mapped[str | None] = mapped_column(String(24))

    # --- Responsables --- #
    encargado_nombre: Mapped[str | None] = mapped_column(String(255))
    jefe_obra_nombre: Mapped[str | None] = mapped_column(String(255))

    # --- Firma --- #
    firmado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    firma_encargado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    firma_jefe_obra: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    firma_administracion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    firmante_rol: Mapped[str | None] = mapped_column(String(64))
    firmante_nombre: Mapped[str | None] = mapped_column(String(255))
    firma_confianza_pct: Mapped[float | None] = mapped_column(Float)

    # --- SharePoint (archivado del PDF) --- #
    sharepoint_url: Mapped[str | None] = mapped_column(Text)
    sharepoint_item_id: Mapped[str | None] = mapped_column(String(255))
    sharepoint_drive_id: Mapped[str | None] = mapped_column(String(255))

    # --- Email --- #
    email_id: Mapped[str | None] = mapped_column(String(255))
    email_subject: Mapped[str | None] = mapped_column(String(512))
    email_sender: Mapped[str | None] = mapped_column(String(255))
    email_received_datetime: Mapped[str | None] = mapped_column(String(64))
    source_attachment_filename: Mapped[str | None] = mapped_column(String(255))
    source_attachment_sha256: Mapped[str | None] = mapped_column(String(64))

    # --- Revision / estado --- #
    review_required: Mapped[bool | None] = mapped_column(Boolean)
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at_utc: Mapped[str | None] = mapped_column(String(64))

    # --- Soft-delete --- #
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    deleted_at_utc: Mapped[str | None] = mapped_column(String(64))
    deleted_by: Mapped[str | None] = mapped_column(String(255))

    # --- Trazabilidad --- #
    raw_extraction_json: Mapped[str | None] = mapped_column(Text)
    raw_context_json: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)

    registros: Mapped[list["ParteRegistroOrm"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class ParteRegistroOrm(Base):
    __tablename__ = "parte_registros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("parte_documents.id"), nullable=False, index=True
    )
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    empleado_line_no: Mapped[int | None] = mapped_column(Integer)

    # --- Empleado LEIDO --- #
    categoria: Mapped[str | None] = mapped_column(String(64))
    trabajador_nombre_leido: Mapped[str | None] = mapped_column(String(255))

    # --- Empleado CASADO (Sigrid emp) --- #
    empleado_ide: Mapped[int | None] = mapped_column(Integer, index=True)
    empleado_codigo: Mapped[str | None] = mapped_column(String(64))
    empleado_nombre: Mapped[str | None] = mapped_column(String(255))
    empleado_dni: Mapped[str | None] = mapped_column(String(64))
    empleado_reside: Mapped[int | None] = mapped_column(Integer)
    empleado_match_score: Mapped[float | None] = mapped_column(Float)
    empleado_match_method: Mapped[str | None] = mapped_column(String(24))

    # --- Dia / obra (desnormalizado del documento) --- #
    fecha: Mapped[str | None] = mapped_column(String(16))    # ISO
    fecha_int: Mapped[int | None] = mapped_column(Integer)
    obra_codigo: Mapped[str | None] = mapped_column(String(64))
    obra_nombre: Mapped[str | None] = mapped_column(String(255))
    obra_ide: Mapped[int | None] = mapped_column(Integer)

    # --- Tipo de registro --- #
    tipo_hora: Mapped[str | None] = mapped_column(String(16))  # normal|extra|V|B|...

    # --- Borrado a nivel LINEA (soft delete -> papelera de sv4). --- #
    # NULL = activo. Lo escribe sv4; sv3 SOLO LO LEE para EXCLUIR estas
    # lineas de las conciliaciones (partida, recurso y reparto de jornada).
    deleted_at_utc: Mapped[str | None] = mapped_column(String(64), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(255))

    es_incidencia: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    incidencia_codigo: Mapped[str | None] = mapped_column(String(8))
    incidencia_texto: Mapped[str | None] = mapped_column(String(255))
    incidencia_dias: Mapped[float | None] = mapped_column(Float)

    horas: Mapped[float | None] = mapped_column(Float)
    partida: Mapped[str | None] = mapped_column(String(128))

    # --- PARTIDA CASADA contra el presupuesto (Sigrid obrparpar) --- #
    # La escribe la conciliacion automatica de sv3 al persistir (no el front).
    # partida_ide/cod/res identifican la partida; partida_capitulo es CD/CI/CP
    # del capitulo raiz; metodo: auto_nombre|auto_categoria|manual|sin.
    partida_ide: Mapped[int | None] = mapped_column(Integer)
    partida_cod: Mapped[str | None] = mapped_column(String(64))
    partida_res: Mapped[str | None] = mapped_column(String(255))
    partida_capitulo: Mapped[str | None] = mapped_column(String(8))
    partida_match_method: Mapped[str | None] = mapped_column(String(24))
    partida_match_score: Mapped[float | None] = mapped_column(Float)

    # --- RECURSO / PARTE DE TRABAJO casado (Sigrid res + hmo) --- #
    # Lo escribe la conciliacion automatica de sv3. recurso_ide = res.ide
    # (recurso del trabajador, via emp.reside o por DNI); recurso_cif = DNI;
    # hmo_ide = parte de trabajo localizado (reside+obra+ano+mes) o NULL;
    # parte_estado: ok | sin_recurso | sin_parte.
    recurso_ide: Mapped[int | None] = mapped_column(Integer)
    recurso_cif: Mapped[str | None] = mapped_column(String(64))
    hmo_ide: Mapped[int | None] = mapped_column(Integer)
    parte_estado: Mapped[str | None] = mapped_column(String(16))

    # --- CODIGO DE HORA resuelto (Sigrid auxhor) --- #
    hora_ide: Mapped[int | None] = mapped_column(Integer)
    hora_codigo: Mapped[str | None] = mapped_column(String(64))
    hora_descripcion: Mapped[str | None] = mapped_column(String(128))
    hora_ext: Mapped[int | None] = mapped_column(Integer)   # 0 normal|1 extra
    hora_precio_coste: Mapped[float | None] = mapped_column(Float)
    hora_precio_nomina: Mapped[float | None] = mapped_column(Float)
    # Cantidad por defecto (CanDefecto) de la hora laborable del recurso =
    # jornada por defecto. La escribe la conciliacion de recurso (sv3) al
    # casar; sirve para contabilizar las horas extra.
    hora_candef: Mapped[float | None] = mapped_column(Float)
    # Precio/valor BASE de la hora laborable del recurso en Sigrid
    # (reshor.pre de su hora por defecto). Lo escribe la conciliacion de
    # recurso; es el coste hora asignado al trabajador.
    recurso_precio_hora: Mapped[float | None] = mapped_column(Float)
    # Horas ORIGINALES del registro normal antes de recortar parte del dia a
    # extra (para poder revertir y recalcular de forma idempotente). NULL si
    # el registro no ha sido recortado.
    horas_orig: Mapped[float | None] = mapped_column(Float)
    # Marca un registro EXTRA generado automaticamente por el calculo de
    # exceso de jornada (no venia en el parte). Se borra y recrea en cada
    # conciliacion. Los extra EXPLICITOS del parte tienen extra_auto=False.
    extra_auto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    hora_match_method: Mapped[str | None] = mapped_column(String(24))

    confianza_pct: Mapped[float | None] = mapped_column(Float)

    document: Mapped[ParteDocumentOrm] = relationship(
        back_populates="registros"
    )


class EmpleadoAliasOrm(Base):
    """Alias aprendido: nombre LEIDO normalizado -> empleado de Sigrid.

    Lo escribe la conciliacion (sv4) al confirmar un casado manual; lo lee
    la INGESTA (sv3) antes de la similitud para casar de forma exacta las
    variantes recurrentes de cada trabajador (OCR / caligrafia)."""
    __tablename__ = "empleado_alias"

    nombre_norm: Mapped[str] = mapped_column(String(300), primary_key=True)
    empleado_ide: Mapped[int] = mapped_column(Integer, nullable=False)
    empleado_codigo: Mapped[str | None] = mapped_column(String(60), nullable=True)
    empleado_nombre: Mapped[str | None] = mapped_column(Text, nullable=True)
    empleado_dni: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at_utc: Mapped[str] = mapped_column(String(40), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

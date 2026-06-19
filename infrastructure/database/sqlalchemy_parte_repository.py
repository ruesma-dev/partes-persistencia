# infrastructure/database/sqlalchemy_parte_repository.py
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text

from domain.models.parte_records import ExistingParte, ParteDocumento
from infrastructure.database.orm_models import (
    Base,
    EmpleadoAliasOrm,
    ParteDocumentOrm,
    ParteRegistroOrm,
)
from infrastructure.database.session_factory import SessionFactory
from application.services import text_match as tm

logger = logging.getLogger(__name__)

_DDL_PARTIAL_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_parte_documents_sha256_active "
    "ON parte_documents (source_sha256) WHERE is_active"
)

# Columnas anadidas despues del esquema inicial. ALTER defensivo por si la
# tabla ya existe (create_all no anade columnas a tablas existentes).
_DDL_ALTERS = (
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "firma_encargado BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "firma_jefe_obra BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "firma_administracion BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "sharepoint_url TEXT",
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "sharepoint_item_id VARCHAR(255)",
    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
    "sharepoint_drive_id VARCHAR(255)",
)


class SqlAlchemyParteRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def initialize(self) -> None:
        engine = self._session_factory.engine
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text(_DDL_PARTIAL_UNIQUE))
            for ddl in _DDL_ALTERS:
                connection.execute(text(ddl))
        logger.info("[parte-repo] esquema inicializado.")

    def find_empleado_alias(self, nombre_leido: str | None) -> dict | None:
        """Busca un alias aprendido (nombre LEIDO -> empleado). Devuelve los
        datos del empleado o None. Usado en ingesta antes de la similitud."""
        norm = tm.normalize(nombre_leido)
        if not norm:
            return None
        with self._session_factory.create_session() as session:
            row = session.get(EmpleadoAliasOrm, norm)
            if row is None:
                return None
            return {
                "ide": row.empleado_ide,
                "codigo": row.empleado_codigo,
                "nombre": row.empleado_nombre,
                "dni": row.empleado_dni,
            }

    def get_by_sha256(self, source_sha256: str) -> ExistingParte | None:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteDocumentOrm)
                .where(ParteDocumentOrm.source_sha256 == source_sha256)
                .where(ParteDocumentOrm.is_active.is_(True))
                .limit(1)
            )
            doc = session.execute(stmt).scalar_one_or_none()
            if doc is None:
                return None
            return ExistingParte(
                document_id=doc.id,
                source_sha256=doc.source_sha256,
                registros=len(doc.registros),
            )

    def _deactivate_same_day_obra(
        self, session, parte: ParteDocumento, exclude_id: str
    ) -> list[str]:
        """Desactiva (soft-delete) los partes ACTIVOS del MISMO dia y MISMA
        obra. Asi un PDF distinto del mismo dia/obra 'pisa' lo anterior: las
        vistas filtran por is_active, de modo que las horas viejas desaparecen.

        Identidad de obra: por codigo casado si lo hay; si no, por el numero
        de obra leido (solo entre partes tambien sin casar). Si no hay fecha o
        ningun identificador de obra, NO se desactiva nada (mas seguro)."""
        fint = parte.fecha_int
        if not fint or fint <= 0:
            return []
        cod = ((parte.obra.codigo if parte.obra else None) or "").strip()
        num = (parte.obra_numero_leido or "").strip()
        if not cod and not num:
            return []
        stmt = (
            select(ParteDocumentOrm)
            .where(ParteDocumentOrm.is_active.is_(True))
            .where(ParteDocumentOrm.id != exclude_id)
            .where(ParteDocumentOrm.fecha_int == fint)
        )
        if cod:
            stmt = stmt.where(ParteDocumentOrm.obra_codigo == cod)
        else:
            stmt = stmt.where(
                ParteDocumentOrm.obra_codigo.is_(None),
                ParteDocumentOrm.obra_numero_leido == num,
            )
        ids: list[str] = []
        for old in session.execute(stmt).scalars().all():
            old.is_active = False
            ids.append(old.id)
        return ids

    def save_parte(
        self,
        *,
        document_id: str,
        parte: ParteDocumento,
        meta: dict[str, Any],
        context: dict[str, Any],
        raw_extraction_json: str,
        raw_context_json: str,
        review_required: bool,
    ) -> None:
        email = (context or {}).get("email") or {}
        attachment = (context or {}).get("attachment") or {}
        document = (context or {}).get("document") or {}
        now_iso = datetime.now(timezone.utc).isoformat()
        obra = parte.obra

        with self._session_factory.create_session() as session:
            # Reemplazo: un PDF distinto del mismo dia y obra pisa lo anterior.
            superseded = self._deactivate_same_day_obra(
                session, parte, document_id
            )
            if superseded:
                logger.info(
                    "[parte-repo] reemplazo: desactivados %s parte(s) previos "
                    "del mismo dia/obra (fecha_int=%s obra=%s): %s",
                    len(superseded), parte.fecha_int,
                    (parte.obra.codigo if parte.obra else None)
                    or parte.obra_numero_leido,
                    superseded,
                )
            doc = ParteDocumentOrm(
                id=document_id,
                source_filename=str(
                    document.get("filename")
                    or meta.get("source_filename")
                    or "document.bin"
                ),
                source_mime_type=str(
                    document.get("mime_type")
                    or meta.get("source_mime_type")
                    or "application/octet-stream"
                ),
                source_sha256=str(
                    document.get("sha256") or meta.get("source_sha256") or ""
                ),
                page_number=_as_int(document.get("page_number")),
                page_count=_as_int(document.get("page_count")),
                provider=_as_str(meta.get("provider")),
                model_name=_as_str(meta.get("model")),
                prompt_key=_as_str(meta.get("prompt_key")),
                schema_name=_as_str(meta.get("schema")),
                # Dia.
                fecha=parte.fecha_iso,
                fecha_int=parte.fecha_int,
                # Obra.
                obra_numero_leido=parte.obra_numero_leido,
                obra_nombre_leido=parte.obra_nombre_leido,
                obra_ide=obra.ide,
                obra_codigo=obra.codigo,
                obra_nombre=obra.nombre,
                obra_match_score=obra.score,
                obra_match_method=obra.method,
                # Responsables.
                encargado_nombre=parte.encargado_nombre,
                jefe_obra_nombre=parte.jefe_obra_nombre,
                # Firma.
                firmado=parte.firmado,
                firma_encargado=parte.firma_encargado,
                firma_jefe_obra=parte.firma_jefe_obra,
                firma_administracion=parte.firma_administracion,
                firmante_rol=parte.firmante_rol,
                firmante_nombre=parte.firmante_nombre,
                firma_confianza_pct=parte.firma_confianza_pct,
                # SharePoint.
                sharepoint_url=parte.sharepoint_url,
                sharepoint_item_id=parte.sharepoint_item_id,
                sharepoint_drive_id=parte.sharepoint_drive_id,
                # Email.
                email_id=_as_str(email.get("id")),
                email_subject=_as_str(email.get("subject")),
                email_sender=_as_str(email.get("sender")),
                email_received_datetime=_as_str(email.get("receivedDateTime")),
                source_attachment_filename=_as_str(attachment.get("name")),
                source_attachment_sha256=_as_str(attachment.get("sha256")),
                # Estado.
                review_required=review_required,
                approved=False,
                is_active=True,
                raw_extraction_json=raw_extraction_json,
                raw_context_json=raw_context_json,
                created_at_utc=now_iso,
            )

            for reg in parte.registros:
                emp = reg.empleado
                hora = reg.hora
                doc.registros.append(
                    ParteRegistroOrm(
                        line_index=reg.line_index,
                        empleado_line_no=reg.empleado_line_no,
                        categoria=reg.categoria,
                        trabajador_nombre_leido=reg.trabajador_nombre_leido,
                        empleado_ide=emp.ide,
                        empleado_codigo=emp.codigo,
                        empleado_nombre=emp.nombre,
                        empleado_dni=emp.dni,
                        empleado_reside=emp.reside,
                        empleado_match_score=emp.score,
                        empleado_match_method=emp.method,
                        fecha=parte.fecha_iso,
                        fecha_int=parte.fecha_int,
                        obra_codigo=obra.codigo,
                        obra_nombre=obra.nombre,
                        obra_ide=obra.ide,
                        tipo_hora=reg.tipo_hora,
                        es_incidencia=reg.es_incidencia,
                        incidencia_codigo=reg.incidencia.codigo,
                        incidencia_texto=reg.incidencia.texto_leido,
                        incidencia_dias=reg.incidencia.dias,
                        horas=reg.horas,
                        partida=reg.partida,
                        hora_ide=hora.ide,
                        hora_codigo=hora.codigo,
                        hora_descripcion=hora.descripcion,
                        hora_ext=hora.ext,
                        hora_precio_coste=hora.pre,
                        hora_precio_nomina=hora.prenom,
                        hora_match_method=hora.method,
                        confianza_pct=reg.confianza_pct,
                    )
                )

            session.add(doc)
            session.commit()

        logger.info(
            "[parte-repo] guardado document_id=%s fecha=%s registros=%s",
            document_id, parte.fecha_iso, len(parte.registros),
        )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

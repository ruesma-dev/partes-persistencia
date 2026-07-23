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
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_ide INTEGER",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_cod VARCHAR(64)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_res VARCHAR(255)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_capitulo VARCHAR(8)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_match_method VARCHAR(24)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "partida_match_score DOUBLE PRECISION",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "recurso_ide INTEGER",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "recurso_cif VARCHAR(64)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "hmo_ide INTEGER",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "parte_estado VARCHAR(16)",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "hora_candef DOUBLE PRECISION",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "recurso_precio_hora DOUBLE PRECISION",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "horas_orig DOUBLE PRECISION",
    "ALTER TABLE parte_registros ADD COLUMN IF NOT EXISTS "
    "extra_auto BOOLEAN NOT NULL DEFAULT false",
)


def _norm_txt(s: str | None) -> str:
    return " ".join((s or "").strip().lower().split())


def _norm_dni(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _emp_signals(
    ide, dni, nombre_leido, nombre_emp
) -> set:
    """Senales que identifican a un trabajador: ide casado, DNI normalizado
    y/o nombre normalizado. Dos partes 'son del mismo trabajador' si comparten
    alguna senal."""
    sig: set = set()
    if ide is not None:
        sig.add(("ide", int(ide)))
    d = _norm_dni(dni)
    if d:
        sig.add(("dni", d))
    n = _norm_txt(nombre_leido) or _norm_txt(nombre_emp)
    if n:
        sig.add(("nom", n))
    return sig


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

    # ----------------------------------------------------------------- #
    # Conciliacion de PARTIDAS (la lanza sv3 al persistir; sv4 solo lee).
    # ----------------------------------------------------------------- #
    def fetch_registros_para_partida(self) -> list[dict]:
        """Registros de partes ACTIVOS con su obra/categoria/nombre, para
        casarlos contra las partidas del presupuesto. Devuelve dicts ligeros
        (no ORM, para no arrastrar sesiones)."""
        with self._session_factory.create_session() as session:
            stmt = (
                select(
                    ParteRegistroOrm.id,
                    ParteRegistroOrm.obra_ide,
                    ParteRegistroOrm.categoria,
                    ParteRegistroOrm.empleado_nombre,
                    ParteRegistroOrm.trabajador_nombre_leido,
                    ParteRegistroOrm.partida_match_method,
                    ParteRegistroOrm.partida,
                )
                .join(
                    ParteDocumentOrm,
                    ParteRegistroOrm.document_id == ParteDocumentOrm.id,
                )
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            out: list[dict] = []
            for (rid, obra_ide, cat, emp_nom, leido, metodo,
                 partida_leida) in session.execute(stmt).all():
                out.append({
                    "registro_id": rid,
                    "obra_ide": obra_ide,
                    "categoria": cat,
                    "nombre": emp_nom or leido,
                    "partida_match_method": metodo,
                    "partida_leida": partida_leida,
                })
            return out

    def apply_partida_matches(self, updates: list[dict]) -> int:
        """Escribe el casado de partida por registro. Cada update:
        {registro_id, partida_ide, partida_cod, partida_res,
         partida_capitulo, partida_match_method, partida_match_score}.
        Devuelve el nº de registros actualizados."""
        if not updates:
            return 0
        n = 0
        with self._session_factory.create_session() as session:
            for u in updates:
                reg = session.get(ParteRegistroOrm, u.get("registro_id"))
                if reg is None:
                    continue
                reg.partida_ide = u.get("partida_ide")
                reg.partida_cod = u.get("partida_cod")
                reg.partida_res = u.get("partida_res")
                reg.partida_capitulo = u.get("partida_capitulo")
                reg.partida_match_method = u.get("partida_match_method")
                reg.partida_match_score = u.get("partida_match_score")
                n += 1
            session.commit()
        return n

    # ----------------------------------------------------------------- #
    # Conciliacion de RECURSO / parte de trabajo (Sigrid res + hmo).
    # ----------------------------------------------------------------- #
    def fetch_registros_para_recurso(self) -> list[dict]:
        """Registros de partes ACTIVOS con lo necesario para localizar el
        recurso y su parte de trabajo (empleado conciliado, obra y fecha) y
        para que el recurso pise categoria/hora: el tipo de hora de la linea
        y el codigo de hora/categoria ya resueltos (para detectar cambios)."""
        with self._session_factory.create_session() as session:
            stmt = (
                select(
                    ParteRegistroOrm.id,
                    ParteRegistroOrm.obra_ide,
                    ParteRegistroOrm.empleado_ide,
                    ParteRegistroOrm.empleado_reside,
                    ParteRegistroOrm.empleado_dni,
                    ParteRegistroOrm.fecha_int,
                    ParteRegistroOrm.tipo_hora,
                    ParteRegistroOrm.hora_ide,
                    ParteRegistroOrm.hora_codigo,
                    ParteRegistroOrm.categoria,
                    ParteRegistroOrm.horas,
                )
                .join(
                    ParteDocumentOrm,
                    ParteRegistroOrm.document_id == ParteDocumentOrm.id,
                )
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            out: list[dict] = []
            for (rid, obra_ide, emp_ide, reside, dni, fint, tipo_hora,
                 hora_ide, hora_codigo, categoria, horas) in session.execute(
                stmt
            ).all():
                out.append({
                    "registro_id": rid,
                    "obra_ide": obra_ide,
                    "empleado_ide": emp_ide,
                    "empleado_reside": reside,
                    "empleado_dni": dni,
                    "fecha_int": fint,
                    "tipo_hora": tipo_hora,
                    "hora_ide": hora_ide,
                    "hora_codigo": hora_codigo,
                    "categoria": categoria,
                    "horas": horas,
                })
            return out

    # ----- extras por jornada (revert + split) ----- #
    def revert_extras_auto(self) -> int:
        """Deshace las extras por jornada de pasadas anteriores para poder
        recalcular el dia desde el estado original del parte: borra los
        registros extra_auto y restaura las horas originales de los normales
        recortados. Idempotente."""
        n = 0
        with self._session_factory.create_session() as session:
            autos = session.execute(
                select(ParteRegistroOrm).where(
                    ParteRegistroOrm.extra_auto.is_(True)
                )
            ).scalars().all()
            for a in autos:
                session.delete(a)
                n += 1
            recortados = session.execute(
                select(ParteRegistroOrm).where(
                    ParteRegistroOrm.horas_orig.isnot(None)
                )
            ).scalars().all()
            for r in recortados:
                r.horas = r.horas_orig
                r.horas_orig = None
            session.commit()
        return n

    def apply_extras_splits(self, splits: list[dict]) -> int:
        """Aplica el paso de exceso de jornada a extra. Por cada split:
        recorta el registro normal (horas -> horas_norm, guardando horas_orig)
        e inserta un registro EXTRA (extra_auto) clonando el normal con la
        hora extra del recurso. Devuelve el numero de registros extra creados."""
        if not splits:
            return 0
        n = 0
        with self._session_factory.create_session() as session:
            for s in splits:
                normal = session.get(ParteRegistroOrm, s["normal_id"])
                if normal is None:
                    continue
                if normal.horas_orig is None:
                    normal.horas_orig = s["horas_orig"]
                normal.horas = s["horas_norm"]
                extra = ParteRegistroOrm(
                    document_id=normal.document_id,
                    line_index=normal.line_index,
                    empleado_line_no=normal.empleado_line_no,
                    categoria=normal.categoria,
                    trabajador_nombre_leido=normal.trabajador_nombre_leido,
                    empleado_ide=normal.empleado_ide,
                    empleado_codigo=normal.empleado_codigo,
                    empleado_nombre=normal.empleado_nombre,
                    empleado_dni=normal.empleado_dni,
                    empleado_reside=normal.empleado_reside,
                    empleado_match_score=normal.empleado_match_score,
                    empleado_match_method=normal.empleado_match_method,
                    fecha=normal.fecha,
                    fecha_int=normal.fecha_int,
                    obra_codigo=normal.obra_codigo,
                    obra_nombre=normal.obra_nombre,
                    obra_ide=normal.obra_ide,
                    tipo_hora="extra",
                    es_incidencia=False,
                    horas=s["extra_horas"],
                    partida=normal.partida,
                    partida_ide=normal.partida_ide,
                    partida_cod=normal.partida_cod,
                    partida_res=normal.partida_res,
                    partida_capitulo=normal.partida_capitulo,
                    partida_match_method=normal.partida_match_method,
                    partida_match_score=normal.partida_match_score,
                    recurso_ide=normal.recurso_ide,
                    recurso_cif=normal.recurso_cif,
                    hmo_ide=normal.hmo_ide,
                    parte_estado=normal.parte_estado,
                    hora_ide=s["hora_ext_ide"],
                    hora_codigo=s["hora_ext_cod"],
                    hora_descripcion=s["hora_ext_desc"],
                    hora_ext=s["hora_ext_ext"],
                    hora_candef=s["hora_candef"],
                    hora_match_method="extra_jornada",
                    extra_auto=True,
                    confianza_pct=normal.confianza_pct,
                )
                session.add(extra)
                n += 1
            session.commit()
        return n

    def apply_recurso_matches(self, updates: list[dict]) -> int:
        """Escribe el casado de recurso/parte por registro. Claves base:
        {registro_id, recurso_ide, recurso_cif, hmo_ide, parte_estado}.
        Si el recurso pisa categoria/hora, vienen ademas (solo entonces, para
        no borrar lo resuelto en lineas sin recurso o incidencias):
        categoria, hora_ide, hora_codigo, hora_descripcion, hora_ext,
        hora_candef, hora_match_method."""
        if not updates:
            return 0
        _PISA = (
            "categoria", "hora_ide", "hora_codigo", "hora_descripcion",
            "hora_ext", "hora_candef", "recurso_precio_hora",
            "hora_match_method",
        )
        n = 0
        with self._session_factory.create_session() as session:
            for u in updates:
                reg = session.get(ParteRegistroOrm, u.get("registro_id"))
                if reg is None:
                    continue
                reg.recurso_ide = u.get("recurso_ide")
                reg.recurso_cif = u.get("recurso_cif")
                reg.hmo_ide = u.get("hmo_ide")
                reg.parte_estado = u.get("parte_estado")
                for k in _PISA:
                    if k in u:
                        setattr(reg, k, u[k])
                n += 1
            session.commit()
        return n

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
        """Desactiva (soft-delete) los partes ACTIVOS del MISMO dia, MISMA
        obra y MISMO trabajador. Asi reenviar el parte de un trabajador (mismo
        dia/obra) 'pisa' al anterior, pero los partes de OTROS trabajadores del
        mismo dia/obra NO se tocan (cada pagina/PDF suele ser un trabajador).

        Identidad de obra: por codigo casado si lo hay; si no, por el numero
        de obra leido. Identidad de trabajador: ide casado, DNI o nombre.
        Si no hay fecha, ni obra, ni trabajador identificable, NO se desactiva
        nada (mas seguro)."""
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
        # Senales de los trabajadores del parte NUEVO.
        nuevos: set = set()
        for r in parte.registros:
            emp = r.empleado
            nuevos |= _emp_signals(
                emp.ide, emp.dni, r.trabajador_nombre_leido, emp.nombre
            )
        if not nuevos:
            return []  # parte sin trabajador identificable: no pisa nada

        ids: list[str] = []
        for old in session.execute(stmt).scalars().all():
            viejos: set = set()
            for oreg in old.registros:
                viejos |= _emp_signals(
                    oreg.empleado_ide, oreg.empleado_dni,
                    oreg.trabajador_nombre_leido, oreg.empleado_nombre,
                )
            if nuevos & viejos:  # comparten trabajador -> es un reemplazo
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

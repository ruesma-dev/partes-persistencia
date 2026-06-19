# application/pipelines/persist_parte_pipeline.py
"""Pipeline de persistencia de un parte de trabajo DIARIO (sv3).

Pasos:
  1. Lee el envelope ``{meta, data, debug}`` de sv2.
  2. Dedup por sha256 del documento (soft-delete consciente).
  3. Normaliza ``data`` -> ParteDocumento (expande empleados en registros).
  4. Casa contra Sigrid (si esta cableado):
       - obra a nivel de parte (numero/nombre),
       - por registro: empleado (por nombre) + codigo de hora ``auxhor``
         (normal/extra por ext, o incidencia por codigo).
  5. Calcula ``review_required``.
  6. Persiste documento + registros.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from application.services.parte_normalizer import ParteNormalizer
from application.services.sigrid_matcher_provider import SigridMatcherProvider
from domain.models.parte_records import ParteDocumento, PersistParteResult
from domain.models.parte_records import EmpleadoMatch
from domain.ports.parte_repository import ParteRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistParteRequest:
    filename: str
    mime_type: str
    file_bytes: bytes
    extraction_envelope: dict[str, Any]
    context: dict[str, Any]


class PersistPartePipeline:
    def __init__(
        self,
        *,
        repository: ParteRepository,
        normalizer: ParteNormalizer,
        matcher_provider: Optional[SigridMatcherProvider],
        sharepoint_uploader: Any = None,
    ) -> None:
        self._repository = repository
        self._normalizer = normalizer
        self._matcher_provider = matcher_provider
        # Uploader best-effort (duck-typing: .upload_parte_pdf). Si es None o
        # falla, el parte se guarda igual sin URL de SharePoint.
        self._sharepoint = sharepoint_uploader

    def run(self, request: PersistParteRequest) -> PersistParteResult:
        envelope = request.extraction_envelope or {}
        meta = envelope.get("meta") or {}
        data = envelope.get("data") or {}
        if not isinstance(meta, dict):
            meta = {}
        if not isinstance(data, dict):
            data = {}

        context = request.context if isinstance(request.context, dict) else {}
        document_ctx = context.get("document") or {}
        sha256 = str(
            document_ctx.get("sha256") or meta.get("source_sha256") or ""
        ).strip()

        # ---- Dedup ---- #
        if sha256:
            existing = self._repository.get_by_sha256(sha256)
            if existing is not None:
                logger.info(
                    "[persist-parte] sha256=%s ya existe (document_id=%s). "
                    "Se omite reinsercion.",
                    sha256, existing.document_id,
                )
                return PersistParteResult(
                    ok=True,
                    document_id=existing.document_id,
                    already_existed=True,
                    fecha=None,
                    obra_codigo=None,
                    empleados_distintos=0,
                    registros_persistidos=existing.registros,
                    registros_con_hora=0,
                    firmado=False,
                )

        # ---- Normalizacion + expansion ---- #
        email_ctx = context.get("email") or {}
        email_text = " ".join(
            str(email_ctx.get(k) or "")
            for k in ("subject", "bodyPreview", "body")
        ).strip() or None
        parte = self._normalizer.normalize(data, email_text=email_text)

        # ---- Casado Sigrid ---- #
        if self._matcher_provider is not None:
            self._match(parte)
        else:
            logger.info(
                "[persist-parte] Sigrid NO cableado: se persiste sin casar."
            )

        review_required = self._compute_review_required(parte)

        # ---- Archivado en SharePoint (best-effort) ---- #
        self._archive_to_sharepoint(parte, request, sha256)

        # ---- Persistencia ---- #
        document_id = str(uuid.uuid4())
        self._repository.save_parte(
            document_id=document_id,
            parte=parte,
            meta=meta,
            context=context,
            raw_extraction_json=json.dumps(envelope, ensure_ascii=False),
            raw_context_json=json.dumps(context, ensure_ascii=False),
            review_required=review_required,
        )

        empleados = {
            r.empleado.ide or r.trabajador_nombre_leido
            for r in parte.registros
        }
        registros_con_hora = sum(
            1 for r in parte.registros if r.hora.ide is not None
        )
        return PersistParteResult(
            ok=True,
            document_id=document_id,
            already_existed=False,
            fecha=parte.fecha_iso,
            obra_codigo=parte.obra.codigo or parte.obra_numero_leido,
            empleados_distintos=len(empleados),
            registros_persistidos=len(parte.registros),
            registros_con_hora=registros_con_hora,
            firmado=parte.firmado,
        )

    # ----------------------------------------------------------------- #
    def _archive_to_sharepoint(
        self,
        parte: ParteDocumento,
        request: PersistParteRequest,
        sha256: str,
    ) -> None:
        if self._sharepoint is None or not request.file_bytes:
            return
        try:
            stored = self._sharepoint.upload_parte_pdf(
                filename=request.filename,
                file_bytes=request.file_bytes,
                source_sha256=sha256 or "",
            )
            parte.sharepoint_url = stored.share_url or stored.web_url
            parte.sharepoint_item_id = stored.item_id
            parte.sharepoint_drive_id = stored.drive_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[persist-parte] SharePoint fallo (se guarda sin URL): %r", exc
            )

    # ----------------------------------------------------------------- #
    def _match(self, parte: ParteDocumento) -> None:
        assert self._matcher_provider is not None
        matchers = self._matcher_provider.get()

        # Obra (nivel parte).
        parte.obra = matchers.obra.match(
            codigo=parte.obra_numero_leido,
            nombre=parte.obra_nombre_leido,
        )

        # Cache de casado de empleado por nombre (evita rematchear el mismo
        # trabajador en sus varias filas normal/extra/incidencia).
        emp_cache: dict[str, Any] = {}
        # Descripcion del codigo ordinario resuelto por empleado, para derivar
        # el codigo extra de la misma categoria si la extra (creada por la
        # resta >8h) no trae codigo propuesto.
        ord_desc_by_emp: dict[str, str | None] = {}

        for reg in parte.registros:
            nombre = reg.trabajador_nombre_leido or ""
            key = nombre.strip().lower()
            if key in emp_cache:
                reg.empleado = emp_cache[key]
            else:
                # 1) Alias aprendido (casado confirmado en conciliacion):
                #    casa de forma EXACTA las variantes recurrentes de OCR.
                alias = self._repository.find_empleado_alias(
                    reg.trabajador_nombre_leido
                )
                if alias is not None:
                    match = EmpleadoMatch(
                        ide=alias["ide"], codigo=alias["codigo"],
                        nombre=alias["nombre"], dni=alias["dni"],
                        reside=None, score=1.0, method="alias",
                    )
                else:
                    # 2) Similitud contra el maestro (algoritmo mejorado).
                    match = matchers.empleado.match(
                        nombre=reg.trabajador_nombre_leido,
                        dni=None,
                        codigo=None,
                    )
                emp_cache[key] = match
                reg.empleado = match

            if reg.es_incidencia:
                reg.hora = matchers.tipo_hora.resolve(
                    tipo_hora=reg.tipo_hora,
                    codigo_hora_leido=reg.codigo_hora_propuesto,
                    incidencia_codigo=reg.incidencia.codigo,
                )
            elif reg.tipo_hora == "extra":
                reg.hora = matchers.tipo_hora.resolve(
                    tipo_hora="extra",
                    codigo_hora_leido=reg.codigo_hora_propuesto,
                    categoria=reg.categoria,
                    categoria_ref_desc=ord_desc_by_emp.get(key),
                )
            else:
                reg.hora = matchers.tipo_hora.resolve(
                    tipo_hora=reg.tipo_hora,
                    codigo_hora_leido=reg.codigo_hora_propuesto,
                    categoria=reg.categoria,
                )
                if reg.hora.descripcion:
                    ord_desc_by_emp[key] = reg.hora.descripcion

    # ----------------------------------------------------------------- #
    @staticmethod
    def _compute_review_required(parte: ParteDocumento) -> bool:
        # Revision si: sin registros, o el parte no esta firmado, o algun
        # registro no caso el empleado, o un registro de HORAS (no
        # incidencia) no tiene codigo de hora resuelto.
        if not parte.registros:
            return True
        if not parte.firmado:
            return True
        for reg in parte.registros:
            if reg.empleado.ide is None:
                return True
            if not reg.es_incidencia and reg.hora.ide is None:
                return True
        return False

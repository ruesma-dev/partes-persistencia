# interface_adapters/api/app.py
"""Wiring del servicio 3 (partes-persister).

Construye:
  - SessionFactory + SqlAlchemyParteRepository (auto-crea DB y tablas).
  - SigridApiClient (lookups de empleados/obras/tipos de hora) si hay
    credenciales SIGRID_API_*; en su defecto, el casado se omite.
  - SigridMatcherProvider (cachea los maestros + matchers con TTL).
  - PersistPartePipeline con todos los colaboradores.

Endpoint:
  POST /v1/partes/persist  (multipart: file + extraction_json + context_json)
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from application.pipelines.persist_parte_pipeline import (
    PersistPartePipeline,
    PersistParteRequest,
)
from application.services.parte_normalizer import ParteNormalizer
from application.services.sigrid_matcher_provider import SigridMatcherProvider
from config.settings import Settings
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_parte_repository import (
    SqlAlchemyParteRepository,
)
from infrastructure.sigrid.sigrid_api_client import SigridApiClient

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    # ----------------------------------------------------------- #
    # Persistencia.
    # ----------------------------------------------------------- #
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
    )
    repository = SqlAlchemyParteRepository(session_factory)
    repository.initialize()

    # ----------------------------------------------------------- #
    # Sigrid (casado). Solo si hay credenciales.
    # ----------------------------------------------------------- #
    matcher_provider: SigridMatcherProvider | None = None
    if settings.sigrid_credentials_present:
        sigrid_client = SigridApiClient(
            base_url=settings.sigrid_api_base_url,        # type: ignore[arg-type]
            function_key=settings.sigrid_api_function_key,  # type: ignore[arg-type]
            database=settings.sigrid_api_database,        # type: ignore[arg-type]
            empresa=settings.sigrid_empresa,
            timeout_s=settings.sigrid_api_timeout_s,
            max_rows=settings.sigrid_api_max_rows,
        )
        matcher_provider = SigridMatcherProvider(
            lookup=sigrid_client,
            empleado_min_score=settings.empleado_min_score,
            obra_min_score=settings.obra_min_score,
            default_hora_normal_cod=settings.default_hora_normal_cod,
            default_hora_extra_cod=settings.default_hora_extra_cod,
        )
        logger.info(
            "[svc3][wiring] Sigrid CABLEADO base_url=%s db=%s empresa=%s",
            settings.sigrid_api_base_url,
            settings.sigrid_api_database,
            settings.sigrid_empresa,
        )
    else:
        logger.warning(
            "[svc3][wiring] Sigrid NO cableado (faltan SIGRID_API_*). "
            "El parte se persistira SIN casar empleado/obra/codigo de hora."
        )

    # SharePoint (best-effort). Solo si hay GRAPH_KEY + config del modo.
    sharepoint_uploader = None
    if settings.sharepoint_enabled:
        from infrastructure.storage.sharepoint_parte_storage import (
            SharePointParteStorage,
        )
        sharepoint_uploader = SharePointParteStorage(
            graph_key=settings.graph_key,            # type: ignore[arg-type]
            timeout_s=settings.graph_timeout_s,
            mode=settings.sharepoint_mode,
            drive_id=settings.sharepoint_drive_id,
            folder_url=settings.sharepoint_folder_url,
            hostname=settings.sharepoint_hostname,
            site_path=settings.sharepoint_site_path,
            drive_name=settings.sharepoint_drive_name,
            folder_root=settings.sharepoint_folder_root,
            create_link=settings.sharepoint_create_link,
            link_type=settings.sharepoint_link_type,
            link_scope=settings.sharepoint_link_scope,
        )
        logger.info(
            "[svc3][wiring] SharePoint CABLEADO mode=%s root=%s",
            settings.sharepoint_mode, settings.sharepoint_folder_root,
        )
    else:
        logger.info(
            "[svc3][wiring] SharePoint NO cableado (falta GRAPH_KEY o config). "
            "El parte se guarda sin archivar el PDF."
        )

    pipeline = PersistPartePipeline(
        repository=repository,
        normalizer=ParteNormalizer(
            jornada_ordinaria_horas=settings.jornada_ordinaria_horas,
        ),
        matcher_provider=matcher_provider,
        sharepoint_uploader=sharepoint_uploader,
    )

    # ----------------------------------------------------------- #
    # FastAPI.
    # ----------------------------------------------------------- #
    app = FastAPI(
        title="Partes Persistence API",
        version=settings.service_version,
    )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "partes-persister",
            "version": settings.service_version,
            "database": settings.pg_db,
            "sigrid_wired": settings.sigrid_credentials_present,
        }

    @app.post("/v1/partes/persist")
    async def persist(
        file: UploadFile = File(...),
        extraction_json: str = Form(...),
        context_json: str = Form("{}"),
    ) -> Dict[str, Any]:
        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="Archivo vacio.")

        try:
            extraction_envelope = json.loads(extraction_json)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"extraction_json invalido: {exc}"
            ) from exc
        try:
            context = json.loads(context_json or "{}")
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"context_json invalido: {exc}"
            ) from exc

        try:
            result = pipeline.run(
                PersistParteRequest(
                    filename=file.filename or "document.bin",
                    mime_type=file.content_type or "application/octet-stream",
                    file_bytes=file_bytes,
                    extraction_envelope=extraction_envelope,
                    context=context if isinstance(context, dict) else {},
                )
            )
            return asdict(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "[svc3] persist FALLO filename=%s; devolviendo 500.",
                file.filename,
            )
            raise HTTPException(
                status_code=500, detail=f"Error persistiendo parte: {exc}"
            ) from exc

    return app

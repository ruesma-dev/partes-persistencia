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
from application.services.partida_conciliador import PartidaConciliador
from application.services.recurso_conciliador import RecursoConciliador
from application.services.sigrid_matcher_provider import SigridMatcherProvider
from config.settings import Settings
from infrastructure.calendario.json_calendario_laboral import (
    JsonCalendarioLaboral,
)
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
    partida_conciliador: PartidaConciliador | None = None
    recurso_conciliador: RecursoConciliador | None = None
    sigrid_client: SigridApiClient | None = None
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
        # Conciliacion de partidas: usa el mismo cliente Sigrid (lee obrparpar
        # por obra) y el repositorio (lee registros / escribe el casado).
        partida_conciliador = PartidaConciliador(
            repository=repository,
            lookup=sigrid_client,
        )
        # Conciliacion de recurso/parte de trabajo (mismo cliente Sigrid:
        # lee res + hmo por obra; el repositorio lee/escribe el casado).
        recurso_conciliador = RecursoConciliador(
            repository=repository,
            lookup=sigrid_client,
            calendario=JsonCalendarioLaboral(
                path=settings.calendario_laboral_path
            ),
            jornada_ordinaria_horas=settings.jornada_ordinaria_horas,
            candef_minimo=settings.candef_minimo_valido,
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
        partida_conciliador=partida_conciliador,
        recurso_conciliador=recurso_conciliador,
    )

    # ----------------------------------------------------------- #
    # FastAPI.
    # ----------------------------------------------------------- #
    app = FastAPI(
        title="Partes Persistence API",
        version=settings.service_version,
    )
    # Expuesto para que el worker (main_worker.py) reutilice el mismo wiring.
    app.state.pipeline = pipeline

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "partes-persister",
            "version": settings.service_version,
            "database": settings.pg_db,
            "sigrid_wired": settings.sigrid_credentials_present,
        }

    @app.post("/admin/reconciliar-recursos")
    def reconciliar_recursos() -> Dict[str, Any]:
        """Re-dispara la conciliacion de recurso/parte sobre TODOS los
        partes activos: revierte los extra-auto previos y recalcula el
        reparto ordinarias/extra aplicando fin de semana y festivos. Util
        para aplicar cambios de calendario sin reprocesar el lote."""
        if recurso_conciliador is None:
            return {
                "ok": False,
                "error": "Sigrid no cableado (faltan SIGRID_API_*).",
            }
        try:
            res = recurso_conciliador.conciliar_todos()
            return {"ok": True, **res}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @app.get("/diag/sharepoint")
    def diag_sharepoint() -> Dict[str, Any]:
        """Diagnostico del archivado en SharePoint: si esta activado, si el
        uploader esta cableado, y una prueba EN VIVO contra Graph que
        devuelve el error concreto (auth / drive inexistente / sin permiso).
        """
        info: Dict[str, Any] = {
            "enabled": settings.sharepoint_enabled,
            "uploader_wired": sharepoint_uploader is not None,
            "mode": settings.sharepoint_mode,
            "folder_root": settings.sharepoint_folder_root,
            "has_graph_key": bool((settings.graph_key or "").strip()),
            "has_drive_id": bool((settings.sharepoint_drive_id or "").strip()),
            "has_folder_url": bool((settings.sharepoint_folder_url or "").strip()),
            "has_site_path": bool(
                (settings.sharepoint_hostname or "").strip()
                and (settings.sharepoint_site_path or "").strip()
            ),
        }
        if sharepoint_uploader is None:
            info["probe"] = {
                "ok": False,
                "reason": "uploader NO cableado: sharepoint_enabled es False "
                          "(falta GRAPH_KEY o la config del modo).",
            }
            return info
        try:
            info["probe"] = {"ok": True, **sharepoint_uploader.probe()}
        except Exception as exc:  # noqa: BLE001
            info["probe"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return info

    @app.get("/diag/partidas/{obra_ide}")
    def diag_partidas(obra_ide: int) -> Dict[str, Any]:
        """Diagnostico del casado de partidas de una obra: lee ``obrparpar``
        en vivo, construye el arbol y muestra el conteo de hojas por capitulo
        (CD/CI/CP/OTRO) y las partidas hoja de CI/CD, para ver por que casa o
        no (p.ej. si CI sale 0, el problema es la clasificacion/los datos)."""
        if sigrid_client is None:
            return {"ok": False, "error": "Sigrid no cableado (faltan SIGRID_API_*)."}
        from application.services.partida_catalog import (
            build_arbol_partidas,
            partidas_hoja,
        )
        try:
            filas = sigrid_client.fetch_partidas_obra(obra_ide)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "obra_ide": obra_ide,
                "error": f"fetch_partidas_obra fallo: {exc!r}",
            }
        nodos = build_arbol_partidas(filas)
        por_cat: Dict[str, int] = {}
        for n in nodos.values():
            if n.es_hoja and n.activa:
                por_cat[n.categoria] = por_cat.get(n.categoria, 0) + 1

        def _muestra(lst):
            return [
                {"cod": n.cod, "res": n.res, "ruta": n.ruta_capitulos}
                for n in lst[:40]
            ]

        return {
            "ok": True,
            "obra_ide": obra_ide,
            "obrparpar_filas": len(filas),
            "nodos": len(nodos),
            "hojas_activas_por_categoria": por_cat,
            "ci_hojas": _muestra(partidas_hoja(nodos, categoria="CI")),
            "cd_hojas_muestra": _muestra(partidas_hoja(nodos, categoria="CD")),
            # primeras filas crudas para inspeccionar codigos/descripciones
            "raw_muestra": [
                {"ide": f.ide, "padide": f.padide, "cod": f.cod, "res": f.res}
                for f in filas[:25]
            ],
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

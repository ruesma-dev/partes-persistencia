# main_worker.py
"""Worker de sv3 (persistencia).

Consume 'q-persistencia', lee el PDF de 'input/{document_id}.pdf' y el envelope
de 'envelopes/{document_id}.json', y persiste (PostgreSQL + SharePoint + Sigrid).
La idempotencia at-least-once la cubre el dedup por sha256 del propio sv3.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from application.pipelines.persist_parte_pipeline import PersistParteRequest
from config.logging_config import configure_logging
from config.settings import Settings
from infrastructure.azure.credenciales import (
    construir_blob_cliente,
    construir_cola_cliente,
)
from interface_adapters.api.app import build_app

logger = logging.getLogger(__name__)

COLA_ENTRADA = os.getenv("COLA_PERSISTENCIA", "q-persistencia")
CONTENEDOR_INPUT = os.getenv("BLOB_INPUT", "input")
CONTENEDOR_ENVELOPES = os.getenv("BLOB_ENVELOPES", "envelopes")


def main() -> int:
    settings = Settings()
    configure_logging(Path(settings.log_dir), settings.log_level)
    logger.info("[sv3-worker] arrancando. cola=%s", COLA_ENTRADA)

    colas_cs = settings.colas_connection_string
    colas_url = settings.colas_account_url or os.getenv("COLAS_ACCOUNT_URL")
    if not colas_cs and not colas_url:
        logger.error(
            "[sv3-worker] falta storage: define COLAS_CONNECTION_STRING "
            "(local/Azurite) o COLAS_ACCOUNT_URL (nube) en el .env / "
            "Container App."
        )
        return 1

    cola = construir_cola_cliente(
        connection_string=colas_cs, account_url=colas_url,
        visibility_timeout_s=int(os.getenv("COLA_VISIBILITY_S", "300")),
    )
    blob = construir_blob_cliente(
        connection_string=settings.blobs_connection_string,
        account_url=settings.blobs_account_url
        or os.getenv("BLOBS_ACCOUNT_URL"),
        colas_connection_string=colas_cs,
    )
    if colas_cs:
        # Modo local (Azurite arranca vacio): asegura colas y contenedores.
        cola.asegurar_colas([COLA_ENTRADA])
        blob.asegurar_contenedores([CONTENEDOR_INPUT, CONTENEDOR_ENVELOPES])
    pipeline = build_app(settings).state.pipeline

    def handler(payload: dict) -> None:
        document_id = payload["document_id"]
        filename = payload.get("filename", "document.pdf")
        mime = payload.get("mime_type", "application/pdf")
        context = payload.get("context", {})
        pdf = blob.descargar(CONTENEDOR_INPUT, f"{document_id}.pdf")
        envelope = json.loads(
            blob.descargar(CONTENEDOR_ENVELOPES, f"{document_id}.json"))
        result = pipeline.run(PersistParteRequest(
            filename=filename, mime_type=mime, file_bytes=pdf,
            extraction_envelope=envelope,
            context=context if isinstance(context, dict) else {}))
        logger.info("[sv3-worker] persistido document_id=%s -> %s",
                    document_id, asdict(result))

    cola.consumir(COLA_ENTRADA, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

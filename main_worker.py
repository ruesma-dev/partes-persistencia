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
from infrastructure.azure.blob_cliente import BlobCliente
from infrastructure.azure.cola_cliente import ColaCliente
from infrastructure.azure.credenciales import build_credential
from interface_adapters.api.app import build_app

logger = logging.getLogger(__name__)

COLA_ENTRADA = os.getenv("COLA_PERSISTENCIA", "q-persistencia")
CONTENEDOR_INPUT = os.getenv("BLOB_INPUT", "input")
CONTENEDOR_ENVELOPES = os.getenv("BLOB_ENVELOPES", "envelopes")


def main() -> int:
    settings = Settings()
    configure_logging(Path(settings.log_dir), settings.log_level)
    logger.info("[sv3-worker] arrancando. cola=%s", COLA_ENTRADA)

    cred = build_credential()
    cola = ColaCliente(
        os.environ["COLAS_ACCOUNT_URL"], cred,
        visibility_timeout_s=int(os.getenv("COLA_VISIBILITY_S", "300")),
    )
    blob = BlobCliente(os.environ["BLOBS_ACCOUNT_URL"], cred)
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

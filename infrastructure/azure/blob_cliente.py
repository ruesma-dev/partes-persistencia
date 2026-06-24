# infrastructure/azure/blob_cliente.py
"""Cliente de Blob para el hand-off efimero entre workers.

Contenedores: 'input' (PDF original) y 'envelopes' (extraccion JSON). Lo
durable vive en SharePoint (PDF) y PostgreSQL (datos); estos blobs los purga
la lifecycle policy a los 14 dias.
"""
from __future__ import annotations

import logging

from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)


class BlobCliente:
    def __init__(self, account_url: str, credential) -> None:
        if not account_url:
            raise ValueError("BlobCliente requiere BLOBS_ACCOUNT_URL.")
        self._svc = BlobServiceClient(account_url=account_url, credential=credential)

    def subir(self, container: str, name: str, data: bytes,
              content_type: str | None = None) -> None:
        from azure.storage.blob import ContentSettings
        cs = ContentSettings(content_type=content_type) if content_type else None
        bc = self._svc.get_blob_client(container=container, blob=name)
        bc.upload_blob(data, overwrite=True, content_settings=cs)
        logger.info("[blob] subido %s/%s (%s bytes)", container, name, len(data))

    def descargar(self, container: str, name: str) -> bytes:
        bc = self._svc.get_blob_client(container=container, blob=name)
        data = bc.download_blob().readall()
        logger.info("[blob] descargado %s/%s (%s bytes)", container, name, len(data))
        return data

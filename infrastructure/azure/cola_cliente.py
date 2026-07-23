# infrastructure/azure/cola_cliente.py
"""Cliente de Storage Queue + bucle de consumo para los workers.

Patron albaranes: mensajes JSON (metadatos; el PDF/envelope van por Blob),
auth por managed identity, escalado por KEDA azure-queue. Idempotencia
at-least-once: si el handler falla, el mensaje reaparece tras el visibility
timeout; superado 'max_dequeue', va a la cola '<cola>-poison' (DLQ) y se borra
de la principal. Los workers NO borran blobs (eso lo hace la lifecycle policy).
"""
from __future__ import annotations

import json
import logging
import signal
import time
from typing import Callable

from azure.storage.queue import QueueClient, QueueServiceClient

logger = logging.getLogger(__name__)


class ColaCliente:
    def __init__(
        self,
        account_url: str | None = None,
        credential=None,
        *,
        connection_string: str | None = None,
        max_dequeue: int = 5,
        visibility_timeout_s: int = 600,
        poll_interval_s: int = 5,
    ) -> None:
        # Dos modos (patron albaranes):
        #  - connection_string: local/Azurite (o cuenta con clave).
        #  - account_url + credential: nube (managed identity / az login).
        if connection_string:
            self._svc = QueueServiceClient.from_connection_string(
                connection_string
            )
        elif account_url:
            self._svc = QueueServiceClient(
                account_url=account_url.rstrip("/"), credential=credential
            )
        else:
            raise ValueError(
                "ColaCliente requiere COLAS_CONNECTION_STRING (local/Azurite)"
                " o COLAS_ACCOUNT_URL (nube)."
            )
        self._max_dequeue = int(max_dequeue)
        self._vt = int(visibility_timeout_s)
        self._poll = int(poll_interval_s)
        self._stop = False

    def asegurar_colas(self, nombres: list[str]) -> None:
        """Crea las colas (y sus '-poison') si no existen. Para el modo
        local con Azurite, que arranca vacio; idempotente."""
        from azure.core.exceptions import ResourceExistsError
        todos: list[str] = []
        for n in nombres:
            todos.extend((n, f"{n}-poison"))
        for n in todos:
            try:
                self._svc.create_queue(n)
                logger.info("[cola] creada cola '%s'", n)
            except ResourceExistsError:
                pass

    # -- Productor ----------------------------------------------------------
    def enviar(self, queue_name: str, payload: dict) -> None:
        qc = self._svc.get_queue_client(queue_name)
        qc.send_message(json.dumps(payload, ensure_ascii=False))
        logger.info("[cola] -> %s document_id=%s", queue_name,
                    payload.get("document_id"))

    # -- Consumidor (bucle) -------------------------------------------------
    def consumir(self, queue_name: str, handler: Callable[[dict], None]) -> None:
        principal: QueueClient = self._svc.get_queue_client(queue_name)
        poison: QueueClient = self._svc.get_queue_client(f"{queue_name}-poison")

        # Salida limpia ante SIGTERM (KEDA escala a 0 / Container Apps reinicia).
        def _sig(_signum, _frame):
            logger.info("[cola] SIGTERM recibido; terminando tras el mensaje actual.")
            self._stop = True
        try:
            signal.signal(signal.SIGTERM, _sig)
            signal.signal(signal.SIGINT, _sig)
        except (ValueError, OSError):
            pass  # no en hilo principal: ignorable

        logger.info("[cola] consumiendo '%s' (vt=%ss, max_dequeue=%s)",
                    queue_name, self._vt, self._max_dequeue)
        while not self._stop:
            got = False
            for msg in principal.receive_messages(
                messages_per_page=1, visibility_timeout=self._vt
            ):
                got = True
                document_id = "?"
                try:
                    payload = json.loads(msg.content)
                    document_id = payload.get("document_id", "?")
                    if msg.dequeue_count and msg.dequeue_count > self._max_dequeue:
                        logger.error(
                            "[cola] %s document_id=%s supero max_dequeue=%s -> poison",
                            queue_name, document_id, self._max_dequeue)
                        poison.send_message(msg.content)
                        principal.delete_message(msg)
                        continue
                    handler(payload)
                    principal.delete_message(msg)
                    logger.info("[cola] OK %s document_id=%s (dequeue=%s)",
                                queue_name, document_id, msg.dequeue_count)
                except Exception:  # noqa: BLE001
                    # No se borra: reaparece tras el visibility timeout y reintenta.
                    logger.exception(
                        "[cola] FALLO %s document_id=%s (dequeue=%s); se reintentara.",
                        queue_name, document_id, getattr(msg, "dequeue_count", "?"))
                if self._stop:
                    break
            if not got and not self._stop:
                time.sleep(self._poll)
        logger.info("[cola] consumo de '%s' detenido.", queue_name)

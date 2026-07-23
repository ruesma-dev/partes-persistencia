# infrastructure/azure/credenciales.py
"""Credencial de Azure para cola/blob.

En Container Apps usa la managed identity asignada (AZURE_CLIENT_ID); en local
cae a la cadena por defecto (az login, variables, etc.). Mismo patron que
albaranes: nada de connection strings ni claves en runtime.
"""
from __future__ import annotations

import os

from azure.identity import DefaultAzureCredential


def build_credential() -> DefaultAzureCredential:
    client_id = os.getenv("AZURE_CLIENT_ID") or None
    return DefaultAzureCredential(managed_identity_client_id=client_id)


def _derivar_blob_connection_string(colas_cs: str) -> str:
    """Deriva la connection string de BLOB desde la de COLAS.

    Con Azurite la CS tipica solo trae QueueEndpoint (puerto 10001); el
    BlobEndpoint es el mismo host con puerto 10000. Si ya trae
    BlobEndpoint, se devuelve tal cual.
    """
    if "BlobEndpoint=" in colas_cs:
        return colas_cs
    partes = [p for p in colas_cs.split(";") if p]
    for p in partes:
        if p.startswith("QueueEndpoint="):
            blob_ep = p.split("=", 1)[1].replace(":10001", ":10000")
            partes.append(f"BlobEndpoint={blob_ep}")
            break
    return ";".join(partes) + ";"


def construir_cola_cliente(
    *,
    connection_string: str | None,
    account_url: str | None,
    **kwargs,
):
    """Factoria de ColaCliente: connection string (local/Azurite) manda
    sobre account_url + identidad (nube)."""
    from infrastructure.azure.cola_cliente import ColaCliente

    if connection_string:
        return ColaCliente(connection_string=connection_string, **kwargs)
    return ColaCliente(account_url, build_credential(), **kwargs)


def construir_blob_cliente(
    *,
    connection_string: str | None,
    account_url: str | None,
    colas_connection_string: str | None = None,
):
    """Factoria de BlobCliente. Si no hay CS de blob pero si de colas
    (Azurite), el BlobEndpoint se deriva solo."""
    from infrastructure.azure.blob_cliente import BlobCliente

    cs = connection_string
    if not cs and colas_connection_string:
        cs = _derivar_blob_connection_string(colas_connection_string)
    if cs:
        return BlobCliente(connection_string=cs)
    return BlobCliente(account_url, build_credential())

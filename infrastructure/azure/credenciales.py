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

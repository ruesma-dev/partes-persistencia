# infrastructure/storage/sharepoint_parte_storage.py
"""Archiva el PDF del parte en SharePoint via Microsoft Graph.

Autocontenido: solo depende de ``httpx`` y del ``GraphTokenProvider``
(credenciales client-credentials). NO usa paquetes compartidos.

Soporta los tres modos del proyecto original:
  - ``drive_id``  : se da el drive directamente (SHAREPOINT_DRIVE_ID).
  - ``folder_url``: se da un enlace de carpeta compartida; se resuelve a
    drive + item base.
  - ``site_path`` : se da hostname + ruta de sitio; se resuelve site ->
    drive por nombre.

El archivo se sube a ``<folder_root>/<YYYY>/<MM>/<sha8>_<nombre>.pdf`` y se
devuelve la URL web (y un enlace de compartir si se pide).

Best-effort: este adaptador hace solo el mecanico y propaga excepciones;
el orquestador (pipeline) las captura para no romper el guardado en BBDD.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

import httpx

from infrastructure.graph.token_provider import GraphTokenProvider

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_SAFE_RE = re.compile(r'[\\/:*?"<>|]+')


@dataclass(frozen=True)
class StoredParteFile:
    drive_id: str
    item_id: str
    relative_path: str
    web_url: str | None
    share_url: str | None


class SharePointParteStorage:
    def __init__(
        self,
        *,
        graph_key: str,
        timeout_s: int,
        mode: str,
        drive_id: str | None,
        folder_url: str | None,
        hostname: str | None,
        site_path: str | None,
        drive_name: str,
        folder_root: str,
        create_link: bool,
        link_type: str,
        link_scope: str,
    ) -> None:
        self._token = GraphTokenProvider(graph_key, timeout_s)
        self._mode = (mode or "drive_id").strip().lower()
        self._drive_id = (drive_id or "").strip() or None
        self._folder_url = (folder_url or "").strip() or None
        self._hostname = (hostname or "").strip() or None
        self._site_path = (site_path or "").strip() or None
        self._drive_name = (drive_name or "Documentos").strip()
        self._folder_root = (folder_root or "partes").strip().strip("/")
        self._create_link = bool(create_link)
        self._link_type = (link_type or "view").strip()
        self._link_scope = (link_scope or "organization").strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(30, timeout_s)),
            trust_env=True,
        )

    # ----------------------------- helpers ----------------------------- #
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_token()}"}

    @staticmethod
    def _safe_filename(name: str) -> str:
        base = _SAFE_RE.sub("_", (name or "").strip()) or "parte.pdf"
        if not base.lower().endswith(".pdf"):
            base = f"{base}.pdf"
        return base

    def _get(self, url: str) -> dict:
        r = self._client.get(url, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def _resolve_site_id(self) -> str:
        assert self._hostname and self._site_path
        path = self._site_path.strip("/")
        url = f"{_GRAPH}/sites/{self._hostname}:/{path}"
        return str(self._get(url)["id"])

    def _resolve_drive_id_by_name(self, site_id: str) -> str:
        data = self._get(f"{_GRAPH}/sites/{site_id}/drives")
        target = self._drive_name.lower()
        for d in data.get("value", []):
            if str(d.get("name", "")).lower() == target:
                return str(d["id"])
        # Si no encuentra por nombre, usa el drive por defecto del sitio.
        return str(self._get(f"{_GRAPH}/sites/{site_id}/drive")["id"])

    def _resolve_folder_from_share_url(self) -> tuple[str, str]:
        """Devuelve (drive_id, item_id) de la carpeta del enlace compartido."""
        assert self._folder_url
        raw = base64.urlsafe_b64encode(
            self._folder_url.encode("utf-8")
        ).decode("utf-8").rstrip("=")
        share_id = f"u!{raw}"
        item = self._get(f"{_GRAPH}/shares/{share_id}/driveItem")
        drive_id = str((item.get("parentReference") or {}).get("driveId") or "")
        item_id = str(item.get("id") or "")
        if not drive_id or not item_id:
            raise RuntimeError("No se pudo resolver la carpeta del enlace.")
        return drive_id, item_id

    def _ensure_child_folder(
        self, *, drive_id: str, parent_item_id: str, folder_name: str
    ) -> str:
        # Busca el hijo por nombre; si no existe, lo crea.
        url = (
            f"{_GRAPH}/drives/{drive_id}/items/{parent_item_id}/children"
            f"?$filter=name eq '{folder_name}'&$select=id,name,folder"
        )
        try:
            data = self._get(url)
            for child in data.get("value", []):
                if str(child.get("name")) == folder_name and child.get("folder"):
                    return str(child["id"])
        except httpx.HTTPStatusError:
            pass
        body = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        r = self._client.post(
            f"{_GRAPH}/drives/{drive_id}/items/{parent_item_id}/children",
            headers=self._headers(),
            json=body,
        )
        if r.status_code == 409:  # carrera: lo creo otro, releo
            data = self._get(url)
            for child in data.get("value", []):
                if str(child.get("name")) == folder_name:
                    return str(child["id"])
        r.raise_for_status()
        return str(r.json()["id"])

    def _resolve_base(self) -> tuple[str, str]:
        """(drive_id, item_id) de la carpeta raiz <folder_root>."""
        if self._mode == "folder_url":
            return self._resolve_folder_from_share_url()

        if self._mode == "site_path":
            site_id = self._resolve_site_id()
            drive_id = self._resolve_drive_id_by_name(site_id)
        elif self._mode == "drive_id":
            if not self._drive_id:
                raise RuntimeError("SHAREPOINT_DRIVE_ID requerido.")
            drive_id = self._drive_id
        else:
            raise RuntimeError(f"SHAREPOINT_MODE invalido: {self._mode}")

        root_id = str(self._get(f"{_GRAPH}/drives/{drive_id}/root")["id"])
        parent = root_id
        for part in [p for p in self._folder_root.split("/") if p]:
            parent = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=parent, folder_name=part
            )
        return drive_id, parent

    def _upload_by_parent(
        self,
        *,
        drive_id: str,
        parent_item_id: str,
        filename: str,
        mime_type: str,
        file_bytes: bytes,
    ) -> dict:
        # Subida simple (PUT content). Valido para PDFs de un parte (pequenos).
        url = (
            f"{_GRAPH}/drives/{drive_id}/items/{parent_item_id}:/"
            f"{filename}:/content"
        )
        r = self._client.put(
            url,
            headers={**self._headers(), "Content-Type": mime_type},
            content=file_bytes,
        )
        r.raise_for_status()
        return r.json()

    def _create_share_link(self, *, drive_id: str, item_id: str) -> str | None:
        try:
            body = {"type": self._link_type, "scope": self._link_scope}
            r = self._client.post(
                f"{_GRAPH}/drives/{drive_id}/items/{item_id}/createLink",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            link = (r.json().get("link") or {}).get("webUrl")
            return str(link).strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sp-parte] createLink fallo: %r", exc)
            return None

    # ------------------------------ API ------------------------------- #
    def upload_parte_pdf(
        self,
        *,
        filename: str,
        file_bytes: bytes,
        source_sha256: str,
    ) -> StoredParteFile:
        if not file_bytes:
            raise ValueError("upload_parte_pdf: file_bytes vacio.")
        drive_id, base_id = self._resolve_base()
        now = datetime.now(timezone.utc)
        parent = base_id
        for folder in (now.strftime("%Y"), now.strftime("%m")):
            parent = self._ensure_child_folder(
                drive_id=drive_id, parent_item_id=parent, folder_name=folder
            )
        prefix = (source_sha256 or "")[:8] or "parte"
        final_name = f"{prefix}_{self._safe_filename(filename)}"
        uploaded = self._upload_by_parent(
            drive_id=drive_id,
            parent_item_id=parent,
            filename=final_name,
            mime_type="application/pdf",
            file_bytes=file_bytes,
        )
        item_id = str(uploaded.get("id") or "")
        if not item_id:
            raise RuntimeError("SharePoint no devolvio driveItem.id.")
        web_url = str(uploaded.get("webUrl") or "").strip() or None
        share_url = (
            self._create_share_link(drive_id=drive_id, item_id=item_id)
            if self._create_link else None
        )
        relative_path = str(
            PurePosixPath(self._folder_root)
            / now.strftime("%Y") / now.strftime("%m") / final_name
        )
        logger.info(
            "[sp-parte] subido %s -> %s", relative_path, web_url,
        )
        return StoredParteFile(
            drive_id=drive_id,
            item_id=item_id,
            relative_path=relative_path,
            web_url=web_url,
            share_url=share_url or web_url,
        )

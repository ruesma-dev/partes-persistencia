# domain/ports/parte_repository.py
from __future__ import annotations

from typing import Any, Protocol

from domain.models.parte_records import ExistingParte, ParteDocumento


class ParteRepository(Protocol):
    def initialize(self) -> None:
        ...

    def get_by_sha256(self, source_sha256: str) -> ExistingParte | None:
        ...

    def find_empleado_alias(self, nombre_leido: str | None) -> Any | None:
        ...

    def save_parte(
        self,
        *,
        document_id: str,
        parte: ParteDocumento,
        meta: dict[str, Any],
        context: dict[str, Any],
        raw_extraction_json: str,
        raw_context_json: str,
        review_required: bool,
    ) -> None:
        ...

# application/services/obra_matcher.py
"""Casa una obra leida (codigo y/o nombre) contra el maestro ``obr`` de
Sigrid. Prioridad: codigo exacto > codigo con ceros a la izquierda >
nombre por similitud.

Los partes escriben el numero de obra sin ceros ('672'), pero en Sigrid
el codigo suele ir con ceros a 4 digitos ('0672'). Por eso, si el codigo
leido es numerico y no casa tal cual, se prueban variantes con/ sin ceros.
"""
from __future__ import annotations

import logging

from application.services import text_match as tm
from domain.models.parte_records import ObraMatch
from domain.models.sigrid_models import ObraRow

logger = logging.getLogger(__name__)


def _code_candidates(code_norm: str) -> list[str]:
    """Variantes de un codigo para tolerar ceros a la izquierda."""
    out = [code_norm]
    if code_norm.isdigit():
        # Sin ceros a la izquierda y rellenado a 3/4/5 digitos.
        stripped = code_norm.lstrip("0") or "0"
        for variant in (stripped, stripped.zfill(3),
                        stripped.zfill(4), stripped.zfill(5)):
            if variant not in out:
                out.append(variant)
    return out


class ObraMatcher:
    def __init__(
        self,
        *,
        obras: list[ObraRow],
        min_score: float = 0.55,
    ) -> None:
        self._obras = obras
        self._min_score = float(min_score)
        self._by_codigo: dict[str, ObraRow] = {}
        for o in obras:
            cod_n = tm.normalize_code(o.codigo)
            if cod_n:
                self._by_codigo.setdefault(cod_n, o)
        logger.info(
            "[obra-matcher] %s obras (cod=%s) min_score=%s",
            len(obras), len(self._by_codigo), self._min_score,
        )

    def match(
        self,
        *,
        codigo: str | None,
        nombre: str | None,
    ) -> ObraMatch:
        cod_n = tm.normalize_code(codigo)
        if cod_n:
            # Exacto.
            if cod_n in self._by_codigo:
                return self._to_match(self._by_codigo[cod_n], 1.0, "codigo")
            # Variantes con ceros a la izquierda.
            for cand in _code_candidates(cod_n)[1:]:
                if cand in self._by_codigo:
                    return self._to_match(
                        self._by_codigo[cand], 0.98, "codigo_padded"
                    )

        if nombre and tm.normalize(nombre):
            best: ObraRow | None = None
            best_score = 0.0
            for o in self._obras:
                score = tm.name_similarity(nombre, o.nombre)
                if score > best_score:
                    best_score, best = score, o
            if best is not None and best_score >= self._min_score:
                return self._to_match(best, best_score, "nombre")

        return ObraMatch()

    @staticmethod
    def _to_match(o: ObraRow, score: float, method: str) -> ObraMatch:
        return ObraMatch(
            ide=o.ide,
            codigo=o.codigo,
            nombre=o.nombre,
            score=round(score, 4),
            method=method,
        )

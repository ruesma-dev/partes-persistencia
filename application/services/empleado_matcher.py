# application/services/empleado_matcher.py
"""Casa el trabajador leido del parte contra el maestro ``emp`` de Sigrid.

Prioridad: DNI exacto > codigo exacto > nombre por similitud. El maestro
se carga UNA vez (en el wiring) y se indexa para casar O(1) por DNI /
codigo y O(n) por nombre.
"""
from __future__ import annotations

import logging

from application.services import text_match as tm
from domain.models.parte_records import EmpleadoMatch
from domain.models.sigrid_models import EmpleadoRow

logger = logging.getLogger(__name__)


class EmpleadoMatcher:
    def __init__(
        self,
        *,
        empleados: list[EmpleadoRow],
        min_score: float = 0.55,
    ) -> None:
        self._empleados = empleados
        self._min_score = float(min_score)
        self._by_dni: dict[str, EmpleadoRow] = {}
        self._by_codigo: dict[str, EmpleadoRow] = {}
        for e in empleados:
            dni_n = tm.normalize_dni(e.dni)
            if dni_n:
                self._by_dni.setdefault(dni_n, e)
            cod_n = tm.normalize_code(e.codigo)
            if cod_n:
                self._by_codigo.setdefault(cod_n, e)
        logger.info(
            "[empleado-matcher] %s empleados (dni=%s cod=%s) min_score=%s",
            len(empleados), len(self._by_dni), len(self._by_codigo),
            self._min_score,
        )

    def match(
        self,
        *,
        nombre: str | None,
        dni: str | None,
        codigo: str | None,
    ) -> EmpleadoMatch:
        # 1) DNI exacto.
        dni_n = tm.normalize_dni(dni)
        if dni_n and dni_n in self._by_dni:
            return self._to_match(self._by_dni[dni_n], 1.0, "dni")

        # 2) Codigo exacto.
        cod_n = tm.normalize_code(codigo)
        if cod_n and cod_n in self._by_codigo:
            return self._to_match(self._by_codigo[cod_n], 1.0, "codigo")

        # 3) Nombre por similitud.
        if nombre and tm.normalize(nombre):
            best: EmpleadoRow | None = None
            best_score = 0.0
            for e in self._empleados:
                score = tm.name_similarity(nombre, e.nombre)
                if score > best_score:
                    best_score, best = score, e
            if best is not None and best_score >= self._min_score:
                return self._to_match(best, best_score, "nombre")

        return EmpleadoMatch()

    @staticmethod
    def _to_match(e: EmpleadoRow, score: float, method: str) -> EmpleadoMatch:
        return EmpleadoMatch(
            ide=e.ide,
            codigo=e.codigo,
            nombre=e.nombre,
            dni=e.dni,
            reside=e.reside,
            score=round(score, 4),
            method=method,
        )

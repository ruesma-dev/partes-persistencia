# application/services/recurso_conciliador.py
"""Conciliacion automatica del RECURSO / parte de trabajo para TODOS los
partes (sv3). Se lanza al persistir, junto al casado de partidas.

Por cada linea de parte (de un trabajador ya conciliado contra ``emp``):
  1. Localiza su RECURSO de Sigrid (``res``): por ``emp.reside`` (ya casado),
     y si falta, por ``res.conide = empleado`` o ``res.cif = DNI``.
  2. Localiza el PARTE DE TRABAJO (``hmo``) donde se imputarian las horas:
     por ``reside + obra + ano + mes`` (la fecha del registro da ano/mes).

Resultado por registro: ``recurso_ide`` / ``recurso_cif`` / ``hmo_ide`` /
``parte_estado`` (``ok`` = hay parte; ``sin_parte`` = recurso si pero sin
parte de ese mes/obra; ``sin_recurso`` = no se localizo el recurso).

NO escribe en Sigrid. Best-effort: si Sigrid falla para una obra, esos
registros NO se tocan (se reintentan en la siguiente persistencia).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from application.services import text_match as tm
from domain.models.sigrid_models import HmoRow, RecursoRow

logger = logging.getLogger(__name__)


def _ano_mes(fecha_int: int | None) -> tuple[int | None, int | None]:
    if not fecha_int:
        return None, None
    return fecha_int // 10000, (fecha_int // 100) % 100


def _upd(registro_id, recurso_ide, recurso_cif, hmo_ide, estado) -> dict:
    return {
        "registro_id": registro_id, "recurso_ide": recurso_ide,
        "recurso_cif": recurso_cif, "hmo_ide": hmo_ide,
        "parte_estado": estado,
    }


class RecursoConciliador:
    def __init__(
        self,
        *,
        repository,
        lookup,
        ttl_seconds: int = 600,
    ) -> None:
        # repository: SqlAlchemyParteRepository. lookup: SigridLookupPort
        # (fetch_recursos, fetch_hmo_obra).
        self._repository = repository
        self._lookup = lookup
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        # maestro de recursos: maps conide->ide y cif_norm->ide (+ timestamp)
        self._res_maps: tuple[float, dict[int, int], dict[str, int]] | None = None
        # hmo por obra: obra_ide -> (timestamp, {(reside,ano,mes): hmo_ide})
        self._hmo_cache: dict[int, tuple[float, dict[tuple, int]]] = {}

    # ----- recursos (maestro) ----- #
    def _recurso_maps(self) -> tuple[dict[int, int], dict[str, int]]:
        now = time.time()
        with self._lock:
            if self._res_maps is not None and (now - self._res_maps[0]) < self._ttl:
                return self._res_maps[1], self._res_maps[2]
        try:
            recursos: list[RecursoRow] = self._lookup.fetch_recursos()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recurso-concil] fallo leyendo recursos: %r", exc)
            return {}, {}   # fallback: solo emp.reside
        by_conide: dict[int, int] = {}
        by_cif: dict[str, int] = {}
        for r in recursos:
            if r.conide is not None:
                by_conide.setdefault(r.conide, r.ide)
            cifn = tm.normalize_dni(r.cif)
            if cifn:
                by_cif.setdefault(cifn, r.ide)
        with self._lock:
            self._res_maps = (now, by_conide, by_cif)
        logger.info(
            "[recurso-concil] recursos: por_empleado=%s por_dni=%s",
            len(by_conide), len(by_cif),
        )
        return by_conide, by_cif

    # ----- hmo por obra ----- #
    def _hmo_index(self, obra_ide: int) -> dict[tuple, int] | None:
        now = time.time()
        with self._lock:
            hit = self._hmo_cache.get(obra_ide)
            if hit is not None and (now - hit[0]) < self._ttl:
                return hit[1]
        try:
            filas: list[HmoRow] = self._lookup.fetch_hmo_obra(obra_ide)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[recurso-concil] fallo leyendo hmo obra=%s: %r", obra_ide, exc
            )
            return None
        idx: dict[tuple, int] = {}
        for h in filas:
            if h.reside is None or h.ano is None or h.mes is None:
                continue
            idx.setdefault((h.reside, h.ano, h.mes), h.ide)
        with self._lock:
            self._hmo_cache[obra_ide] = (now, idx)
        logger.info(
            "[recurso-concil] obra=%s hmo=%s partes indexados", obra_ide, len(idx)
        )
        return idx

    def _resuelve_recurso(
        self, reg: dict, by_conide: dict[int, int], by_cif: dict[str, int]
    ) -> int | None:
        reside = reg.get("empleado_reside")
        if reside:
            return reside
        emp_ide = reg.get("empleado_ide")
        if emp_ide is not None and emp_ide in by_conide:
            return by_conide[emp_ide]
        dni = tm.normalize_dni(reg.get("empleado_dni"))
        if dni and dni in by_cif:
            return by_cif[dni]
        return None

    def conciliar_todos(self) -> dict:
        registros = self._repository.fetch_registros_para_recurso()
        by_conide, by_cif = self._recurso_maps()

        por_obra: dict[int | None, list[dict]] = defaultdict(list)
        for r in registros:
            por_obra[r.get("obra_ide")].append(r)

        updates: list[dict] = []
        con_parte = 0
        for obra_ide, regs in por_obra.items():
            if not obra_ide:
                # Sin obra casada: no se puede localizar el parte.
                for r in regs:
                    ride = self._resuelve_recurso(r, by_conide, by_cif)
                    estado = "sin_parte" if ride else "sin_recurso"
                    updates.append(
                        _upd(r["registro_id"], ride, r.get("empleado_dni"),
                             None, estado)
                    )
                continue
            idx = self._hmo_index(obra_ide)
            if idx is None:
                continue  # Sigrid fallo: no tocar, se reintenta luego
            for r in regs:
                ride = self._resuelve_recurso(r, by_conide, by_cif)
                if not ride:
                    updates.append(
                        _upd(r["registro_id"], None, r.get("empleado_dni"),
                             None, "sin_recurso")
                    )
                    continue
                ano, mes = _ano_mes(r.get("fecha_int"))
                hmo_ide = idx.get((ride, ano, mes)) if ano and mes else None
                if hmo_ide:
                    con_parte += 1
                    estado = "ok"
                else:
                    estado = "sin_parte"
                updates.append(
                    _upd(r["registro_id"], ride, r.get("empleado_dni"),
                         hmo_ide, estado)
                )

        actualizados = self._repository.apply_recurso_matches(updates)
        logger.info(
            "[recurso-concil] registros=%s actualizados=%s con_parte=%s",
            len(registros), actualizados, con_parte,
        )
        return {
            "registros": len(registros),
            "actualizados": actualizados,
            "con_parte": con_parte,
        }

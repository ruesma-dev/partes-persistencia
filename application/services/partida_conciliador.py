# application/services/partida_conciliador.py
"""Conciliacion automatica de PARTIDAS para TODOS los partes (sv3).

Se lanza al persistir un parte: recorre todos los registros de partes
ACTIVOS, los agrupa por obra, lee las partidas del presupuesto de cada obra
(``obrparpar`` via ``sigrid-api``, cacheadas con TTL), y casa cada linea con
la PARTIDA segun la categoria del trabajador y, si aparece, su nombre.

Ambito de busqueda por categoria:
- Mando/indirectos (encargado, capataz, gruista, jefe...) -> capitulo CI.
- Oficiales/peones (varia) y desconocidos -> CI + CD.

Reglas:
- Si no hay candidata por encima del umbral -> ``metodo='sin'`` (en blanco).
- Se PRESERVAN los casados manuales (``metodo='manual'``): no se tocan.
- Best-effort: si Sigrid falla para una obra, esos registros NO se tocan
  (se reintentan en la siguiente persistencia); nunca rompe el persist.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from application.services.partida_catalog import (
    PartidaNodo,
    build_arbol_partidas,
    partidas_hoja,
)
from application.services.partida_matcher import (
    UMBRAL_ROL,
    ambito_categoria,
    match_partida,
)

logger = logging.getLogger(__name__)


def _sin(registro_id: int) -> dict:
    return {
        "registro_id": registro_id,
        "partida_ide": None, "partida_cod": None, "partida_res": None,
        "partida_capitulo": None, "partida_match_method": "sin",
        "partida_match_score": None,
    }


class PartidaConciliador:
    def __init__(
        self, *, repository, lookup,
        umbral: float = UMBRAL_ROL, ttl_seconds: int = 600,
    ) -> None:
        self._repository = repository
        self._lookup = lookup
        self._umbral = float(umbral)
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        # obra_ide -> (timestamp, {'CI': [...], 'CD': [...]})
        self._cache: dict[int, tuple[float, dict[str, list[PartidaNodo]]]] = {}

    def _partidas_obra(self, obra_ide: int) -> dict[str, list[PartidaNodo]] | None:
        """Partidas hoja por capitulo {'CI': [...], 'CD': [...]} (cacheado).
        ``None`` si Sigrid falla (no se cachea el fallo)."""
        now = time.time()
        with self._lock:
            hit = self._cache.get(obra_ide)
            if hit is not None and (now - hit[0]) < self._ttl:
                return hit[1]
        try:
            filas = self._lookup.fetch_partidas_obra(obra_ide)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[partida-concil] fallo leyendo partidas obra=%s: %r",
                obra_ide, exc,
            )
            return None
        nodos = build_arbol_partidas(filas)
        cat = {
            "CI": partidas_hoja(nodos, categoria="CI"),
            "CD": partidas_hoja(nodos, categoria="CD"),
        }
        with self._lock:
            self._cache[obra_ide] = (now, cat)
        logger.info(
            "[partida-concil] obra=%s partidas=%s CI=%s CD=%s",
            obra_ide, len(nodos), len(cat["CI"]), len(cat["CD"]),
        )
        return cat

    def conciliar_todos(self) -> dict:
        registros = self._repository.fetch_registros_para_partida()

        por_obra: dict[int | None, list[dict]] = defaultdict(list)
        for r in registros:
            if (r.get("partida_match_method") or "") == "manual":
                continue
            por_obra[r.get("obra_ide")].append(r)

        updates: list[dict] = []
        casados = 0
        for obra_ide, regs in por_obra.items():
            if not obra_ide:
                updates.extend(_sin(r["registro_id"]) for r in regs)
                continue
            cat = self._partidas_obra(obra_ide)
            if cat is None:
                continue  # Sigrid fallo: no tocar, se reintenta luego
            for r in regs:
                amb = ambito_categoria(r.get("categoria"))
                candidatas = list(cat["CI"])
                if "CD" in amb:
                    candidatas = candidatas + list(cat["CD"])
                m = match_partida(
                    r.get("categoria"), r.get("nombre"), candidatas,
                    umbral=self._umbral,
                )
                if m is None:
                    updates.append(_sin(r["registro_id"]))
                else:
                    casados += 1
                    updates.append({
                        "registro_id": r["registro_id"],
                        "partida_ide": m.partida.ide,
                        "partida_cod": m.partida.cod,
                        "partida_res": m.partida.res,
                        "partida_capitulo": m.partida.categoria,
                        "partida_match_method": m.metodo,
                        "partida_match_score": m.score,
                    })

        actualizados = self._repository.apply_partida_matches(updates)
        logger.info(
            "[partida-concil] registros=%s actualizados=%s casados=%s",
            len(registros), actualizados, casados,
        )
        return {
            "registros": len(registros),
            "actualizados": actualizados,
            "casados": casados,
        }

# application/services/partida_conciliador.py
"""Conciliacion automatica de PARTIDAS para TODOS los partes (sv3).

Se lanza al persistir un parte: recorre todos los registros de partes
ACTIVOS, los agrupa por obra, lee las partidas del presupuesto de cada obra
(``obrparpar`` via ``sigrid-api``, cacheadas con TTL), y casa cada linea con
la PARTIDA segun la categoria del trabajador y, si aparece, su nombre.

Con la plantilla J.310 rev. 1 la partida PUEDE venir escrita en el parte
(``partida`` leida por sv2, repartida por el normalizador). Prioridad:

1. Registro CON partida leida: se casa por CODIGO contra TODAS las hojas
   del presupuesto de la obra (CI y CD), ``metodo='parte'``. Si el codigo
   no existe en el presupuesto -> blanco (``'sin'``) + warning: se ve en
   el front como '⚠ ... no encontrada' y se corrige a mano.
2. Registro SIN partida leida: eleccion automatica por categoria/nombre,
   SOLO capitulo CI (costes indirectos); si no casa, queda en blanco.

Reglas:
- La automatica solo busca en CI. Si nada casa -> ``metodo='sin'``.
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
    match_partida,
)

logger = logging.getLogger(__name__)


def _norm_cod(cod: str | None) -> tuple[str, ...]:
    """Normaliza un codigo de partida a grupos comparables.

    '03.09' -> ('3','9'); '05.04.01' -> ('5','4','1'); tolera espacios,
    guiones y ceros a la izquierda. Grupos no numericos en mayusculas.
    """
    import re
    grupos = re.split(r"[^0-9A-Za-z]+", (cod or "").strip())
    out: list[str] = []
    for g in grupos:
        if not g:
            continue
        out.append(g.lstrip("0") or "0" if g.isdigit() else g.upper())
    return tuple(out)


def _match_codigo_leido(leida: str, hojas: list[PartidaNodo]) -> PartidaNodo | None:
    """Casa el codigo LEIDO del parte contra las hojas del presupuesto.

    Igualdad exacta de grupos normalizados; si no, PREFIJO unico (el
    encargado escribe '03.09' y la hoja es '03.09.01'). Ambiguo -> None.
    """
    objetivo = _norm_cod(leida)
    if not objetivo:
        return None
    exactas = [h for h in hojas if _norm_cod(h.cod) == objetivo]
    if len(exactas) == 1:
        return exactas[0]
    if len(exactas) > 1:
        return None
    prefijo = [
        h for h in hojas
        if _norm_cod(h.cod)[: len(objetivo)] == objetivo
    ]
    if len(prefijo) == 1:
        return prefijo[0]
    return None


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
                # 1) Partida LEIDA del parte: prioridad, casa por codigo
                #    contra TODAS las hojas (CI y CD).
                leida = (r.get("partida_leida") or "").strip()
                if leida:
                    nodo = _match_codigo_leido(
                        leida, list(cat["CI"]) + list(cat["CD"])
                    )
                    if nodo is None:
                        logger.warning(
                            "[partida-concil] registro=%s obra=%s: partida "
                            "leida %r NO encontrada (o ambigua) en el "
                            "presupuesto: queda en blanco, asignar a mano",
                            r["registro_id"], obra_ide, leida,
                        )
                        updates.append(_sin(r["registro_id"]))
                    else:
                        casados += 1
                        updates.append({
                            "registro_id": r["registro_id"],
                            "partida_ide": nodo.ide,
                            "partida_cod": nodo.cod,
                            "partida_res": nodo.res,
                            "partida_capitulo": nodo.categoria,
                            "partida_match_method": "parte",
                            "partida_match_score": 1.0,
                        })
                    continue

                # 2) Eleccion automatica: SOLO capitulo CI. Si no casa en CI,
                #    la linea queda en blanco (no se cae a CD).
                candidatas = list(cat["CI"])
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

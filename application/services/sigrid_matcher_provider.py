# application/services/sigrid_matcher_provider.py
"""Carga los maestros de Sigrid (empleados / obras / tipos de hora) y
construye los matchers, cacheandolos con un TTL.

Motivo: casar en cada ``persist`` re-descargando los 3 maestros seria
lento. Aqui se cargan una vez y se reutilizan, refrescando cuando el
cache supera ``ttl_seconds`` (asi un empleado/obra nuevo aparece sin
reiniciar el servicio). Si Sigrid falla durante un refresh, se conserva
el cache anterior (best-effort) y, si nunca se pudo cargar, los matchers
quedan vacios (el parte se persiste sin casar).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from application.services.empleado_matcher import EmpleadoMatcher
from application.services.obra_matcher import ObraMatcher
from application.services.tipo_hora_resolver import TipoHoraResolver
from domain.ports.sigrid_lookup_port import SigridLookupPort

logger = logging.getLogger(__name__)


@dataclass
class Matchers:
    empleado: EmpleadoMatcher
    obra: ObraMatcher
    tipo_hora: TipoHoraResolver


class SigridMatcherProvider:
    def __init__(
        self,
        *,
        lookup: SigridLookupPort,
        empleado_min_score: float,
        obra_min_score: float,
        default_hora_normal_cod: str | None,
        default_hora_extra_cod: str | None,
        incidencia_cod_map: dict[str, str] | None = None,
        ttl_seconds: int = 600,
    ) -> None:
        self._lookup = lookup
        self._empleado_min_score = empleado_min_score
        self._obra_min_score = obra_min_score
        self._default_normal = default_hora_normal_cod
        self._default_extra = default_hora_extra_cod
        self._inc_map = incidencia_cod_map or {}
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._matchers: Matchers | None = None
        self._loaded_at: float = 0.0

    def get(self) -> Matchers:
        with self._lock:
            now = time.time()
            fresh = (
                self._matchers is not None
                and (now - self._loaded_at) < self._ttl
            )
            if fresh:
                return self._matchers  # type: ignore[return-value]
            try:
                self._matchers = self._build()
                self._loaded_at = now
            except Exception:
                logger.exception(
                    "[matcher-provider] fallo cargando maestros de Sigrid."
                )
                if self._matchers is not None:
                    logger.warning(
                        "[matcher-provider] se reutiliza el cache anterior."
                    )
                    return self._matchers
                # Nunca se pudo cargar: matchers vacios (persist sin casar).
                self._matchers = self._empty()
                self._loaded_at = now
            return self._matchers

    def _build(self) -> Matchers:
        empleados = self._lookup.fetch_empleados()
        obras = self._lookup.fetch_obras()
        tipos = self._lookup.fetch_tipos_hora()
        logger.info(
            "[matcher-provider] maestros cargados: empleados=%s obras=%s "
            "tipos_hora=%s", len(empleados), len(obras), len(tipos),
        )
        return Matchers(
            empleado=EmpleadoMatcher(
                empleados=empleados, min_score=self._empleado_min_score
            ),
            obra=ObraMatcher(obras=obras, min_score=self._obra_min_score),
            tipo_hora=TipoHoraResolver(
                tipos_hora=tipos,
                default_normal_cod=self._default_normal,
                default_extra_cod=self._default_extra,
                incidencia_cod_map=self._inc_map,
            ),
        )

    def _empty(self) -> Matchers:
        return Matchers(
            empleado=EmpleadoMatcher(
                empleados=[], min_score=self._empleado_min_score
            ),
            obra=ObraMatcher(obras=[], min_score=self._obra_min_score),
            tipo_hora=TipoHoraResolver(
                tipos_hora=[],
                default_normal_cod=self._default_normal,
                default_extra_cod=self._default_extra,
                incidencia_cod_map=self._inc_map,
            ),
        )

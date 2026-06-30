# application/services/parte_normalizer.py
"""Convierte el ``data`` del envelope de sv2 (dict) en un ``ParteDocumento``
SIN casar todavia, EXPANDIENDO cada empleado en registros horarios:

  - una fila de horas ORDINARIAS  -> registro tipo_hora="normal"
  - una fila de horas EXTRAORDIN.  -> registro tipo_hora="extra"
  - una INCIDENCIA (V/B/AT/...)     -> registro es_incidencia=True

El casado (empleado / obra / codigo de hora) lo aplica el pipeline despues.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from domain.models.parte_records import (
    IncidenciaInfo,
    ParteDocumento,
    RegistroNormalizado,
)
from application.services.fecha_resolver import FechaParteResolver

logger = logging.getLogger(__name__)


_INCIDENCIA_CODES = {"V", "B", "AT", "FJ", "F", "H", "M"}


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        s = value.strip().replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_incidencia_codigo(value: Any) -> str | None:
    s = _opt_str(value)
    if not s:
        return None
    up = s.strip().upper()
    if up in _INCIDENCIA_CODES:
        return up
    # Tolerancia minima por si la IA devuelve el codigo en minusculas o con
    # puntos ('a.t.', 'f.j.').
    cleaned = re.sub(r"[^A-Z]", "", up)
    return cleaned if cleaned in _INCIDENCIA_CODES else up


class ParteNormalizer:
    def __init__(
        self,
        *,
        fecha_resolver: FechaParteResolver | None = None,
        jornada_ordinaria_horas: float = 8.0,
    ) -> None:
        self._fecha_resolver = fecha_resolver or FechaParteResolver()
        # Jornada ordinaria estandar: lo que exceda pasa a extra (determinista).
        # Configurable para preparar la jornada de verano.
        self._jornada = float(jornada_ordinaria_horas)

    def normalize(
        self,
        data: dict[str, Any],
        *,
        email_text: str | None = None,
    ) -> ParteDocumento:
        cabecera = data.get("cabecera") or {}
        firma = data.get("firma") or {}
        empleados_raw = data.get("empleados") or []
        if not isinstance(cabecera, dict):
            cabecera = {}
        if not isinstance(firma, dict):
            firma = {}
        if not isinstance(empleados_raw, list):
            empleados_raw = []

        # Fecha resuelta de forma determinista (dia del parte + mes del email,
        # anio 2026+). No se confia en la conversion de la IA.
        fecha = self._fecha_resolver.resolve(
            raw_fecha=_opt_str(cabecera.get("fecha")),
            email_text=email_text,
        )

        firma_enc = bool(firma.get("firma_encargado", False))
        firma_jo = bool(firma.get("firma_jefe_obra", False))
        firma_adm = bool(firma.get("firma_administracion", False))
        firmado = bool(firma.get("firmado", False)) or firma_enc or firma_jo or firma_adm

        parte = ParteDocumento(
            fecha_iso=fecha.iso,
            fecha_int=fecha.fecha_int,
            obra_numero_leido=_opt_str(cabecera.get("obra_numero")),
            obra_nombre_leido=_opt_str(cabecera.get("obra_nombre")),
            encargado_nombre=_opt_str(cabecera.get("encargado_nombre")),
            jefe_obra_nombre=_opt_str(cabecera.get("jefe_obra_nombre")),
            firmado=firmado,
            firma_encargado=firma_enc,
            firma_jefe_obra=firma_jo,
            firma_administracion=firma_adm,
            firmante_rol=_opt_str(firma.get("firmante_rol")),
            firmante_nombre=_opt_str(firma.get("firmante_nombre")),
            firma_confianza_pct=_opt_float(firma.get("confianza_pct")),
        )

        line_index = 0
        for emp in empleados_raw:
            if not isinstance(emp, dict):
                continue
            nombre = _opt_str(emp.get("nombre"))
            categoria = _opt_str(emp.get("categoria"))
            numero_linea = _opt_int(emp.get("numero_linea"))
            confianza = _opt_float(emp.get("confianza_pct"))
            horas_ord = _opt_float(emp.get("horas_ordinarias"))
            horas_extra = _opt_float(emp.get("horas_extraordinarias"))
            cod_ord = _opt_str(emp.get("codigo_hora_ordinaria"))
            cod_extra = _opt_str(emp.get("codigo_hora_extra"))

            inc_raw = emp.get("incidencia")
            incidencia: IncidenciaInfo | None = None
            inc_cod_sigrid: str | None = None
            if isinstance(inc_raw, dict):
                cod = _norm_incidencia_codigo(inc_raw.get("codigo"))
                texto = _opt_str(inc_raw.get("texto_leido"))
                dias = _opt_float(inc_raw.get("dias"))
                inc_cod_sigrid = _opt_str(inc_raw.get("codigo_sigrid"))
                if cod or texto:
                    incidencia = IncidenciaInfo(
                        codigo=cod, texto_leido=texto, dias=dias
                    )

            # Empleado sin nada util: lo saltamos.
            if (
                not nombre
                and not horas_ord
                and not horas_extra
                and incidencia is None
            ):
                continue

            base = dict(
                empleado_line_no=numero_linea,
                categoria=categoria,
                trabajador_nombre_leido=nombre,
                confianza_pct=confianza,
            )

            # El split por jornada se hace ahora en sv3 tras la conciliacion
            # de recurso (con el CanDefecto real del recurso y la vision del
            # dia completo across obras). Aqui se respetan las horas tal cual:
            # las ordinarias del parte como normal y las extra EXPLICITAS como
            # extra. (self._jornada queda sin uso; ver recurso_conciliador.)
            ord_eff = horas_ord
            extra_eff = horas_extra

            if ord_eff and ord_eff > 0:
                parte.registros.append(
                    RegistroNormalizado(
                        line_index=line_index,
                        tipo_hora="normal",
                        es_incidencia=False,
                        horas=ord_eff,
                        codigo_hora_propuesto=cod_ord,
                        **base,
                    )
                )
                line_index += 1

            if extra_eff and extra_eff > 0:
                parte.registros.append(
                    RegistroNormalizado(
                        line_index=line_index,
                        tipo_hora="extra",
                        es_incidencia=False,
                        horas=extra_eff,
                        codigo_hora_propuesto=cod_extra,
                        **base,
                    )
                )
                line_index += 1

            if incidencia is not None and (
                incidencia.codigo or incidencia.texto_leido
            ):
                parte.registros.append(
                    RegistroNormalizado(
                        line_index=line_index,
                        tipo_hora=incidencia.codigo or "incidencia",
                        es_incidencia=True,
                        incidencia=incidencia,
                        horas=incidencia.dias,
                        codigo_hora_propuesto=inc_cod_sigrid,
                        **base,
                    )
                )
                line_index += 1

        logger.info(
            "[parte-normalizer] fecha=%s obra=%r empleados=%s registros=%s "
            "firmado=%s",
            parte.fecha_iso,
            parte.obra_numero_leido,
            sum(1 for _ in {r.trabajador_nombre_leido for r in parte.registros}),
            len(parte.registros),
            parte.firmado,
        )
        return parte

# infrastructure/sigrid/sigrid_api_client.py
"""Cliente HTTP de SOLO LECTURA contra la Function App ``sigrid-api``.

Sirve los tres lookups que el sv3 necesita para casar un parte:
  - ``fetch_empleados``   -> tabla ``emp`` (extiende ``con``)
  - ``fetch_obras``       -> tabla ``obr`` (extiende ``con``)
  - ``fetch_tipos_hora``  -> tabla ``auxhor`` (codigo de hora)

Realiza POST a ``/api/sql/read`` con cabecera ``x-functions-key`` y
cuerpo ``{database, sql, parameters, timeout_seconds, max_rows}``. La
respuesta es ``{ok, columns, rows, row_count}``.

Cumple el puerto ``SigridLookupPort``. Best-effort: ante error de Sigrid
las llamadas lanzan, pero el pipeline las captura y degrada a "sin
casar" (los lookups solo se invocan si Sigrid esta cableado).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from domain.models.sigrid_models import EmpleadoRow, ObraRow, TipoHoraRow

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[sigrid-client]"


# Empleados: emp extiende con (emp.ide = con.ide). Codigo en con.cod,
# nombre completo en emp.res, DNI en emp.dni, Recurso en emp.reside.
# Filtramos por empresa (con.emp) si SIGRID_EMPRESA > 0.
_SQL_EMPLEADOS_BASE = """\
SELECT
    con.ide    AS ide,
    con.cod    AS codigo,
    emp.res    AS nombre,
    emp.dni    AS dni,
    emp.reside AS reside
FROM emp
JOIN con ON emp.ide = con.ide
"""

# Obras: obr extiende con. Sin DISTINCT (obr.res es text/ntext y SQL
# Server no permite DISTINCT sobre ese tipo). Deduplicamos por codigo en
# Python.
_SQL_OBRAS = """\
SELECT
    con.ide AS ide,
    con.cod AS codigo,
    obr.res AS nombre
FROM obr
JOIN con ON obr.ide = con.ide
WHERE con.cod IS NOT NULL
ORDER BY con.cod
"""

# Tipos de hora (auxhor). fecbaj=0 (o NULL) => activo. ext distingue
# normal(0)/extra(1).
_SQL_TIPOS_HORA = """\
SELECT
    auxhor.ide    AS ide,
    auxhor.cod    AS codigo,
    auxhor.res    AS descripcion,
    auxhor.ext    AS ext,
    auxhor.pre    AS pre,
    auxhor.prenom AS prenom
FROM auxhor
WHERE (auxhor.fecbaj IS NULL OR auxhor.fecbaj = 0)
ORDER BY auxhor.ext, auxhor.cod
"""


class SigridApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        empresa: int = 1,
        timeout_s: float = 30.0,
        max_rows: int = 10000,
    ) -> None:
        if not base_url:
            raise ValueError("SigridApiClient requiere base_url no vacio")
        if not function_key:
            raise ValueError("SigridApiClient requiere function_key no vacio")
        if not database:
            raise ValueError("SigridApiClient requiere database no vacio")
        self._base_url = base_url.rstrip("/")
        self._function_key = function_key
        self._database = database
        self._empresa = int(empresa)
        self._timeout_s = float(timeout_s)
        self._max_rows = int(max_rows)
        logger.info(
            "%s Instanciado. base_url=%s database=%s empresa=%s timeout_s=%s "
            "max_rows=%s key_len=%s",
            _LOG_PREFIX,
            self._base_url,
            self._database,
            self._empresa,
            self._timeout_s,
            self._max_rows,
            len(function_key),
        )

    # ----------------------------------------------------------------- #
    # Lookups publicos.
    # ----------------------------------------------------------------- #
    def fetch_empleados(self) -> list[EmpleadoRow]:
        if self._empresa > 0:
            sql = _SQL_EMPLEADOS_BASE + "WHERE con.emp = ?\nORDER BY con.cod\n"
            params: list[Any] = [self._empresa]
        else:
            sql = _SQL_EMPLEADOS_BASE + "ORDER BY con.cod\n"
            params = []
        columns, rows = self._post_sql_read(
            sql=sql, parameters=params, label="empleados"
        )
        out: list[EmpleadoRow] = []
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            out.append(
                EmpleadoRow(
                    ide=ide,
                    codigo=_opt_str(rm.get("codigo")),
                    nombre=_opt_str(rm.get("nombre")),
                    dni=_opt_str(rm.get("dni")),
                    reside=_opt_int(rm.get("reside")),
                )
            )
        logger.info("%s empleados -> %s filas", _LOG_PREFIX, len(out))
        return out

    def fetch_obras(self) -> list[ObraRow]:
        columns, rows = self._post_sql_read(
            sql=_SQL_OBRAS, parameters=[], label="obras"
        )
        seen: set[str] = set()
        out: list[ObraRow] = []
        for row in rows:
            rm = dict(zip(columns, row))
            cod = _opt_str(rm.get("codigo"))
            ide = _opt_int(rm.get("ide"))
            if not cod or ide is None or cod in seen:
                continue
            seen.add(cod)
            out.append(
                ObraRow(ide=ide, codigo=cod, nombre=_opt_str(rm.get("nombre")))
            )
        logger.info("%s obras -> %s filas", _LOG_PREFIX, len(out))
        return out

    def fetch_tipos_hora(self) -> list[TipoHoraRow]:
        columns, rows = self._post_sql_read(
            sql=_SQL_TIPOS_HORA, parameters=[], label="tipos_hora"
        )
        out: list[TipoHoraRow] = []
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            out.append(
                TipoHoraRow(
                    ide=ide,
                    codigo=_opt_str(rm.get("codigo")),
                    descripcion=_opt_str(rm.get("descripcion")),
                    ext=_opt_int(rm.get("ext")) or 0,
                    pre=_opt_float(rm.get("pre")),
                    prenom=_opt_float(rm.get("prenom")),
                )
            )
        logger.info("%s tipos_hora -> %s filas", _LOG_PREFIX, len(out))
        return out

    # ----------------------------------------------------------------- #
    # HTTP primitive.
    # ----------------------------------------------------------------- #
    def _post_sql_read(
        self,
        *,
        sql: str,
        parameters: list[Any],
        label: str,
    ) -> tuple[list[str], list[list[Any]]]:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": self._database,
            "sql": sql,
            "parameters": parameters,
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }
        logger.info(
            "%s REQUEST [%s] -> POST %s db=%s params=%s",
            _LOG_PREFIX, label, url, self._database, parameters,
        )
        transport = httpx.HTTPTransport(retries=1)
        try:
            with httpx.Client(timeout=self._timeout_s, transport=transport) as client:
                response = client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception(
                "%s FALLO de transporte [%s]. exc=%r", _LOG_PREFIX, label, exc
            )
            raise

        status = response.status_code
        body_text = response.text or ""
        if status >= 400:
            logger.warning(
                "%s RESPONSE [%s] status=%s preview=%s",
                _LOG_PREFIX, label, status, body_text[:300],
            )
            raise RuntimeError(f"sigrid-api respondio {status}: {body_text[:300]}")

        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"sigrid-api respuesta no JSON: {body_text[:300]}"
            ) from exc

        if not body.get("ok", False):
            raise RuntimeError(f"sigrid-api devolvio ok=false: {body!r}")

        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        logger.info(
            "%s RESPONSE [%s] <- %s filas", _LOG_PREFIX, label, len(rows)
        )
        return columns, rows


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
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

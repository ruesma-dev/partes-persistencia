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
import re
import threading
import time
from collections import defaultdict

from application.services import text_match as tm
from domain.models.sigrid_models import HmoRow, RecursoRow
from domain.ports.calendario_laboral_port import CalendarioLaboralPort

logger = logging.getLogger(__name__)


def _ano_mes(fecha_int: int | None) -> tuple[int | None, int | None]:
    if not fecha_int:
        return None, None
    return fecha_int // 10000, (fecha_int // 100) % 100


_INCID_TIPOS = {"V", "B", "AT", "FJ", "F", "H", "M"}


def _tipo_inc(res: str | None) -> str | None:
    """Tipo de incidencia (V|B|AT|FJ|F|H|M) leido del parentesis final de la
    descripcion de un codigo de hora, p.ej. 'Vacaciones(V)' -> 'V',
    'Accidente/Enf. Profesional(AT)' -> 'AT'. None si no es una incidencia."""
    if not res:
        return None
    m = re.search(r"\(([A-Za-z]{1,3})\)\s*$", res)
    if not m:
        return None
    t = m.group(1).strip().upper()
    return t if t in _INCID_TIPOS else None


def _tipo(reg: dict) -> str:
    return (reg.get("tipo_hora") or "").strip().lower()


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
        calendario: CalendarioLaboralPort | None = None,
        jornada_ordinaria_horas: float = 8.0,
        ttl_seconds: int = 600,
    ) -> None:
        # repository: SqlAlchemyParteRepository. lookup: SigridLookupPort
        # (fetch_recursos, fetch_reshor, fetch_hmo_obra).
        self._repository = repository
        self._lookup = lookup
        # Calendario laboral (fin de semana + festivos). Si es None, no
        # se aplica la regla de no laborable (comportamiento anterior).
        self._calendario = calendario
        # Jornada ordinaria por defecto cuando el recurso no informa el
        # CanDefecto (candef vacio): se resta esta cantidad y el resto va
        # a extra.
        self._jornada = float(jornada_ordinaria_horas)
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        # maestro de recursos: maps conide->ide, cif_norm->ide, ide->RecursoRow
        # (para la clasificacion/categoria y la hora por defecto) + timestamp.
        self._res_maps: tuple[
            float, dict[int, int], dict[str, int], dict[int, RecursoRow]
        ] | None = None
        # costes de horas por recurso: reside -> {ord, ext, candef} (+ ts).
        self._reshor_cache: tuple[float, dict[int, dict]] | None = None
        # hmo por obra: obra_ide -> (timestamp, {(reside,ano,mes): hmo_ide})
        self._hmo_cache: dict[int, tuple[float, dict[tuple, int]]] = {}

    # ----- recursos (maestro) ----- #
    def _recurso_maps(
        self,
    ) -> tuple[dict[int, int], dict[str, int], dict[int, RecursoRow]]:
        now = time.time()
        with self._lock:
            if self._res_maps is not None and (now - self._res_maps[0]) < self._ttl:
                return self._res_maps[1], self._res_maps[2], self._res_maps[3]
        try:
            recursos: list[RecursoRow] = self._lookup.fetch_recursos()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recurso-concil] fallo leyendo recursos: %r", exc)
            return {}, {}, {}   # fallback: solo emp.reside
        by_conide: dict[int, int] = {}
        by_cif: dict[str, int] = {}
        by_ide: dict[int, RecursoRow] = {}
        for r in recursos:
            by_ide[r.ide] = r
            if r.conide is not None:
                by_conide.setdefault(r.conide, r.ide)
            cifn = tm.normalize_dni(r.cif)
            if cifn:
                by_cif.setdefault(cifn, r.ide)
        with self._lock:
            self._res_maps = (now, by_conide, by_cif, by_ide)
        logger.info(
            "[recurso-concil] recursos: por_empleado=%s por_dni=%s total=%s",
            len(by_conide), len(by_cif), len(by_ide),
        )
        return by_conide, by_cif, by_ide

    # ----- costes de horas por recurso (reshor) ----- #
    def _reshor_index(self, by_ide: dict[int, RecursoRow]) -> dict[int, dict]:
        """reside -> {'ord': ReshorRow|None, 'ext': ReshorRow|None,
        'candef': float|None}. ``ord`` es la hora LABORABLE (la hora por
        defecto del recurso, ``res.horide``; si falta, la primera ext=0);
        ``ext`` la primera hora extra; ``candef`` la cantidad por defecto de
        la hora laborable (jornada por defecto)."""
        now = time.time()
        with self._lock:
            if self._reshor_cache is not None and (
                now - self._reshor_cache[0]
            ) < self._ttl:
                return self._reshor_cache[1]
        try:
            filas = self._lookup.fetch_reshor()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recurso-concil] fallo leyendo reshor: %r", exc)
            return {}   # sin reshor: no se pisa la hora (solo categoria)
        por_reside: dict[int, list] = defaultdict(list)
        for f in filas:
            por_reside[f.reside].append(f)
        idx: dict[int, dict] = {}
        for reside, lst in por_reside.items():
            rec = by_ide.get(reside)
            horide_def = rec.horide_def if rec is not None else None
            # Separar el grupo del recurso en INCIDENCIAS (por el (TIPO) de la
            # descripcion) y horas de TRABAJO (el resto; los codigos de
            # incidencia CIx que no mapean a un tipo de parte, p.ej. CIZ, se
            # descartan).
            incidencias: dict[str, object] = {}
            trabajo: list = []
            for f in lst:
                t = _tipo_inc(f.res)
                cod = (f.cod or "").upper()
                if t:
                    incidencias.setdefault(t, f)
                elif cod.startswith("CI"):
                    continue
                else:
                    trabajo.append(f)
            # Ordinaria: hora por defecto del recurso; si falta, la primera
            # no-extra; si no, la primera de trabajo.
            ordinaria = None
            if horide_def is not None:
                ordinaria = next(
                    (f for f in trabajo if f.horide == horide_def), None
                )
            if ordinaria is None:
                ordinaria = next((f for f in trabajo if f.ext == 0), None)
            if ordinaria is None and trabajo:
                ordinaria = trabajo[0]
            # Extra: por el flag ext=1; si el flag no la marca (Sigrid no
            # siempre lo informa), por la palabra "extra" en la descripcion;
            # como ultimo recurso, la hora de trabajo distinta de la ordinaria.
            extra = next((f for f in trabajo if f.ext == 1), None)
            if extra is None:
                extra = next(
                    (f for f in trabajo
                     if "extra" in tm.normalize(f.res or "")), None
                )
            if extra is None and ordinaria is not None:
                extra = next(
                    (f for f in trabajo if f.horide != ordinaria.horide), None
                )
            idx[reside] = {
                "ord": ordinaria,
                "ext": extra,
                "candef": ordinaria.candef if ordinaria is not None else None,
                "incidencias": incidencias,
            }
        with self._lock:
            self._reshor_cache = (now, idx)
        logger.info("[recurso-concil] reshor: %s recursos con horas", len(idx))
        return idx

    # ----- pisado de categoria + codigo de hora desde el recurso ----- #
    def _overwrite(
        self,
        reg: dict,
        ride: int,
        by_ide: dict[int, RecursoRow],
        reshor_idx: dict[int, dict],
    ) -> dict:
        """Campos que el RECURSO impone sobre el registro al casar: categoria
        (clasificacion del recurso), codigo de hora (su hora laborable/extra)
        y la jornada por defecto (candef). Devuelve solo las claves a pisar;
        las incidencias NO pisan la hora (D2). Emite warning si el codigo de
        hora impuesto difiere del que ya tenia el registro."""
        out: dict = {}
        rec = by_ide.get(ride)
        if rec is not None and rec.restip_res:
            out["categoria"] = rec.restip_res
        hsel = reshor_idx.get(ride)
        if not hsel:
            return out
        if hsel.get("candef") is not None:
            out["hora_candef"] = hsel["candef"]
        ord_row = hsel.get("ord")
        if ord_row is not None and ord_row.pre is not None:
            out["recurso_precio_hora"] = ord_row.pre
        tipo = (reg.get("tipo_hora") or "").strip().lower()
        if tipo not in ("", "normal", "extra"):
            # Incidencia (V|B|AT|FJ|F|H|M): coge SU codigo del grupo del
            # recurso (reshor), nunca la ordinaria. Si el recurso no tiene ese
            # codigo de incidencia, no se pisa (se mantiene el resuelto).
            hora = (hsel.get("incidencias") or {}).get(tipo.upper())
        elif tipo == "extra":
            hora = hsel.get("ext") or hsel.get("ord")  # D3: sin extra -> normal
        else:
            hora = hsel.get("ord")
        if hora is None:
            return out
        prev_ide = reg.get("hora_ide")
        if prev_ide is not None and prev_ide != hora.horide:
            logger.warning(
                "[recurso-concil] DIFERENCIA codigo de hora en registro=%s "
                "recurso=%s: previo=%s/%s -> recurso=%s/%s "
                "(categoria leida=%r recurso=%r)",
                reg.get("registro_id"), ride, prev_ide, reg.get("hora_codigo"),
                hora.horide, hora.cod, reg.get("categoria"),
                out.get("categoria"),
            )
            metodo = "recurso_difiere"
        else:
            metodo = "recurso"
        out["hora_ide"] = hora.horide
        out["hora_codigo"] = hora.cod
        out["hora_descripcion"] = hora.res
        out["hora_ext"] = hora.ext
        out["hora_match_method"] = metodo
        return out


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
        # Idempotencia: revertir las extras por jornada de pasadas anteriores
        # (restaurar horas originales y borrar los extra auto) para recalcular
        # el dia completo desde el estado original del parte.
        self._repository.revert_extras_auto()

        registros = self._repository.fetch_registros_para_recurso()
        by_conide, by_cif, by_ide = self._recurso_maps()
        reshor_idx = self._reshor_index(by_ide)

        por_obra: dict[int | None, list[dict]] = defaultdict(list)
        for r in registros:
            por_obra[r.get("obra_ide")].append(r)

        ride_por_reg: dict[int, int] = {}
        updates: list[dict] = []
        con_parte = 0
        pisados = 0
        for obra_ide, regs in por_obra.items():
            if not obra_ide:
                # Sin obra casada: no se puede localizar el parte, pero si el
                # recurso se resuelve igualmente se pisa categoria/hora.
                for r in regs:
                    ride = self._resuelve_recurso(r, by_conide, by_cif)
                    estado = "sin_parte" if ride else "sin_recurso"
                    u = _upd(r["registro_id"], ride, r.get("empleado_dni"),
                             None, estado)
                    if ride:
                        ride_por_reg[r["registro_id"]] = ride
                        ov = self._overwrite(r, ride, by_ide, reshor_idx)
                        if ov:
                            pisados += 1
                        u.update(ov)
                    updates.append(u)
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
                ride_por_reg[r["registro_id"]] = ride
                ano, mes = _ano_mes(r.get("fecha_int"))
                hmo_ide = idx.get((ride, ano, mes)) if ano and mes else None
                if hmo_ide:
                    con_parte += 1
                    estado = "ok"
                else:
                    estado = "sin_parte"
                u = _upd(r["registro_id"], ride, r.get("empleado_dni"),
                         hmo_ide, estado)
                ov = self._overwrite(r, ride, by_ide, reshor_idx)
                if ov:
                    pisados += 1
                u.update(ov)
                updates.append(u)

        actualizados = self._repository.apply_recurso_matches(updates)

        # Segunda pasada: pasar a extra el exceso de horas ORDINARIAS sobre el
        # CanDefecto del recurso, por (recurso, dia) across obras.
        splits = self._reclasificar_extras_jornada(
            registros, ride_por_reg, reshor_idx
        )
        reclasificadas = self._repository.apply_extras_splits(splits)
        logger.info(
            "[recurso-concil] registros=%s actualizados=%s con_parte=%s "
            "pisados=%s extras_reclasificadas=%s",
            len(registros), actualizados, con_parte, pisados, reclasificadas,
        )
        return {
            "registros": len(registros),
            "actualizados": actualizados,
            "con_parte": con_parte,
            "pisados": pisados,
            "extras_reclasificadas": reclasificadas,
        }

    # ----- exceso de jornada -> extra (por recurso y dia, across obras) ----- #
    def _reclasificar_extras_jornada(
        self,
        registros: list[dict],
        ride_por_reg: dict[int, int],
        reshor_idx: dict[int, dict],
    ) -> list[dict]:
        """Por (recurso, dia) reuniendo TODAS las obras: si NO hay extra
        EXPLICITA y la suma de horas ordinarias supera el CanDefecto del
        recurso, pasa el exceso a extra empezando por los registros de mayor
        id (ultimas obras creadas), partiendo el registro donde caiga el
        corte. Solo calcula los 'splits'; la BD la toca el repositorio."""
        grupos: dict[tuple[int, int | None], list[dict]] = defaultdict(list)
        for r in registros:
            ride = ride_por_reg.get(r["registro_id"])
            if ride is None:
                continue
            grupos[(ride, r.get("fecha_int"))].append(r)

        splits: list[dict] = []
        for (ride, fecha_int), regs in grupos.items():
            hsel = reshor_idx.get(ride)
            if not hsel:
                continue
            hora_ext = hsel.get("ext")
            if hora_ext is None:
                continue  # sin hora extra del recurso: no se puede crear

            if self._es_no_laborable(fecha_int, regs):
                # Fin de semana / festivo: NO hay jornada ordinaria, TODO
                # el trabajo pasa a extra (aunque ya haya extra explicita).
                candef_efectivo = 0.0
                logger.info(
                    "[recurso-concil] dia NO laborable %s (recurso=%s): "
                    "horas ordinarias -> extra.",
                    self._fecha_int_to_iso(fecha_int), ride,
                )
            else:
                # D-B: si ya hay extra EXPLICITA ese dia, respetar desglose.
                if any(_tipo(x) == "extra" for x in regs):
                    continue
                candef = hsel.get("candef")
                # Si el recurso no informa CanDefecto, jornada por defecto.
                candef_efectivo = (
                    float(candef) if candef is not None else self._jornada
                )

            ordinarios = [x for x in regs if _tipo(x) in ("", "normal")]
            total = sum((x.get("horas") or 0.0) for x in ordinarios)
            if total <= candef_efectivo + 1e-9:
                continue
            restante = total - candef_efectivo
            # Las ultimas horas del dia (registros creados despues = id mayor).
            for x in sorted(
                ordinarios, key=lambda z: z["registro_id"], reverse=True
            ):
                if restante <= 1e-9:
                    break
                h = x.get("horas") or 0.0
                if h <= 0:
                    continue
                porcion = h if h <= restante + 1e-9 else restante
                splits.append({
                    "normal_id": x["registro_id"],
                    "horas_norm": round(h - porcion, 2),
                    "horas_orig": h,
                    "extra_horas": round(porcion, 2),
                    "hora_ext_ide": hora_ext.horide,
                    "hora_ext_cod": hora_ext.cod,
                    "hora_ext_desc": hora_ext.res,
                    "hora_ext_ext": hora_ext.ext,
                    "hora_candef": candef_efectivo,
                })
                restante -= porcion
        return splits

    # ----- calendario laboral (fin de semana / festivos) ----- #
    def _es_no_laborable(
        self, fecha_int: int | None, regs: list[dict]
    ) -> bool:
        """True si el dia es fin de semana o festivo segun el calendario.
        Sin calendario cableado o sin fecha valida, devuelve False."""
        if self._calendario is None:
            return False
        iso = self._fecha_int_to_iso(fecha_int)
        if iso is None:
            return False
        dni = next(
            (r.get("empleado_dni") for r in regs if r.get("empleado_dni")),
            None,
        )
        try:
            return bool(self._calendario.es_no_laborable(iso, dni=dni))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[recurso-concil] calendario laboral fallo en %s: %r",
                iso, exc,
            )
            return False

    @staticmethod
    def _fecha_int_to_iso(fecha_int: int | None) -> str | None:
        try:
            fi = int(fecha_int)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if fi <= 0:
            return None
        y, m, d = fi // 10000, (fi // 100) % 100, fi % 100
        if not (1 <= m <= 12 and 1 <= d <= 31):
            return None
        return f"{y:04d}-{m:02d}-{d:02d}"

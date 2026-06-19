# application/services/tipo_hora_resolver.py
"""Resuelve el CODIGO DE HORA de Sigrid (``auxhor``) para un registro.

Prioridad:
  1. CODIGO PROPUESTO por la IA (del catalogo inyectado en sv2) o elegido a
     mano: si existe en el maestro ``auxhor``, manda.
  2. INCIDENCIA sin codigo propuesto: se intenta casar por el codigo de
     incidencia (o alias), y como ultimo recurso por la descripcion del
     ``auxhor`` que termina en la letra de la incidencia entre parentesis,
     p.ej. "... Vacaciones(V)".
  3. HORAS NORMALES/EXTRA: por ``auxhor.ext`` (extra=1, normal=0), con el
     codigo por defecto configurado o el primer activo de ese flag.
"""
from __future__ import annotations

import logging
import re

from application.services import text_match as tm
from domain.models.parte_records import TipoHoraMatch
from domain.models.sigrid_models import TipoHoraRow

logger = logging.getLogger(__name__)


class TipoHoraResolver:
    def __init__(
        self,
        *,
        tipos_hora: list[TipoHoraRow],
        default_normal_cod: str | None = None,
        default_extra_cod: str | None = None,
        incidencia_cod_map: dict[str, str] | None = None,
    ) -> None:
        self._tipos = tipos_hora
        self._by_codigo: dict[str, TipoHoraRow] = {}
        self._by_ext: dict[int, list[TipoHoraRow]] = {0: [], 1: []}
        # Indice por letra de incidencia presente como sufijo "(X)" en la
        # descripcion (V, B, AT, FJ, F, H, M).
        self._by_inc_suffix: dict[str, TipoHoraRow] = {}
        for t in tipos_hora:
            cod_n = tm.normalize_code(t.codigo)
            if cod_n:
                self._by_codigo.setdefault(cod_n, t)
            self._by_ext.setdefault(int(t.ext), []).append(t)
            for letter in _desc_suffix_letters(t.descripcion):
                self._by_inc_suffix.setdefault(letter, t)

        self._inc_map = {
            tm.normalize_code(k): tm.normalize_code(v)
            for k, v in (incidencia_cod_map or {}).items()
            if k and v
        }

        self._default: dict[int, TipoHoraRow | None] = {0: None, 1: None}
        self._default[0] = self._resolve_default(default_normal_cod, ext=0)
        self._default[1] = self._resolve_default(default_extra_cod, ext=1)
        logger.info(
            "[tipo-hora-resolver] %s tipos (normal=%s extra=%s) "
            "inc_suffix=%s default_normal=%s default_extra=%s inc_map=%s",
            len(tipos_hora),
            len(self._by_ext.get(0, [])),
            len(self._by_ext.get(1, [])),
            sorted(self._by_inc_suffix.keys()),
            self._default[0].codigo if self._default[0] else None,
            self._default[1].codigo if self._default[1] else None,
            self._inc_map or {},
        )

    def _resolve_default(
        self, cod: str | None, *, ext: int
    ) -> TipoHoraRow | None:
        cod_n = tm.normalize_code(cod)
        if cod_n and cod_n in self._by_codigo:
            row = self._by_codigo[cod_n]
            if int(row.ext) == ext:
                return row
            logger.warning(
                "[tipo-hora-resolver] default cod=%s tiene ext=%s, se pidio "
                "ext=%s; se ignora.", cod, row.ext, ext,
            )
        return None

    def resolve(
        self,
        *,
        tipo_hora: str | None,
        codigo_hora_leido: str | None = None,
        incidencia_codigo: str | None = None,
        categoria: str | None = None,
        categoria_ref_desc: str | None = None,
    ) -> TipoHoraMatch:
        # 1) INCIDENCIA (los registros de incidencia no dependen de categoria).
        if incidencia_codigo:
            inc_n = tm.normalize_code(incidencia_codigo)
            target = self._inc_map.get(inc_n, inc_n)
            cod_n = tm.normalize_code(codigo_hora_leido)
            if cod_n and cod_n in self._by_codigo:
                return self._to_match(self._by_codigo[cod_n], "codigo_propuesto")
            if target in self._by_codigo:
                return self._to_match(self._by_codigo[target], "incidencia")
            if inc_n in self._by_inc_suffix:
                return self._to_match(
                    self._by_inc_suffix[inc_n], "incidencia_desc"
                )
            return TipoHoraMatch(method="none")

        extra = tm.normalize(tipo_hora) == "extra"

        # 2) MATCHEO DETERMINISTA por CATEGORIA + tipo (primario). Casa la
        # categoria del empleado con el codigo "HORA EXTRA <cat>" / "HORA
        # LABORABLE <cat>". Si la categoria no tiene codigo extra (p.ej.
        # encargado), usa su codigo normal. Esto revisa/lidera sobre la IA.
        if categoria:
            det = self.resolve_by_categoria(categoria=categoria, extra=extra)
            if det is not None:
                row, method = det
                return self._to_match(row, method)

        # 3) Codigo PROPUESTO por la IA (o elegido a mano) si el determinista
        # no resolvio (categoria desconocida o sin codigo en el catalogo).
        cod_n = tm.normalize_code(codigo_hora_leido)
        if cod_n and cod_n in self._by_codigo:
            return self._to_match(self._by_codigo[cod_n], "codigo_propuesto")

        # 4) EXTRA sin nada: deriva de la descripcion del codigo ordinario ya
        # resuelto del mismo empleado (red de seguridad de la resta >8h).
        if extra and categoria_ref_desc:
            ref = self._extra_from_ref(categoria_ref_desc)
            if ref is not None:
                return self._to_match(ref, "extra_por_categoria")

        # 5) Por flag ext (ultimo recurso).
        ext = 1 if extra else 0
        default = self._default.get(ext)
        if default is not None:
            return self._to_match(default, "ext_default")
        candidates = self._by_ext.get(ext, [])
        if candidates:
            return self._to_match(candidates[0], "ext_first")
        return TipoHoraMatch()

    def resolve_by_categoria(
        self, *, categoria: str, extra: bool
    ) -> tuple[TipoHoraRow, str] | None:
        """Casa la categoria del empleado con el codigo de hora del catalogo.

        extra=True  -> busca "HORA EXTRA <cat>"; si la categoria no tiene
                       codigo extra (encargado), cae al codigo NORMAL de esa
                       categoria.
        extra=False -> busca el codigo NORMAL/laborable de la categoria.
        """
        cat = _normalize_categoria(categoria)
        if not cat:
            return None
        if extra:
            row = self._best_cat(cat, want_extra=True)
            if row is not None:
                return (row, "categoria_extra")
            row = self._best_cat(cat, want_extra=False)
            if row is not None:
                return (row, "categoria_extra_a_normal")
            return None
        row = self._best_cat(cat, want_extra=False)
        if row is not None:
            return (row, "categoria_normal")
        return None

    def _best_cat(
        self, cat_tokens: set[str], *, want_extra: bool
    ) -> TipoHoraRow | None:
        """Mejor codigo del catalogo para una categoria. Clasifica extra vs
        normal por la palabra 'EXTRA' en la descripcion (no por el flag ext,
        que puede venir mal). Prioriza: mas solape de tokens de categoria,
        luego 'LABORABLE' (para normal), luego menos tokens sobrantes."""
        best: TipoHoraRow | None = None
        best_key: tuple[int, int, int] = (0, 0, 0)
        for t in self._tipos:
            desc = (t.descripcion or "").upper()
            is_extra = "EXTRA" in desc
            if is_extra != want_extra:
                continue
            cand = _category_tokens(t.descripcion)
            overlap = len(cat_tokens & cand)
            if overlap == 0:
                continue
            is_laborable = 1 if ("LABORABLE" in desc and not want_extra) else 0
            extra_tokens = len(cand - cat_tokens)
            key = (overlap, is_laborable, -extra_tokens)
            if key > best_key:
                best_key = key
                best = t
        return best

    def _extra_from_ref(self, ord_descripcion: str) -> TipoHoraRow | None:
        """Busca el codigo EXTRA (ext=1) que comparte la categoria del codigo
        ordinario dado (mayor solape de tokens de categoria)."""
        toks = _category_tokens(ord_descripcion)
        if not toks:
            return None
        best: TipoHoraRow | None = None
        best_overlap = 0
        for t in self._by_ext.get(1, []):
            overlap = len(toks & _category_tokens(t.descripcion))
            if overlap > best_overlap:
                best_overlap = overlap
                best = t
        return best if best_overlap > 0 else None

    @staticmethod
    def _to_match(t: TipoHoraRow, method: str) -> TipoHoraMatch:
        return TipoHoraMatch(
            ide=t.ide,
            codigo=t.codigo,
            descripcion=t.descripcion,
            ext=int(t.ext),
            pre=t.pre,
            prenom=t.prenom,
            method=method,
        )


_INC_LETTERS = {"V", "B", "AT", "FJ", "F", "H", "M"}
_SUFFIX_RE = re.compile(r"\(([A-Za-z]{1,2})\)")

# Palabras genericas que NO distinguen la categoria (se ignoran al comparar
# un codigo ordinario con su par extra).
_STOP_TOKENS = {
    "HORA", "HORAS", "LABORABLE", "LABORABLES", "EXTRA", "EXTRAS",
    "MES", "MESES", "DE", "DEL", "LA", "EL", "POR", "Y",
}


def _category_tokens(descripcion: str | None) -> set[str]:
    if not descripcion:
        return set()
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", descripcion.upper())
    return {w for w in words if w not in _STOP_TOKENS and len(w) >= 3}


# Sinonimos/abreviaturas de categoria del parte -> token canonico del catalogo.
_CAT_SYN = {
    "OF": "OFICIAL", "OFIC": "OFICIAL", "OFICIAL": "OFICIAL",
    "CAP": "CAPATAZ", "CAPATAZ": "CAPATAZ",
    "AY": "AYUDANTE", "AYTE": "AYUDANTE", "AYUD": "AYUDANTE",
    "AYUDANTE": "AYUDANTE", "AYDTE": "AYUDANTE",
    "ENC": "ENCARGADO", "ENCARGADO": "ENCARGADO",
    "GRUA": "GRUISTA", "GRUISTA": "GRUISTA",
    "PEON": "PEON", "PEONES": "PEON",
    "MIRAS": "MIRAS",
    "ESP": "ESPECIALISTA", "ESPECIALISTA": "ESPECIALISTA",
}

# Accentos -> sin acento, para tokenizar la categoria del parte.
_ACCENTS = str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNAEIOUUN")


def _normalize_categoria(categoria: str | None) -> set[str]:
    """Tokens canonicos de categoria a partir del texto del parte.
    'OF. 1a' -> {OFICIAL}; 'Oficial de miras' -> {OFICIAL, MIRAS};
    'Encargado' -> {ENCARGADO}; 'Peon' -> {PEON}."""
    if not categoria:
        return set()
    raw = categoria.translate(_ACCENTS).upper()
    raw = re.sub(r"[^A-Z0-9 ]", " ", raw)          # quita puntos, ª, etc.
    raw = re.sub(r"\b\d+[AOª]?\b", " ", raw)        # quita grados (1, 2, 1A)
    toks: set[str] = set()
    for w in raw.split():
        if w in _CAT_SYN:
            toks.add(_CAT_SYN[w])
        elif len(w) >= 3 and w not in _STOP_TOKENS:
            toks.add(w)
    return toks


def _desc_suffix_letters(descripcion: str | None) -> list[str]:
    """Letras de incidencia que aparezcan como '(X)' en la descripcion."""
    if not descripcion:
        return []
    out: list[str] = []
    for m in _SUFFIX_RE.finditer(descripcion):
        letter = m.group(1).upper()
        if letter in _INC_LETTERS:
            out.append(letter)
    return out

# application/services/partida_matcher.py
"""Casa una linea de horas (categoria del trabajador y, si aplica, su nombre)
con una PARTIDA del presupuesto.

Senales y prioridad (de mas a menos fuerte):
  1. ROL + NOMBRE: la categoria casa con el ROL de la partida (texto antes del
     parentesis, p.ej. "CAPATAZ" en "CAPATAZ (MARTIN)") Y el nombre del
     recurso aparece en la descripcion. -> 1.00 (auto_nombre)
  2. NOMBRE COMPLETO: >=2 tokens del nombre aparecen (nombre+apellido), aunque
     el rol no case. -> 0.92 (auto_nombre). Asi el nombre completo tiene
     prioridad, pero un APELLIDO suelto (1 token comun) NO pisa un rol correcto.
  3. ROL: la categoria casa con el rol (sin nombre). -> 0.50..0.90 (auto_categoria)
  4. Apellido suelto sin rol -> se descarta (demasiado debil, falsos positivos).

El parentesis de la descripcion se ignora para casar la categoria (asi
"OFICIAL (REMATES Y AYUDAS)" casa "Oficial" limpio).
"""
from __future__ import annotations

from dataclasses import dataclass

from application.services import text_match as tm
from application.services.partida_catalog import PartidaNodo

_STOP = {
    "de", "del", "la", "el", "los", "las", "y", "o", "u", "en", "a", "al",
    "por", "para", "con", "sin", "obra", "obras", "ci", "cd", "cp",
    "coste", "costes", "indirecto", "indirectos", "gasto", "gastos",
}

UMBRAL_ROL = 0.50          # fraccion de tokens de categoria que casan el rol
NOMBRE_TOKEN_MIN_LEN = 4   # longitud minima de token de nombre para contar
NOMBRE_FUERTE_MIN = 2      # nº de tokens de nombre que dan prioridad (nombre completo)


@dataclass
class PartidaMatch:
    partida: PartidaNodo
    score: float
    metodo: str            # "auto_nombre" | "auto_categoria"


def _content_tokens(texto: str | None) -> list[str]:
    toks = tm._tokens(tm.normalize(texto or ""))
    return [t for t in toks if len(t) >= 3 and t not in _STOP]


def _rol_texto(p: PartidaNodo) -> str:
    """Rol de la partida = descripcion (res) hasta el primer parentesis."""
    base = (p.res or p.tex or "")
    corte = base.find("(")
    if corte > 0:
        base = base[:corte]
    return base


def _folded(tokens) -> set[str]:
    return {tm.phonetic_fold(t) for t in tokens if len(t) >= 3}


def _token_en(token: str, folded: set[str]) -> bool:
    f = tm.phonetic_fold(token)
    if f in folded:
        return True
    for pf in folded:
        if tm._osa_distance(f, pf) <= 1:
            return True
        # Abreviaturas: "OFIC." casa "OFICIAL", "ADMIN" casa
        # "ADMINISTRATIVO". Prefijo de al menos 4 letras en cualquier
        # sentido (evita falsos positivos de prefijos cortos).
        if len(f) >= 4 and pf.startswith(f):
            return True
        if len(pf) >= 4 and f.startswith(pf):
            return True
    return False


def _score_rol(categoria: str | None, rol_folded: set[str]) -> float:
    """Casado categoria<->rol en AMBOS sentidos: cuenta tanto la fraccion
    de tokens de la CATEGORIA cubiertos por el rol como la fraccion de
    tokens del ROL cubiertos por la categoria, y se queda con la mayor.
    Asi "OFIC. 1a ALBANIL" casa la partida "OFICIAL" (el rol entero esta
    contenido en la categoria) aunque la categoria traiga mas apellidos."""
    cats = _content_tokens(categoria)
    if not cats:
        return 0.0
    hits_cat = sum(1 for t in cats if _token_en(t, rol_folded))
    score_cat = hits_cat / len(cats)
    cat_folded = _folded(cats)
    roles = list(rol_folded)
    if roles and cat_folded:
        hits_rol = sum(1 for t in roles if _token_en(t, cat_folded))
        score_rol = hits_rol / len(roles)
    else:
        score_rol = 0.0
    return max(score_cat, score_rol)


def _rol_exacto(categoria: str | None, rol_texto: str) -> bool:
    a = _content_tokens(categoria)
    b = _content_tokens(rol_texto)
    return bool(a) and set(a) == set(b)


def _hits_nombre(nombre: str | None, full_folded: set[str]) -> int:
    names = [t for t in _content_tokens(nombre) if len(t) >= NOMBRE_TOKEN_MIN_LEN]
    return sum(1 for t in names if _token_en(t, full_folded))


def match_partida(
    categoria: str | None,
    nombre: str | None,
    candidatas: list[PartidaNodo],
    *,
    umbral: float = UMBRAL_ROL,
) -> PartidaMatch | None:
    """Mejor partida para (categoria, nombre) entre ``candidatas``. ``None``
    si nada casa. Ver prioridades en el docstring del modulo."""
    mejor: PartidaMatch | None = None
    mejor_rol = 0.0
    mejor_exacto = False
    mejor_nombre = 0

    for p in candidatas:
        rol_folded = _folded(tm._tokens(tm.normalize(_rol_texto(p))))
        full_folded = _folded(
            tm._tokens(tm.normalize(f"{p.res or ''} {p.tex or ''}"))
        )
        if not rol_folded and not full_folded:
            continue
        rol_score = _score_rol(categoria, rol_folded)
        rol_ok = rol_score >= umbral
        nombre_hits = _hits_nombre(nombre, full_folded)

        if rol_ok and nombre_hits >= 1:
            score, metodo = 1.0, "auto_nombre"           # rol + nombre
        elif nombre_hits >= NOMBRE_FUERTE_MIN:
            score, metodo = 0.92, "auto_nombre"          # nombre completo (prioridad)
        elif rol_ok:
            score, metodo = 0.50 + 0.40 * rol_score, "auto_categoria"  # solo rol
        else:
            continue                                     # apellido suelto / nada

        exacto = _rol_exacto(categoria, _rol_texto(p))
        # Mejor por: score, luego rol exacto, luego mas score de rol, luego mas hits.
        clave = (score, 1 if exacto else 0, rol_score, nombre_hits)
        clave_mejor = (
            mejor.score if mejor else -1.0,
            1 if mejor_exacto else 0, mejor_rol, mejor_nombre,
        )
        if mejor is None or clave > clave_mejor:
            mejor = PartidaMatch(partida=p, score=round(score, 4), metodo=metodo)
            mejor_rol, mejor_exacto, mejor_nombre = rol_score, exacto, nombre_hits

    if mejor is None or mejor.score < 0.50:
        return None
    return mejor


# Categorias de mando/indirectas que SIEMPRE van a CI.
_MANDO_CI = {
    "encargado", "encargada", "capataz", "gruista", "jefe", "grupo",
    "produccion", "administrativo", "administrativa", "topografo",
    "responsable", "tecnico", "tecnica", "ingeniero", "ingeniera",
    "maquinista", "vigilante", "gerencia", "gerente", "delineante",
    "calidad", "prevencion", "seguridad", "bim", "leed", "encargado/a",
}
# Categorias de produccion que pueden ir a CI o a CD (varia).
_VARIABLE = {
    "oficial", "peon", "peones", "ayudante", "ayudantes", "albanil",
    "encofrador", "ferralla", "ferrallista", "especialista", "conductor",
    "pintor", "fontanero", "electricista", "carpintero", "yesero",
    "solador", "gruero", "operario",
}


def ambito_categoria(categoria: str | None) -> set[str]:
    """Capitulos donde buscar la partida segun la categoria del trabajador:
    {'CI'} para mando/indirectos; {'CI','CD'} para oficiales/peones (varia) y
    para categorias desconocidas (mas recall)."""
    toks = set(_content_tokens(categoria))
    if toks & _VARIABLE:
        return {"CI", "CD"}
    if toks & _MANDO_CI:
        return {"CI"}
    return {"CI", "CD"}

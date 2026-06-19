# application/services/text_match.py
"""Utilidades de normalizacion y similitud de texto para el casado.

Deterministas y sin dependencias externas. Se usan para puntuar nombres
de trabajador y de obra contra el maestro de Sigrid.

Mejoras de casado de nombres (jun 2026):
  - Iniciales: 'M.' casa con cualquier token que empiece por 'M'.
  - Umbral por token ADAPTATIVO a la longitud (tokens cortos casi exactos;
    largos toleran 2-3 erratas).
  - Plegado FONETICO de transliteracion (y->i, k->c, w->v, ch->c, h muda,
    dobles->simple) como SEGUNDO intento si el directo no llega.
  - ANCLAJE: no casar solo por el nombre de pila (penaliza si solo coincide
    1 token habiendo 2+ en ambos nombres).

IMPORTANTE: este archivo es IDENTICO en sv3 (ingesta) y sv4 (conciliacion).
Si se modifica, mantener ambas copias en sync (o extraer a un paquete comun).
"""
from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    """minusculas, sin acentos, sin signos, espacios colapsados."""
    if not text:
        return ""
    t = strip_accents(str(text)).lower()
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def normalize_code(code: str | None) -> str:
    """Codigo comparable: mayusculas, sin espacios/guiones."""
    if not code:
        return ""
    return strip_accents(str(code)).upper().replace(" ", "").replace("-", "")


def normalize_dni(dni: str | None) -> str:
    """DNI/CIF comparable: mayusculas, solo alfanumerico."""
    if not dni:
        return ""
    t = strip_accents(str(dni)).upper()
    return re.sub(r"[^A-Z0-9]", "", t)


def _tokens(text: str) -> list[str]:
    return [tok for tok in normalize(text).split(" ") if tok]


# Palabras que NO son nombre: categorias/roles que a veces se cuelan en el
# campo nombre cuando el manuscrito es ilegible. Se usan para detectar
# "nombre ausente" y NO intentar casar.
_CATEGORIA_WORDS = {
    "gruista", "oficial", "peon", "capataz", "encargado", "ayudante",
    "miras", "of", "ofi", "pe", "enc", "ay", "cap", "jefe", "obra",
    "administracion", "operario", "conductor", "maquinista", "albanil",
    "ferralla", "ferrallista", "encofrador",
}


def looks_like_category(name: str | None) -> bool:
    """True si el 'nombre' parece ser solo una categoria/rol (sin nombre
    real): p.ej. 'Gruista', 'Oficial 1a'. Util para no intentar casar."""
    toks = _tokens(name)
    if not toks:
        return True
    sign = [t for t in toks if not t.isdigit() and t not in {"1", "2", "1a", "2a"}]
    if not sign:
        return True
    return all(t in _CATEGORIA_WORDS for t in sign)


# ------------------------------------------------------------------ #
# Plegado fonetico (segundo intento). Conservador: solo variantes de
# transliteracion habituales; evita z->s y v->b (demasiado agresivos,
# fundirian apellidos distintos).
# ------------------------------------------------------------------ #
def phonetic_fold(token: str) -> str:
    t = token.lower()
    for a, b in (("ph", "f"), ("th", "t"), ("ch", "c"), ("sh", "s"),
                 ("qu", "k"), ("ck", "c"), ("gh", "g"), ("ll", "l")):
        t = t.replace(a, b)
    out: list[str] = []
    for ch in t:
        if ch in ("y", "j"):
            ch = "i"
        elif ch == "k":
            ch = "c"
        elif ch == "q":
            ch = "c"
        elif ch == "w":
            ch = "v"
        elif ch == "h":
            continue
        out.append(ch)
    s = "".join(out)
    res: list[str] = []
    for ch in s:
        if res and res[-1] == ch:
            continue
        res.append(ch)
    return "".join(res)


def _osa_distance(s1: str, s2: str) -> int:
    """Distancia Optimal String Alignment (Damerau-Levenshtein restringida)."""
    n, m = len(s1), len(s2)
    if n == 0:
        return m
    if m == 0:
        return n
    prev2 = [0] * (m + 1)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and s1[i - 1] == s2[j - 2]
                    and s1[i - 2] == s2[j - 1]):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[m]


def token_ratio(a: str, b: str) -> float:
    """Similitud 0..1 entre dos tokens (1 - dist_edicion / longitud mayor)."""
    if a == b:
        return 1.0
    L = max(len(a), len(b))
    if L == 0:
        return 0.0
    return 1.0 - _osa_distance(a, b) / L


def _token_threshold(a: str, b: str) -> float:
    """Umbral de ratio adaptativo a la longitud."""
    L = max(len(a), len(b))
    if L <= 3:
        return 0.99
    if L <= 5:
        return 0.80
    if L <= 7:
        return 0.72
    return 0.66


def _is_initial(t: str) -> bool:
    return len(t) == 1


def _token_match(t: str, u: str) -> float:
    """Puntuacion de emparejamiento de dos tokens (0 si no casan)."""
    if _is_initial(t) and not _is_initial(u):
        return 0.9 if u[:1] == t else 0.0
    if _is_initial(u) and not _is_initial(t):
        return 0.9 if t[:1] == u else 0.0

    thr = _token_threshold(t, u)
    r = token_ratio(t, u)
    if r >= thr:
        return r
    rf = token_ratio(phonetic_fold(t), phonetic_fold(u))
    if rf >= thr:
        return rf * 0.97
    return 0.0


def name_similarity(a: str | None, b: str | None) -> float:
    """Similitud 0..1 entre dos nombres por solape DIFUSO de tokens."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0

    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    used = [False] * len(big)
    matched_weight = 0.0
    matched_count = 0
    for t in small:
        best_r = 0.0
        best_j = -1
        for j, u in enumerate(big):
            if used[j]:
                continue
            r = _token_match(t, u)
            if r > best_r:
                best_r, best_j = r, j
        if best_j >= 0 and best_r > 0.0:
            used[best_j] = True
            matched_weight += best_r
            matched_count += 1

    if matched_count == 0:
        return 0.0
    recall = matched_weight / len(small)
    jaccard = matched_count / (len(ta) + len(tb) - matched_count)
    score = 0.6 * recall + 0.4 * jaccard

    if matched_count < 2 and min(len(ta), len(tb)) >= 2:
        score *= 0.45

    return round(score, 4)

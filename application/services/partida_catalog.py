# application/services/partida_catalog.py
"""Arbol de partidas del presupuesto de una obra (Sigrid ``obrparpar``).

- Relacion padre-hijo: ``obrparpar.padide`` -> ``obrparpar.ide`` (raices
  ``padide=0``). La raiz suele ser "PRESUPUESTO" y CD/CI/CP cuelgan de ella.
- CATEGORIA (CD/CI/CP): se recorre la cadena hoja->raiz y se toma la primera
  que se pueda determinar, mirando en cada nodo DOS cosas:
    a) el PREFIJO DEL CODIGO (CI.../CD.../CP...), y
    b) la PALABRA CLAVE en la descripcion del capitulo: "INDIRECT" -> CI,
       "PROPORCION" -> CP, "DIRECT" -> CD (INDIRECT se comprueba antes que
       DIRECT por la subcadena).
  Asi funciona aunque el codigo jerarquico este en otro campo o no exista:
  los capitulos se llaman "COSTES INDIRECTOS" / "MANO DE OBRA INDIRECTA" /
  "COSTES DIRECTOS", y eso basta para clasificar.
- PARTIDA = HOJA del arbol (sin hijos). Los capitulos tienen hijos.
- ``tipdes = 0`` => activa.
"""
from __future__ import annotations

from dataclasses import dataclass

from application.services import text_match as tm


@dataclass
class PartidaNodo:
    ide: int
    padide: int | None
    cod: str | None
    res: str | None
    tex: str | None
    tipdes: int = 0
    cosindide: int | None = None
    unimed: str | None = None
    # ---- calculado ----
    capitulo_cod: str | None = None
    categoria: str = "OTRO"               # CD / CI / CP / OTRO
    es_hoja: bool = False
    ruta_capitulos: str = ""
    activa: bool = True


def _empieza_capitulo(cod_norm: str, prefijo: str) -> bool:
    """``cod_norm`` empieza por el prefijo y detras NO hay una letra (asi
    'CI', 'CI.1', 'CI1' valen; 'CIMENTACION' no)."""
    if not cod_norm.startswith(prefijo):
        return False
    resto = cod_norm[len(prefijo):]
    return resto == "" or not resto[0].isalpha()


def _cat_por_codigo(cod: str | None) -> str:
    c = tm.strip_accents((cod or "").strip()).upper()
    if not c:
        return "OTRO"
    if _empieza_capitulo(c, "CI"):
        return "CI"
    if _empieza_capitulo(c, "CP"):
        return "CP"
    if _empieza_capitulo(c, "CD"):
        return "CD"
    return "OTRO"


def _cat_por_descripcion(res: str | None) -> str:
    r = tm.strip_accents((res or "")).upper()
    if "INDIRECT" in r:          # COSTES INDIRECTOS / MANO DE OBRA INDIRECTA
        return "CI"
    if "PROPORCION" in r:        # COSTES PROPORCIONALES
        return "CP"
    if "DIRECT" in r:            # COSTES DIRECTOS  (despues de INDIRECT)
        return "CD"
    return "OTRO"


def clasifica_categoria(cod: str | None, res: str | None = None) -> str:
    """CD/CI/CP/OTRO de UN nodo: primero por prefijo de codigo, luego por
    palabra clave de la descripcion."""
    c = _cat_por_codigo(cod)
    if c != "OTRO":
        return c
    return _cat_por_descripcion(res)


def build_arbol_partidas(filas) -> dict[int, PartidaNodo]:
    """Construye PartidaNodo desde filas crudas y calcula hoja, categoria
    (CD/CI/CP por codigo o descripcion en la cadena) y ruta."""
    nodos: dict[int, PartidaNodo] = {}
    for r in filas:
        nodos[r.ide] = PartidaNodo(
            ide=r.ide,
            padide=getattr(r, "padide", None),
            cod=getattr(r, "cod", None),
            res=getattr(r, "res", None),
            tex=getattr(r, "tex", None),
            tipdes=getattr(r, "tipdes", 0) or 0,
            cosindide=getattr(r, "cosindide", None),
            unimed=getattr(r, "unimed", None),
        )

    con_hijos: set[int] = set()
    for n in nodos.values():
        pad = n.padide or 0
        if pad and pad in nodos:
            con_hijos.add(pad)

    for n in nodos.values():
        n.es_hoja = n.ide not in con_hijos
        n.activa = (n.tipdes or 0) == 0

        codigos: list[str] = []
        categoria = "OTRO"
        cap_cod: str | None = None
        cur: PartidaNodo | None = n
        visto: set[int] = set()
        while cur is not None and cur.ide not in visto:
            visto.add(cur.ide)
            if cur.cod:
                codigos.append(cur.cod)
            if categoria == "OTRO":
                cat = clasifica_categoria(cur.cod, cur.res)
                if cat != "OTRO":
                    categoria = cat
                    cap_cod = cur.cod
            pad = cur.padide or 0
            if not pad or pad not in nodos:
                break
            cur = nodos.get(pad)

        n.categoria = categoria
        n.capitulo_cod = cap_cod
        n.ruta_capitulos = " > ".join(reversed(codigos))

    return nodos


def partidas_hoja(
    nodos: dict[int, PartidaNodo], *, categoria: str | None = None,
    categorias: set[str] | None = None, solo_activas: bool = True,
) -> list[PartidaNodo]:
    """Partidas (hojas). Filtra por ``categoria`` (una) o ``categorias``
    (varias, p.ej. {'CI','CD'}). Ordenadas por codigo."""
    permitidas: set[str] | None = None
    if categorias is not None:
        permitidas = set(categorias)
    elif categoria is not None:
        permitidas = {categoria}
    out = []
    for n in nodos.values():
        if not n.es_hoja:
            continue
        if solo_activas and not n.activa:
            continue
        if permitidas is not None and n.categoria not in permitidas:
            continue
        out.append(n)
    out.sort(key=lambda n: (n.cod or ""))
    return out

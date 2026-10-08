"""Departamento de una entidad a partir de su nombre, con la tabla de ubigeos.

POR QUE HACE FALTA

  El detector de texto (`ocds_oece._departamento`, `orchestrator._detectar_depto`)
  busca el NOMBRE de un departamento en el texto. Una municipalidad casi nunca
  lo lleva: "MUNICIPALIDAD PROVINCIAL DE HUAYLAS - CARAZ", "MUNICIPALIDAD
  DISTRITAL DE ESPINAR", "UGEL ISLAY". Medido el 2026-10-07: 296 filas de
  municipalidades distritales, 65 de provinciales y 27 de UGEL sin
  departamento, con el nombre de la provincia o del distrito a la vista.

  Y cuando SI lleva un nombre, a veces es el equivocado: "MUNICIPALIDAD
  DISTRITAL DE SAN MARTIN DE PORRES" es Lima, "MUNICIPALIDAD PROVINCIAL DEL
  ALTO AMAZONAS" es Loreto, "MUNICIPALIDAD DISTRITAL DE NUEVA CAJAMARCA" es San
  Martin. El detector de palabras las etiquetaba por el nombre que contienen.
  Para una municipalidad, la tabla de ubigeos manda sobre el texto.

LA TABLA

  `shared/ubigeo.csv`: 1.893 distritos del INEI con su provincia y
  departamento (fuente: jmcastagnetto/ubigeo-peru-aumentado, columnas inei,
  departamento, provincia, distrito; recortado el 2026-10-07). Las 196
  provincias tienen nombre unico en el pais. De los 1.733 nombres de distrito,
  95 se repiten en mas de un departamento (Santa Rosa, San Jeronimo, Asuncion,
  ...): para esos se devuelve None salvo que la propia entidad diga el
  departamento ("... - LORETO"). Mejor sin region que con una adivinada: el
  filtro de alertas se la creeria.
"""
from __future__ import annotations

import csv
import functools
import re
from pathlib import Path

from shared.config import DEPARTAMENTOS, normalizar

_CSV = Path(__file__).with_name("ubigeo.csv")

# El CSV escribe ANCASH, JUNIN, APURIMAC; la app usa Áncash, Junín, Apurímac.
_NOMBRE_APP = {normalizar(d): d for d in DEPARTAMENTOS}

# El articulo se conserva: "LA LIBERTAD DE PALLAN", "EL TAMBO" y "LOS OLIVOS"
# estan asi en la tabla oficial.
_RE_MUNI = re.compile(
    r"municipalidad\s+(?:(provincial|distrital|metropolitana)\s+)?"
    r"(?:de\s+|del\s+)?(.+?)"
    r"(?:\s*[-–/(,]|\s+sede\b|$)")
_RE_UGEL = re.compile(
    r"(?:\bugel\b|unidad de gestion educativa local)\s+(?:n[°º]?\s*)?(?:\d+\s+)?"
    r"(?:de\s+|del\s+)?(.+?)(?:\s*[-–/(,]|$)")


@functools.lru_cache(maxsize=1)
def _tablas() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    provincias: dict[str, set[str]] = {}
    distritos: dict[str, set[str]] = {}
    with _CSV.open(encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            depto = _NOMBRE_APP[normalizar(fila["departamento"])]
            provincias.setdefault(normalizar(fila["provincia"]), set()).add(depto)
            distritos.setdefault(normalizar(fila["distrito"]), set()).add(depto)
    return provincias, distritos


@functools.lru_cache(maxsize=1)
def _por_codigo() -> dict[str, tuple[str, str, str]]:
    """ubigeo INEI de 6 digitos -> (departamento, provincia, distrito)."""
    salida = {}
    with _CSV.open(encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            depto = _NOMBRE_APP[normalizar(fila["departamento"])]
            salida[fila["ubigeo"].zfill(6)] = (depto, fila["provincia"].title(),
                                               fila["distrito"].title())
    return salida


def por_ubigeo(codigo: str | None) -> dict | None:
    """(departamento, provincia, distrito) de un ubigeo INEI, como lo da SUNAT.

    SUNAT publica el ubigeo del domicilio fiscal ("150130" = Lima/Lima/San
    Borja) y es la forma mas fiable de saber la region de una empresa: el
    nombre del departamento escrito a mano se equivoca, el codigo no.
    """
    if not codigo:
        return None
    codigo = str(codigo).strip().zfill(6)
    fila = _por_codigo().get(codigo)
    if not fila:
        return None
    return {"departamento": fila[0], "provincia": fila[1], "distrito": fila[2]}


def _palabra(nombre: str, texto: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(nombre) + r"(?![a-z0-9])",
                     texto) is not None


def _departamento_nombrado(texto: str) -> str | None:
    """El departamento que el texto nombra como palabra entera, si hay uno solo."""
    hallados = {real for norm, real in _NOMBRE_APP.items() if _palabra(norm, texto)}
    return hallados.pop() if len(hallados) == 1 else None


def _buscar(nombre: str, tabla: dict[str, set[str]], pista: str) -> str | None:
    """`nombre` en la tabla, probando tambien sin las ultimas palabras.

    "san jeronimo de tunan" esta tal cual; "huaylas caraz" no, pero "huaylas"
    si. Se recorta por el final porque lo que sobra suele ser la capital o un
    apellido del nombre oficial, nunca el principio.
    """
    palabras = nombre.split()
    for corte in range(len(palabras), 0, -1):
        candidato = " ".join(palabras[:corte])
        deptos = tabla.get(candidato)
        if not deptos:
            continue
        if len(deptos) == 1:
            return next(iter(deptos))
        # Ambiguo: solo si la propia entidad lo desempata ("... - LORETO").
        nombrado = _departamento_nombrado(pista)
        return nombrado if nombrado in deptos else None
    return None


def departamento_de_entidad(entidad: str) -> str | None:
    """Departamento de una municipalidad o UGEL por su nombre. None si no se sabe."""
    if not entidad:
        return None
    t = normalizar(entidad)
    provincias, distritos = _tablas()

    m = _RE_MUNI.search(t)
    if m:
        nivel, nombre = m.group(1), m.group(2).strip()
        resto = t[m.end(2):]
        if nivel == "provincial":
            return _buscar(nombre, provincias, resto)
        if nivel == "distrital":
            return _buscar(nombre, distritos, resto)
        # "Municipalidad de X" / "Metropolitana de Lima": provincia primero,
        # que es la forma oficial de las provinciales, y distrito despues.
        return _buscar(nombre, provincias, resto) or _buscar(nombre, distritos, resto)

    m = _RE_UGEL.search(t)
    if m:
        nombre = m.group(1).strip()
        resto = t[m.end(1):]
        return _buscar(nombre, provincias, resto) or _buscar(nombre, distritos, resto)

    return None

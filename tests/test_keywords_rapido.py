"""El filtro de keywords se reescribio para ser rapido (una sola busqueda por
fila en vez de una por keyword). Tiene que decidir EXACTAMENTE igual que el de
antes: se compara contra la version anterior, copiada aqui como referencia."""
import itertools
import re
import unicodedata

import pytest

from shared.config import match_keywords, normalizar


def _normalizar_antes(texto):
    if not texto:
        return ""
    d = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in d if not unicodedata.combining(c)).lower()


def _match_antes(texto, keywords):
    if not keywords:
        return True
    t = _normalizar_antes(texto)
    for kw in keywords:
        c = _normalizar_antes(kw).strip()
        if c and re.search(r"(?<!\w)" + re.escape(c) + r"(?:es|s)?(?!\w)", t):
            return True
    return False


TEXTOS = [
    "CUERPO GENERAL DE BOMBEROS", "Implementación de un ERP institucional",
    "ADQUISICION DE PRENDAS DE VESTIR", "SERVIDORES Y COMPUTADORAS",
    "CONTRATACI?N DEL SERVICIO DE LIMPIEZA", "Señalización vial en Cusco",
    "papel bond A4 x 500", "Útiles de escritorio: tijera, libreta, archivador",
    "camión cisterna", "ab abc abd", "", "año 2026 – compra de arroz",
    "Tecnología de la información", "computadoras portátiles (laptops)",
]
LISTAS = [
    ["erp"], ["prenda"], ["servidor"], ["tecnologia"], ["señalizacion"],
    ["arroz", "aceite"], ["ab", "abc"], ["abc", "ab"], ["  ", ""], [" erp "],
    ["útiles", "papel bond"], ["camion"], ["laptop", "computadora portatil"],
    ["c++"], ["contratacion"], ["a"],
]


@pytest.mark.parametrize("texto,keywords", list(itertools.product(TEXTOS, LISTAS)))
def test_decide_igual_que_antes(texto, keywords):
    assert match_keywords(texto, keywords) == _match_antes(texto, keywords)


def test_normalizar_igual_que_antes():
    for t in TEXTOS + ["ÁÉÍÓÚÑ ü", "ﬁ ligadura", "x²"]:
        assert normalizar(t) == _normalizar_antes(t)


def test_sin_keywords_pasa_todo():
    assert match_keywords("lo que sea", []) is True

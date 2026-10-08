"""La tabla de ubigeos manda sobre el nombre que contenga la entidad.

Casos reales de la base del 2026-10-07: municipalidades sin departamento y
municipalidades etiquetadas por una palabra que no era su region.
"""
import pytest

from shared.ubigeo import departamento_de_entidad


@pytest.mark.parametrize("entidad, esperado", [
    # Provinciales: 196 nombres unicos en el pais.
    ("MUNICIPALIDAD PROVINCIAL DE HUAYLAS - CARAZ", "Áncash"),
    ("MUNICIPALIDAD PROVINCIAL DE ESPINAR", "Cusco"),
    ("MUNICIPALIDAD PROVINCIAL DE YUNGUYO", "Puno"),
    ("MUNICIPALIDAD PROVINCIAL DE CHUCUITO - JULI", "Puno"),
    ("MUNICIPALIDAD PROVINCIAL DEL CUSCO", "Cusco"),
    ("MUNIPAUCARTAMBO - Municipalidad Provincial de Paucartambo", "Cusco"),
    ("MUNICIPALIDAD PROVINCIAL DE RODRIGUEZ DE MENDOZA", "Amazonas"),
    ("MUNICIPALIDAD PROVINCIAL DE SAN ROMAN - JULIACA", "Puno"),
    # El nombre contiene otro departamento y la tabla lo corrige.
    ("MUNICIPALIDAD DISTRITAL DE SAN MARTIN DE PORRES", "Lima"),
    ("MUNICIPALIDAD PROVINCIAL DEL ALTO AMAZONAS - YURIMAGUAS", "Loreto"),
    ("MUNICIPALIDAD DISTRITAL DE NUEVA CAJAMARCA", "San Martín"),
    ("MUNICIPALIDAD DISTRITAL DE LA LIBERTAD DE PALLAN", "Cajamarca"),
    # Distrito ambiguo: solo con la pista de la propia entidad.
    ("MUNICIPALIDAD DISTRITAL DE SAN JUAN BAUTISTA - LORETO", "Loreto"),
    ("MUNICIPALIDAD DISTRITAL DE SAN JUAN BAUTISTA", None),
    ("MUNICIPALIDAD DISTRITAL DE SANTA ROSA", None),
    # UGEL: casi siempre llevan el nombre de la provincia.
    ("UGEL HUANCAYO", "Junín"),
    ("UNIDAD DE GESTION EDUCATIVA LOCAL ISLAY", "Arequipa"),
    ("UGEL ANTA", "Cusco"),
    # Lo que no es municipalidad ni UGEL no se adivina.
    ("EJERCITO PERUANO", None),
    ("GOBIERNO REGIONAL DE PUNO SEDE CENTRAL", None),
    ("", None),
])
def test_departamento_por_ubigeo(entidad, esperado):
    assert departamento_de_entidad(entidad) == esperado


def test_la_tabla_cubre_los_25_departamentos():
    from shared.config import DEPARTAMENTOS
    from shared.ubigeo import _tablas
    provincias, _ = _tablas()
    cubiertos = set().union(*provincias.values())
    assert cubiertos == set(DEPARTAMENTOS)

"""El detector de departamento de OECE compara palabras enteras.

El fallo que protege paso de verdad (2026-10-07): "ica" dentro de "publica",
"tecnica" o "electrica" etiquetaba como Ica la mitad de las compras del
Gobierno Regional de Puno. El filtro por regiones lee esta columna, asi que
el error no era cosmetico: cambiaba a quien le llegaba cada alerta.
"""
import pytest

from radar_bot.scrapers.ocds_oece import _departamento


@pytest.mark.parametrize("texto, esperado", [
    ("GOBIERNO REGIONAL DE PUNO SEDE CENTRAL SERVICIO DE ENERGIA ELECTRICA", "Puno"),
    ("MUNICIPALIDAD DISTRITAL DE SAN JUAN BAUTISTA SERVICIO DE SUPERVISION TECNICA", None),
    ("CENTRO NACIONAL DE ABASTECIMIENTO ADQUISICION DE PRODUCTOS FARMACEUTICOS", None),
    ("MUNICIPALIDAD PROVINCIAL DE ICA MEJORAMIENTO DE PISTAS", "Ica"),
    ("GOBIERNO REGIONAL DE ICA - HOSPITAL REGIONAL", "Ica"),
    ("UGEL HUANCAYO ADQUISICION DE MOBILIARIO", "Junín"),
    ("MUNICIPALIDAD PROVINCIAL DE SAN ROMAN JULIACA OBRA DE SANEAMIENTO", "Puno"),
    ("OBRA EN EL DISTRITO DE TAMBOPATA, DEPARTAMENTO DE MADRE DE DIOS", "Madre de Dios"),
    ("INSTITUTO NACIONAL DE SALUD - LIMA", "Lima"),
    ("SERVICIO DE CLIMATIZACION DE AMBIENTES", None),   # "lima" dentro de climatizacion
])
def test_el_departamento_sale_de_palabras_enteras(texto, esperado):
    assert _departamento(texto) == esperado


def test_la_entidad_manda_sobre_el_objeto():
    """GORE Tacna que hace una obra en la frontera con Moquegua compra en Tacna."""
    assert _departamento(
        "GOBIERNO REGIONAL DE TACNA-TRANSPORTES MEJORAMIENTO DE LA VIA HACIA MOQUEGUA",
        entidad="GOBIERNO REGIONAL DE TACNA-TRANSPORTES",
    ) == "Tacna"


def test_la_municipalidad_se_ubica_por_ubigeo_aunque_el_nombre_engane():
    assert _departamento(
        "MUNICIPALIDAD DISTRITAL DE SAN MARTIN DE PORRES COMPRA DE LLANTAS",
        entidad="MUNICIPALIDAD DISTRITAL DE SAN MARTIN DE PORRES",
    ) == "Lima"
    assert _departamento(
        "MUNICIPALIDAD PROVINCIAL DE ESPINAR ADQUISICION DE CEMENTO",
        entidad="MUNICIPALIDAD PROVINCIAL DE ESPINAR",
    ) == "Cusco"


def test_departamento_de_tiene_prioridad_sobre_la_sede():
    """Una entidad de Lima que compra para Cusco es una compra de Cusco."""
    assert _departamento(
        "MINISTERIO DE CULTURA LIMA RESTAURACION EN EL DEPARTAMENTO DE CUSCO"
    ) == "Cusco"

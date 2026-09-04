"""Pruebas del scraper de compras menores de gob.pe.

QUE PROTEGEN

  Esta fuente guarda lo que devuelve un buscador generalista, y ese buscador
  mezcla compras con concursos CAS, comunicados y resoluciones. Ya se pago el
  precio de no filtrar: datos_abiertos guardo 17 fichas de catalogo invisibles
  mientras el parte anunciaba "17 nuevas". El filtro es la fuente; lo demas es
  fontaneria.

  Ninguna toca la red: se prueba la REGLA sobre items reales copiados de la
  API, que es lo que se rompe al editar el codigo.
"""
from datetime import datetime

import pytest

from radar_bot.scrapers.gob_pe import _es_compra, _fecha_es, _parsear_item
from shared import fechas

# ─── La fecha en castellano ──────────────────────────────

@pytest.mark.parametrize("texto, esperado", [
    ("28 de agosto de 2026", fechas.fija(2026, 8, 28)),
    (" 8 de agosto de 2026", fechas.fija(2026, 8, 8)),
    ("3 de setiembre de 2026", fechas.fija(2026, 9, 3)),   # variante peruana
    ("3 de septiembre de 2026", fechas.fija(2026, 9, 3)),
])
def test_la_fecha_en_castellano_se_entiende(texto, esperado):
    assert _fecha_es(texto) == esperado


@pytest.mark.parametrize("texto", [None, "", "ayer", "32 de enero de 2026",
                                   "5 de brumario de 2026"])
def test_una_fecha_rara_da_none_y_no_un_invento(texto):
    """None descarta el item; una fecha de relleno lo mostraria como fresco."""
    assert _fecha_es(texto) is None


# ─── Que es una compra y que es ruido ────────────────────

@pytest.mark.parametrize("titulo", [
    "Solicitud de cotización N°123",
    "Solicitud de información: ADQUISICIÓN DEL PRODUCTO FARMACÉUTICO X",
    "Indagación de mercado - servicio de vigilancia",
    "Contratación de bienes por montos menores a 8 UIT",
])
def test_una_compra_se_reconoce(titulo):
    assert _es_compra(titulo, "") is True


@pytest.mark.parametrize("titulo", [
    "Concurso Público de Méritos N° 001-2026",     # empleo, no compra
    "Comunicado N°042-2026-CCD",
    "Resolución Directoral N° 55-2026",
    "Resultado de la solicitud de cotización N°9", # resultado: ya cerro
    "Convocatoria CAS N° 33-2026",
])
def test_el_ruido_del_buscador_no_entra(titulo):
    """Lo que reventaria la fuente: empleo y burocracia como si fueran compras."""
    assert _es_compra(titulo, "") is False


def test_el_ruido_gana_aunque_mencione_una_compra():
    """"Comunicado sobre la cotización X" sigue sin ser una oportunidad."""
    assert _es_compra("Comunicado sobre la solicitud de cotización N°4",
                      "adquisición de equipos") is False


# ─── El item completo ────────────────────────────────────

def _item(**cambios):
    base = {
        "id": "6502648",
        "publication": "28 de agosto de 2026",
        "name_with_parent": "Solicitud de cotización",
        "content_sub_title_card": "SUSALUD - Superintendencia Nacional de Salud",
        "url": '<a href="/institucion/susalud/informes-publicaciones/691-x">'
               'Solicitud de cotización</a>',
        "content": "adquisición de bienes según el artículo 50",
        "action_url": "https://cdn.www.gob.pe/uploads/documento.pdf",
    }
    base.update(cambios)
    return base


def test_un_item_bueno_produce_la_licitacion():
    d = _parsear_item(_item())
    assert d["id"] == "gobpe_6502648"
    assert d["fuente"] == "gob_pe"
    assert d["entidad"].startswith("SUSALUD")
    assert d["url"] == "https://www.gob.pe/institucion/susalud/informes-publicaciones/691-x"
    assert d["bases_urls"] == ["https://cdn.www.gob.pe/uploads/documento.pdf"]
    assert d["fecha_publicacion"] == fechas.fija(2026, 8, 28)
    # El plazo vive en el PDF: NULL a proposito, jamas inventado.
    assert d["fecha_cierre"] is None


@pytest.mark.parametrize("roto", [
    {"publication": None},              # ficha sin fecha
    {"content_sub_title_card": ""},     # sin entidad
    {"name_with_parent": "", "url": ""},  # sin titulo por ninguna via
    {"name_with_parent": "Comunicado N°7"},  # ruido
])
def test_un_item_incompleto_o_ruidoso_se_descarta(roto):
    """Mejor perder una publicacion rara que guardar otra fila invisible."""
    assert _parsear_item(_item(**roto)) is None


def test_el_titulo_se_rescata_del_anchor_si_falta():
    """La API devuelve items sin `name_with_parent` pero con el anchor lleno."""
    d = _parsear_item(_item(name_with_parent=None))
    assert d is not None
    assert "cotización" in d["objeto"]

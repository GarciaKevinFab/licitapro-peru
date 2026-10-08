"""Autocompletar la empresa por RUC: validacion, normalizacion y region.

Ninguna toca la red. Los JSON son copias de lo que devolvieron OpenRUC y
apis.net.pe el 2026-10-07 para el mismo RUC.
"""
import pytest

from shared.sunat import _normalizar, ruc_valido
from shared.ubigeo import por_ubigeo

OPENRUC = {"ruc": "20100070970", "razon_social": "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A.",
           "estado": "ACTIVO", "condicion": "HABIDO", "direccion": "CAL. MORELLI NRO 181 INT P-2",
           "ubigeo": "150130", "source": "SUNAT"}
APISNET = {"nombre": "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A.", "tipoDocumento": "6",
           "numeroDocumento": "20100070970", "estado": "ACTIVO", "condicion": "HABIDO",
           "direccion": "CAL. MORELLI NRO 181 INT. P-2 ", "ubigeo": "150130", "lote": "-",
           "distrito": "SAN BORJA", "provincia": "LIMA", "departamento": "LIMA"}


@pytest.mark.parametrize("ruc, ok", [
    ("20100070970", True),    # SUNAT: digito verificador correcto
    ("20131312955", True),
    ("20100070971", False),   # mismo RUC con el verificador cambiado
    ("20600000001", False),
    ("1234567890", False),    # 10 digitos
    ("30100070970", False),   # prefijo que SUNAT no emite
    ("", False), (None, False),
])
def test_el_ruc_se_valida_antes_de_gastar_una_consulta(ruc, ok):
    assert ruc_valido(ruc) is ok


def test_las_dos_fuentes_quedan_en_la_misma_forma():
    a = _normalizar("20100070970", OPENRUC, "openruc")
    b = _normalizar("20100070970", APISNET, "apis.net.pe")
    for k in ("razon_social", "estado", "condicion", "departamento", "provincia", "distrito"):
        assert a[k] == b[k], k
    assert a["razon_social"].startswith("SUPERMERCADOS PERUANOS")
    # La direccion se limpia de espacios y guiones de relleno.
    assert b["direccion"] == "CAL. MORELLI NRO 181 INT. P-2"


def test_la_region_sale_del_ubigeo_y_no_del_texto():
    d = _normalizar("20100070970", OPENRUC, "openruc")
    assert d["departamento"] == "Lima"          # con el nombre que usa la app
    assert d["provincia"] == "Lima"
    assert d["distrito"] == "San Borja"


def test_un_ubigeo_de_region_con_tilde_cae_en_el_nombre_de_la_app():
    assert por_ubigeo("080101")["departamento"] == "Cusco"
    assert por_ubigeo("020101")["departamento"] == "Áncash"
    assert por_ubigeo("170101") == {"departamento": "Madre de Dios", "provincia": "Tambopata",
                                    "distrito": "Tambopata"}
    assert por_ubigeo("999999") is None
    assert por_ubigeo(None) is None


def test_un_ubigeo_desconocido_deja_la_region_vacia_y_no_inventada():
    d = _normalizar("20100070970", {**OPENRUC, "ubigeo": "999999"}, "openruc")
    assert d["departamento"] is None

"""Pruebas del parser de contratos menores del OECE.

Ninguna toca la red: los JSON son copias de lo que devolvio la API el
2026-10-07. Lo que protege es que la fila que entra a `licitaciones` tenga la
region, el plazo con hora y el texto entero (la lista lo trunca a 38
caracteres, el detalle no).
"""
import pytest

from radar_bot.scrapers.oece_menores import CODIGOS_DEPARTAMENTO, _fecha, parsear
from shared import fechas
from shared.config import DEPARTAMENTOS
from tests.conftest import sin_base

ITEM = {
    "secuencia": "1", "idContrato": "100545", "nroContratacion": "97",
    "desContratacion": "CM-97-2026-UL-INGEMMET", "idObjetoContrato": "2",
    "nomObjetoContrato": "Servicio",
    "desObjetoContrato": "SERVICIO DE SEGURIDAD Y VIGILANCIA PAR",   # truncado
    "fecIniCotizacion": "07/10/2026 13:05:00", "fecFinCotizacion": "07/10/2026 19:00:00",
    "cotizar": "True", "idEstadoContrato": "2", "nomEstadoContrato": "Vigente",
    "fecPublica": "07/10/2026 13:04:34", "idTipoCotizacion": "2",
    "nomEntidad": "INSTITUTO GEOLOGICO, MINERO Y METALURG",          # truncado
}

DETALLE = {
    "uitContratoCompletoProjection": {
        "idContrato": 100545, "nomEstadoContrato": "Vigente", "nomObjetoContrato": "Servicio",
        "nroDescripcion": "CM-97-2026-UL-INGEMMET", "anio": 2026,
        "desObjetoContrato": "SERVICIO DE SEGURIDAD Y VIGILANCIA PARA LOS ÓRGANOS DESCONCENTRADOS DE",
        "fecPublica": "07/10/2026 13:04:34", "idEntidad": 1962,
        "nomEntidad": "INSTITUTO GEOLOGICO, MINERO Y METALURGICO",
    },
    "uitContratoEtapaProjectionList": [
        {"nomEtapaContrato": "ETAPA DE CONSULTAS", "fecIni": "07/10/2026 13:05:00",
         "fecFin": "07/10/2026 14:00:00"},
    ],
    "uitContratoItemProjectionList": [
        {"nomCubso": "SERVICIO DE SEGURIDAD Y VIGILANCIA", "nomDistritoExt": "LIMA/LIMA/SAN BORJA",
         "descripcionItem": "SERVICIO DE SEGURIDAD Y VIGILANCIA PARA LOS ÓRGANOS DESCONCENTRADOS DE",
         "cantidad": 1, "precioTotal": None},
    ],
}


def test_con_detalle_entra_el_texto_entero_y_la_region():
    f = parsear(ITEM, "Lima", DETALLE)
    assert f["id"] == "oecem_100545"
    assert f["fuente"] == "oece_menores"
    assert f["tipo"] == "CM"
    assert f["nomenclatura"] == "CM-97-2026-UL-INGEMMET"
    assert f["entidad"] == "INSTITUTO GEOLOGICO, MINERO Y METALURGICO"
    assert f["objeto"].startswith("[SERVICIO] SERVICIO DE SEGURIDAD Y VIGILANCIA PARA LOS")
    assert f["departamento"] == "Lima"
    assert f["provincia"] == "LIMA"
    assert f["categoria"] == "services"
    assert f["fecha_publicacion"] == fechas.fija(2026, 10, 7, 13, 4, 34)
    # El plazo para cotizar cierra a las 19:00, no a medianoche.
    assert f["fecha_cierre"] == fechas.fija(2026, 10, 7, 19, 0, 0)
    assert f["monto_referencial"] is None          # la parte publica no lo trae
    assert f["url"].endswith("/detail/100545")


def test_sin_detalle_sirve_igual_para_refrescar():
    """Para una fila ya guardada basta la lista: lo truncado no se escribe."""
    f = parsear(ITEM, "Lima")
    assert f is not None
    assert f["entidad"] == "INSTITUTO GEOLOGICO, MINERO Y METALURG"
    assert f["fecha_cierre"] == fechas.fija(2026, 10, 7, 19, 0, 0)
    assert f["departamento"] == "Lima"


def test_la_region_pedida_manda_sobre_el_detalle():
    """Se pidio la pagina de Cusco: la fila es de Cusco aunque el item diga otra cosa."""
    f = parsear(ITEM, "Cusco", DETALLE)
    assert f["departamento"] == "Cusco"


def test_sin_id_o_sin_texto_no_hay_fila():
    assert parsear({}, "Lima") is None
    assert parsear({"idContrato": "1", "nomEntidad": "X"}, "Lima") is None


def test_fechas_de_la_api():
    assert _fecha("07/10/2026 13:04:34") == fechas.fija(2026, 10, 7, 13, 4, 34)
    assert _fecha("07/10/2026") == fechas.fija(2026, 10, 7)
    assert _fecha("") is None and _fecha(None) is None and _fecha("ayer") is None


def test_los_codigos_cubren_los_25_departamentos_de_la_app():
    assert sorted(CODIGOS_DEPARTAMENTO.values()) == sorted(DEPARTAMENTOS)
    assert sorted(CODIGOS_DEPARTAMENTO) == list(range(1, 26))


# ─── Ritmo: SEACE corta las rafagas con 403 desde el 14/10/2026 ─

class _Resp:
    def __init__(self, status, cuerpo=None):
        self.status_code, self._cuerpo = status, cuerpo or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "https://prod6.seace.gob.pe/x")
            raise httpx.HTTPStatusError("corte", request=req,
                                        response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._cuerpo


class _Cliente:
    def __init__(self, respuestas):
        self.respuestas, self.pedidas = list(respuestas), []

    async def get(self, url, params=None):
        self.pedidas.append((url, params))
        return self.respuestas.pop(0) if self.respuestas else _Resp(403)


@pytest.fixture
def sin_esperas(monkeypatch):
    from radar_bot.scrapers import oece_menores

    async def nada(_):
        return None
    monkeypatch.setattr(oece_menores.asyncio, "sleep", nada)


@pytest.mark.asyncio
async def test_ante_un_corte_por_ritmo_se_espera_y_se_reintenta_una_vez(sin_esperas):
    from radar_bot.scrapers.oece_menores import _get
    cliente = _Cliente([_Resp(403), _Resp(200, {"data": [1]})])
    assert await _get(cliente, "/contrataciones/buscador", page=1) == {"data": [1]}
    assert len(cliente.pedidas) == 2
    # Dos cortes seguidos: se rinde y lo dice.
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        await _get(_Cliente([_Resp(403), _Resp(403)]), "/contrataciones/buscador")


@sin_base
@pytest.mark.asyncio
async def test_tres_regiones_cortadas_seguidas_dejan_la_corrida_para_la_proxima_hora(
        sin_esperas, monkeypatch):
    from radar_bot.scrapers import oece_menores
    cliente = _Cliente([])            # todo responde 403

    class _Fabrica:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return cliente

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(oece_menores.httpx, "AsyncClient", _Fabrica)
    monkeypatch.setattr(oece_menores, "_verificar_codigos", lambda c: _nada())
    assert await oece_menores.scrape_oece_menores(0) == []
    # 3 regiones x 2 intentos, y no las 25.
    busquedas = [p for u, p in cliente.pedidas if u.endswith("/contrataciones/buscador")]
    assert len(busquedas) == oece_menores.CORTES_SEGUIDOS_MAX * 2


async def _nada():
    return None

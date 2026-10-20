"""Hallazgos de la auditoria en los scrapers: que no fallen en silencio y que
no pidan lo que el HTML de un portal ajeno les diga.

QUE PROTEGE

  * Descargas (red_segura): solo http/https, solo el dominio del portal,
    nunca una IP interna, redirecciones revalidadas y tope en streaming.
  * TLS: sin verificar SOLO el host del GOREMAD (autofirmado, medido).
  * Fallos de SC/DJ/ficha a warning, y una ficha que no cargo no descarta la
    convocatoria por falta de keywords.
  * oece_menores no guarda una fila nueva sin detalle (la reintenta).
  * gob_pe: sin id no hay fila, "18 UIT" no es 8 UIT, `&amp;` decodificado.
  * BaseScraper reintenta los errores de red y registra los no-200.
  * SEACE dice la causa de un no-200; datos_abiertos no acepta "inf"/"nan";
    OCDS no suma importes en monedas distintas.

  Ninguna toca la red ni la base: MockTransport, clientes falsos y
  monkeypatch de las escrituras.
"""
import logging

import httpx
import pytest

from radar_bot.scrapers import red_segura

PUBLICA = "200.48.225.130"


@pytest.fixture
def dns_publico(monkeypatch):
    async def _resolver(host, puerto):
        return [PUBLICA]
    monkeypatch.setattr(red_segura, "_resolver", _resolver)


# ─── red_segura: que URL se acepta ────────────────────────

DOM = ("regionmadrededios.gob.pe",)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://cotizaciones.regionmadrededios.gob.pe/x.pdf",
    "gopher://cotizaciones.regionmadrededios.gob.pe/",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:5432/",
    "http://cotizaciones.regionmadrededios.gob.pe:8080/x.pdf",
    "http://regionmadrededios.gob.pe.evil.com/x.pdf",
    "http://evilregionmadrededios.gob.pe/x.pdf",
    "http://usuario@evil.com/regionmadrededios.gob.pe/x.pdf",
])
def test_una_url_fuera_del_portal_no_se_pide(url):
    with pytest.raises(red_segura.DescargaRechazada):
        red_segura.validar_url(url, DOM)


@pytest.mark.parametrize("url", [
    "http://cotizaciones.regionmadrededios.gob.pe/solicitud/1/descargar",
    "https://REGIONMADREDEDIOS.gob.pe/storage/x.pdf",
    "https://cotizaciones.regionmadrededios.gob.pe:443/storage/x.pdf",
])
def test_una_url_del_portal_se_acepta(url):
    assert red_segura.validar_url(url, DOM).scheme in ("http", "https")


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1",
                                "172.16.0.1", "100.64.0.1", "0.0.0.0", "::1",
                                "fe80::1%eth0", "::ffff:127.0.0.1", "224.0.0.1"])
async def test_un_host_que_resuelve_a_ip_interna_no_se_pide(monkeypatch, ip):
    async def _resolver(host, puerto):
        return [PUBLICA, ip]     # basta UNA interna entre varias
    monkeypatch.setattr(red_segura, "_resolver", _resolver)
    with pytest.raises(red_segura.DescargaRechazada, match="no publica"):
        await red_segura.exigir_ip_publica(httpx.URL("http://cotizaciones.regionmadrededios.gob.pe/"))


async def test_un_host_con_ip_publica_pasa(dns_publico):
    await red_segura.exigir_ip_publica(httpx.URL("http://cotizaciones.regionmadrededios.gob.pe/"))


# ─── red_segura: la descarga ──────────────────────────────

def _cliente(manejador):
    return httpx.AsyncClient(transport=httpx.MockTransport(manejador))


async def test_baja_el_pdf_del_portal(dns_publico):
    async def manejador(req):
        return httpx.Response(200, content=b"%PDF-1.4 hola")
    async with _cliente(manejador) as c:
        cuerpo = await red_segura.bajar(
            c, "http://cotizaciones.regionmadrededios.gob.pe/solicitud/1/descargar", DOM, 1000)
    assert cuerpo == b"%PDF-1.4 hola"


async def test_una_redireccion_a_otro_host_no_se_sigue(dns_publico):
    pedidas = []

    async def manejador(req):
        pedidas.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
    async with _cliente(manejador) as c:
        with pytest.raises(red_segura.DescargaRechazada, match="fuera de"):
            await red_segura.bajar(c, "http://cotizaciones.regionmadrededios.gob.pe/a", DOM, 1000)
    assert len(pedidas) == 1     # el destino nunca se pidio


async def test_una_redireccion_dentro_del_portal_se_sigue(dns_publico):
    async def manejador(req):
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/storage/b.pdf"})
        return httpx.Response(200, content=b"%PDF b")
    async with _cliente(manejador) as c:
        assert await red_segura.bajar(
            c, "http://cotizaciones.regionmadrededios.gob.pe/a", DOM, 1000) == b"%PDF b"


async def test_mas_de_tres_redirecciones_se_cortan(dns_publico):
    pedidas = []

    async def manejador(req):
        pedidas.append(req.url.path)
        return httpx.Response(302, headers={"location": f"/r{len(pedidas)}"})
    async with _cliente(manejador) as c:
        with pytest.raises(red_segura.DescargaRechazada, match="redirecciones"):
            await red_segura.bajar(c, "http://cotizaciones.regionmadrededios.gob.pe/r0", DOM, 1000)
    assert len(pedidas) == red_segura.MAX_REDIRECCIONES + 1


async def test_el_tope_corta_en_streaming_sin_cargarlo_todo(dns_publico):
    servidos = []

    async def trozos():
        for _ in range(100):
            servidos.append(1)
            yield b"x" * 1024

    async def manejador(req):
        return httpx.Response(200, content=trozos())    # sin Content-Length
    async with _cliente(manejador) as c:
        with pytest.raises(red_segura.DescargaRechazada, match="bytes"):
            await red_segura.bajar(c, "http://cotizaciones.regionmadrededios.gob.pe/x", DOM, 4096)
    assert len(servidos) < 100


async def test_un_content_length_grande_se_rechaza_antes_de_leer(dns_publico):
    async def manejador(req):
        return httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")
    async with _cliente(manejador) as c:
        with pytest.raises(red_segura.DescargaRechazada, match="tope"):
            await red_segura.bajar(c, "http://cotizaciones.regionmadrededios.gob.pe/x", DOM, 4096)


async def test_un_404_es_rechazo_con_su_codigo(dns_publico):
    async def manejador(req):
        return httpx.Response(404)
    async with _cliente(manejador) as c:
        with pytest.raises(red_segura.DescargaRechazada, match="HTTP 404"):
            await red_segura.bajar(c, "http://cotizaciones.regionmadrededios.gob.pe/x", DOM, 4096)


# ─── TLS ──────────────────────────────────────────────────

def test_solo_el_host_autofirmado_va_sin_verificar():
    import ssl
    c = red_segura.cliente(timeout=5)

    def modo(url):
        return c._transport_for_url(httpx.URL(url))._pool._ssl_context.verify_mode

    assert modo("https://cotizaciones.regionmadrededios.gob.pe/x") == ssl.CERT_NONE
    for otro in ("https://www.gob.pe/", "https://prod6.seace.gob.pe/",
                 "https://evil.cotizaciones.regionmadrededios.gob.pe/",
                 "https://regionmadrededios.gob.pe/"):
        assert modo(otro) == ssl.CERT_REQUIRED, otro


def test_ningun_scraper_apaga_tls_para_todo_el_cliente():
    """Solo red_segura puede pasar verify=False (y solo a su transporte por host)."""
    import ast
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parent.parent / "radar_bot"
    culpables = []
    for p in raiz.rglob("*.py"):
        for nodo in ast.walk(ast.parse(p.read_text("utf-8"))):
            if isinstance(nodo, ast.keyword) and nodo.arg == "verify" \
                    and isinstance(nodo.value, ast.Constant) and nodo.value.value is False:
                culpables.append(f"{p.name}:{nodo.value.lineno}")
    assert [c for c in culpables if not c.startswith("red_segura.py:")] == []


# ─── Documentos y ficha del GOREMAD ───────────────────────

async def test_un_documento_que_no_baja_queda_marcado_y_avisa(monkeypatch, caplog):
    from radar_bot.scrapers import orchestrator as o
    import shared.db
    guardados = []

    async def _conocidos(lic):
        return {}

    async def _guardar(lic, tipo, cat, nombre, url, contenido, tamano=None):
        guardados.append((url, contenido))

    async def _bajar(client, url, dominios, maximo):
        raise red_segura.DescargaRechazada("host evil.com fuera de regionmadrededios.gob.pe")

    async def _nada(_s):
        return None
    monkeypatch.setattr(shared.db, "documentos_conocidos", _conocidos)
    monkeypatch.setattr(shared.db, "guardar_documento", _guardar)
    monkeypatch.setattr(red_segura, "bajar", _bajar)
    monkeypatch.setattr(o.asyncio, "sleep", _nada)
    docs = [{"tipo": "sc", "categoria": "SC", "nombre": "sc.pdf",
             "url": "http://evil.com/solicitud/1/descargar"}]
    with caplog.at_level(logging.WARNING, logger="radar.orchestrator"):
        await o._guardar_documentos(object(), "gore_x", docs, DOM)
    # La fila queda con su enlace y SIN contenido: la proxima pasada reintenta.
    assert guardados == [("http://evil.com/solicitud/1/descargar", None)]
    assert any("evil.com/solicitud/1/descargar" in r.getMessage() and "fuera de" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


async def test_una_ficha_que_no_carga_avisa_y_se_distingue_de_una_sin_items(caplog):
    from radar_bot.scrapers import orchestrator as o

    class _Cliente:
        async def get(self, url):
            return httpx.Response(500, request=httpx.Request("GET", url))
    with caplog.at_level(logging.WARNING, logger="radar.orchestrator"):
        leida, detalle = await o._pedir_ficha(
            _Cliente(), "http://cotizaciones.regionmadrededios.gob.pe/solicitud/9", None, DOM)
    assert (leida, detalle) == (False, None)
    assert any("HTTP 500" in r.getMessage() for r in caplog.records)

    class _Vacia:
        async def get(self, url):
            return httpx.Response(200, text="<html><body>sin items</body></html>",
                                  request=httpx.Request("GET", url))
    assert await o._pedir_ficha(_Vacia(), "http://cotizaciones.regionmadrededios.gob.pe/s/1",
                                None, DOM) == (True, None)


async def test_una_ficha_fuera_del_portal_ni_se_pide():
    from radar_bot.scrapers import orchestrator as o

    class _Cliente:
        async def get(self, url):
            raise AssertionError("no debia pedirse")
    assert await o._pedir_ficha(_Cliente(), "http://10.0.0.1/solicitud/1", None, DOM) == (False, None)


FILTROS = {"keywords": ["arroz"], "keywords_excluir": ["vigilancia"], "regiones": ["Madre de Dios"],
           "monto_min": 0, "monto_max": 999999999}
CONCEPTO = "[BIENES] ADQUISICION DE ALIMENTOS NO PERECIBLES PARA LA GERENCIA REGIONAL"


def test_un_detalle_desconocido_no_se_descarta_por_keywords():
    from radar_bot.scrapers import orchestrator as o
    ent = "Gobierno Regional de Madre de Dios"
    # Ficha leida y sin arroz: fuera, como siempre.
    assert not o._apply_filters(ent, CONCEPTO, None, "Madre de Dios", FILTROS, detalle="SAL")
    # Ficha que no cargo: no se sabe que compra, no se descarta.
    assert o._apply_filters(ent, CONCEPTO, None, "Madre de Dios", FILTROS, detalle_desconocido=True)
    # Lo demas sigue filtrando.
    assert not o._apply_filters(ent, CONCEPTO, None, "Cusco", FILTROS, detalle_desconocido=True)
    assert not o._apply_filters(ent, CONCEPTO + " Y VIGILANCIA", None, "Madre de Dios", FILTROS,
                                detalle_desconocido=True)


async def test_la_pasada_guarda_la_cotizacion_cuya_ficha_no_cargo(monkeypatch):
    from radar_bot.scrapers import orchestrator as o
    guardadas = []
    listado = """<html><body><table>
    <tr><td>BIENES</td><td>2026</td><td>0123</td><td>Alimentos</td>
    <td>ADQUISICION DE ALIMENTOS NO PERECIBLES PARA LA GERENCIA REGIONAL</td>
    <td>INICIO:07/10/2026 09:00 FIN:10/10/2026 12:00</td>
    <td><a href="/solicitud/555">ver</a></td></tr></table></body></html>"""

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            req = httpx.Request("GET", url)
            if "/solicitud/" in url:
                return httpx.Response(503, request=req)
            return httpx.Response(200, text=listado + " " * 3000, request=req)

    async def _start(f):
        return 1

    async def _end(*a, **k):
        return None

    async def _filtros(uid):
        return dict(FILTROS)

    async def _refrescar(licit):
        guardadas.append(licit)
        return True

    async def _nada(_s):
        return None
    monkeypatch.setattr(o, "log_scraping_start", _start)
    monkeypatch.setattr(o, "log_scraping_end", _end)
    monkeypatch.setattr(o, "_get_filters", _filtros)
    monkeypatch.setattr(o, "refrescar_licitacion", _refrescar)
    monkeypatch.setattr(o.asyncio, "sleep", _nada)
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente)
    monkeypatch.setattr(o, "GORE_COTIZACIONES_PORTALS", {
        "Madre de Dios": o.GORE_COTIZACIONES_PORTALS["Madre de Dios"]})

    await o._run_gore_portals(0)

    assert len(guardadas) == 1
    assert guardadas[0]["descripcion"] is None


# ─── oece_menores ─────────────────────────────────────────

async def test_oece_una_fila_nueva_sin_detalle_no_se_guarda(monkeypatch):
    from radar_bot.scrapers import oece_menores as om
    guardadas, cierre = [], {}

    class _Resp:
        def __init__(self, codigo, datos=None):
            self.status_code, self._d = codigo, datos

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    str(self.status_code), request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(self.status_code))

        def json(self):
            return self._d

    fila = {"idContrato": 77, "nomEntidad": "MUNICIPALIDAD DISTRITAL DE LAS PIE",
            "desObjetoContrato": "ADQUISICION DE CEMENTO PORTLAND TIPO",
            "fecFinCotizacion": "30/10/2026 17:00:00", "nomObjetoContrato": "Bien"}

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if url.endswith("/contrataciones/buscador"):
                return _Resp(200, {"data": [fila], "pageable": {"totalElements": 1}})
            return _Resp(500)

    async def _start(f):
        return 1

    async def _end(log_id, enc, nuevas, errores, detalle=None):
        cierre.update(enc=enc, errores=errores, detalle=detalle)

    async def _ya(ids):
        return {}

    async def _refrescar(lic):
        guardadas.append(lic)
        return True

    async def _orden():
        return [(17, "Madre de Dios")]

    async def _nada(*a, **k):
        return None
    monkeypatch.setattr(om.httpx, "AsyncClient", _Cliente)
    monkeypatch.setattr(om, "log_scraping_start", _start)
    monkeypatch.setattr(om, "log_scraping_end", _end)
    monkeypatch.setattr(om, "_ya_guardadas", _ya)
    monkeypatch.setattr(om, "refrescar_licitacion", _refrescar)
    monkeypatch.setattr(om, "_orden_regiones", _orden)
    monkeypatch.setattr(om, "_verificar_codigos", _nada)
    monkeypatch.setattr(om.asyncio, "sleep", _nada)

    assert await om.scrape_oece_menores(0) == []
    assert guardadas == []                 # se reintenta la proxima pasada
    assert cierre["errores"] == 1 and "77" in cierre["detalle"]


# ─── gob_pe ───────────────────────────────────────────────

def _item_gob(**cambios):
    base = {
        "id": "6502648",
        "publication": "28 de agosto de 2026",
        "name_with_parent": "Solicitud de cotización",
        "content_sub_title_card": "SUSALUD - Superintendencia Nacional de Salud",
        "url": '<a href="/busqueda?a=1&amp;b=2">Solicitud de cotización</a>',
        "content": "adquisición de bienes",
        "action_url": "https://cdn.www.gob.pe/uploads/doc.pdf?x=1&amp;y=2",
    }
    base.update(cambios)
    return base


@pytest.mark.parametrize("sin_id", [{"id": None}, {"id": ""}])
def test_gobpe_un_item_sin_id_se_descarta(sin_id):
    from radar_bot.scrapers.gob_pe import _parsear_item
    item = _item_gob(**sin_id)
    if sin_id["id"] is None:
        item.pop("id")
    assert _parsear_item(item) is None


def test_gobpe_el_href_se_decodifica():
    from radar_bot.scrapers.gob_pe import _parsear_item
    d = _parsear_item(_item_gob())
    assert d["url"] == "https://www.gob.pe/busqueda?a=1&b=2"
    assert d["bases_urls"] == ["https://cdn.www.gob.pe/uploads/doc.pdf?x=1&y=2"]


@pytest.mark.parametrize("texto, es", [
    ("Contratacion menor a 8 UIT", True),
    ("bienes por montos menores a 8uit", True),
    ("Remuneracion de 18 UIT anuales", False),
    ("tope de 28 uit", False),
    ("8 uits", False),
])
def test_gobpe_8_uit_con_bordes(texto, es):
    from radar_bot.scrapers.gob_pe import _es_compra
    assert _es_compra(texto, "") is es


# ─── BaseScraper ──────────────────────────────────────────

@pytest.fixture
def base_aislada(monkeypatch):
    from radar_bot.scrapers import base_scraper as b
    cierre = {}

    async def _start(f):
        return 1

    async def _end(log_id, enc, nuevas, errores, detalle=None):
        cierre.update(enc=enc, errores=errores, detalle=detalle)

    async def _config(uid):
        return None
    monkeypatch.setattr(b, "log_scraping_start", _start)
    monkeypatch.setattr(b, "log_scraping_end", _end)
    monkeypatch.setattr(b, "get_config", _config)
    return cierre


async def test_base_reintenta_los_errores_de_red(base_aislada):
    from radar_bot.scrapers.base_scraper import BaseScraper
    intentos = []

    class _S(BaseScraper):
        FUENTE = "prueba"
        ESPERA_MIN = 0

        async def _fetch_items(self, client):
            intentos.append(1)
            if len(intentos) < 3:
                raise httpx.ConnectError("caido")
            return []

    await _S().scrape(0)
    assert len(intentos) == 3
    assert base_aislada["errores"] == 0


async def test_base_tras_agotar_reintentos_deja_la_causa(base_aislada):
    from radar_bot.scrapers.base_scraper import BaseScraper

    class _S(BaseScraper):
        FUENTE = "prueba"
        ESPERA_MIN = 0

        async def _fetch_items(self, client):
            raise httpx.ConnectError("sin ruta")

    await _S().scrape(0)
    assert base_aislada["errores"] == 1
    assert "ConnectError" in base_aislada["detalle"]


async def test_base_un_no_200_se_registra(base_aislada, caplog):
    from radar_bot.scrapers.base_scraper import BaseScraper

    class _Falso:
        async def get(self, url, **k):
            return httpx.Response(404, request=httpx.Request("GET", url))

    class _S(BaseScraper):
        FUENTE = "prueba"

        async def _fetch_items(self, client):
            resp = await self._pedir(_Falso(), "https://www.sbs.gob.pe/x")
            return [] if resp is None else ["no"]

    with caplog.at_level(logging.WARNING):
        await _S().scrape(0)
    assert "HTTP 404" in base_aislada["detalle"]
    assert base_aislada["errores"] == 1
    assert any("HTTP 404" in r.getMessage() for r in caplog.records)


# ─── SEACE ────────────────────────────────────────────────

async def test_seace_un_no_200_queda_con_su_causa(monkeypatch, caplog):
    from radar_bot.scrapers import seace
    cierre = {}

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return httpx.Response(503, request=httpx.Request("GET", url))

    async def _start(f):
        return 1

    async def _end(log_id, enc, nuevas, errores, detalle=None):
        cierre.update(detalle=detalle)

    async def _config(uid):
        return None
    monkeypatch.setattr(seace.httpx, "AsyncClient", _Cliente)
    monkeypatch.setattr(seace, "log_scraping_start", _start)
    monkeypatch.setattr(seace, "log_scraping_end", _end)
    monkeypatch.setattr(seace, "get_config", _config)
    with caplog.at_level(logging.WARNING, logger="radar"):
        await seace.scrape_seace(0)
    assert "HTTP 503" in cierre["detalle"]
    assert "reCAPTCHA" not in cierre["detalle"]
    assert any("503" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ─── Montos ───────────────────────────────────────────────

@pytest.mark.parametrize("valor", ["inf", "nan", "-inf", "Infinity", "NaN", "1e400"])
def test_datos_abiertos_un_monto_no_finito_es_none(valor):
    from radar_bot.scrapers.datos_abiertos import _parse_monto
    assert _parse_monto(valor) is None


def test_datos_abiertos_un_monto_normal_se_lee():
    from radar_bot.scrapers.datos_abiertos import _parse_monto
    assert _parse_monto("1,234.50") == 1234.5


def test_ocds_no_suma_importes_de_monedas_distintas(caplog):
    from radar_bot.scrapers.ocds_oece import _adjudicacion
    release = {"ocid": "ocds-x", "tag": ["award"], "parties": [], "awards": [
        {"value": {"amount": 1000, "currency": "PEN"}},
        {"value": {"amount": 500, "currency": "USD"}},
        {"items": [{"totalValue": {"amount": 200}}]},                 # sin moneda: PEN
        {"items": [{"totalValue": {"amount": 300, "currency": "EUR"}}]},
        {"value": {"amount": "NaN", "currency": "PEN"}},
    ]}
    with caplog.at_level(logging.WARNING, logger="radar.ocds_oece"):
        datos = _adjudicacion(release)
    assert datos["monto_adjudicado"] == 1200
    assert any("USD" in r.getMessage() for r in caplog.records)

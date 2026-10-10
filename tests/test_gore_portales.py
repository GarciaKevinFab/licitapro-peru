"""Los portales de cotizaciones de los GORE se diagnostican uno a uno.

QUE PROTEGE

  La primera pasada del puente con Cusco en la lista (2026-10-07) anoto
  `25 encontradas, 11 nuevas, error_detalle NULL`. Las 25 eran de Madre de
  Dios. Cusco respondio 200 con una pagina sin tabla y el parser salio con
  cero filas sin decir nada: con una sola sonda para la fuente entera, el
  portal que funciona tapa al que no.

  Aqui se comprueba que el portal mudo queda nombrado en `error_detalle`
  aunque el otro haya rendido, y que un dia en que los dos rinden no deja
  diagnostico (una alarma que suena siempre es una alarma que nadie oye).

POR QUE NO TOCA NI LA RED NI LA BASE

  Los dos portales solo responden a conexiones peruanas; una prueba que los
  pidiera de verdad fallaria en el VPS y en CI. Se simula el cliente HTTP y
  se capturan las escrituras a `scraping_log`.
"""
import pytest

from radar_bot.scrapers import orchestrator as o

TABLA = """<html><head><title>Cotizaciones GOREMAD</title></head><body>
<table>
<tr><th>TIPO</th><th>ANO</th><th>NUM</th><th>RUBRO</th><th>CONCEPTO</th><th>FECHAS</th><th>ACC</th></tr>
<tr><td>BIENES</td><td>2026</td><td>0123</td><td>Utiles</td>
<td>Adquisicion de utiles de escritorio para la sede regional</td>
<td>INICIO:07/10/2026 09:00 FIN:10/10/2026 12:00</td><td><a href="/detalle/123">ver</a></td></tr>
</table></body></html>"""

# Mas grande que _PAGINA_MINIMA para que el diagnostico apunte a los
# selectores y no a "la entidad tiene la aplicacion caida".
SIN_TABLA = ("<html><head><title>Plesk Obsidian 18</title></head><body>"
             + "<p>Inicie sesion para continuar</p>" * 200 + "</body></html>")


class _Resp:
    def __init__(self, url, html, codigo=200):
        self.status_code = codigo
        self.text = html
        self.content = html.encode()
        self.url = url


class _Cliente:
    """Cliente HTTP falso: contesta segun el host y sirve de contexto async."""

    def __init__(self, respuestas):
        self.respuestas = respuestas

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        for trozo, html in self.respuestas.items():
            if trozo in url:
                return _Resp(url, html)
        return _Resp(url, "", 404)


@pytest.fixture
def _aislado(monkeypatch):
    """Sin base, sin red, sin esperas. Devuelve lo que se escribio en scraping_log."""
    escrito = {}

    async def _start(fuente):
        return 1

    async def _end(log_id, enc, nuevas, errores, detalle=None):
        escrito.update(encontradas=enc, nuevas=nuevas, errores=errores, detalle=detalle)

    async def _filtros(user_id):
        return {"keywords": [], "keywords_excluir": [], "regiones": [],
                "monto_min": 0, "monto_max": 999999999}

    async def _refrescar(licit):
        return True

    async def _sin_espera(_s):
        return None

    monkeypatch.setattr(o, "log_scraping_start", _start)
    monkeypatch.setattr(o, "log_scraping_end", _end)
    monkeypatch.setattr(o, "_get_filters", _filtros)
    monkeypatch.setattr(o, "refrescar_licitacion", _refrescar)
    monkeypatch.setattr(o.asyncio, "sleep", _sin_espera)
    monkeypatch.setattr(o, "GORE_COTIZACIONES_PORTALS", {
        "Madre de Dios": {"url": "http://cotizaciones.regionmadrededios.gob.pe/",
                          "type": "cotizaciones_app",
                          "entidad": "Gobierno Regional de Madre de Dios", "sigla": "GOREMAD"},
        "Cusco": {"url": "https://cotizaciones.regioncusco.gob.pe/",
                  "type": "cotizaciones_app",
                  "entidad": "Gobierno Regional de Cusco", "sigla": "GORECUSCO"},
    })
    return escrito


async def test_un_portal_mudo_no_se_esconde_detras_de_otro_que_rinde(_aislado, monkeypatch):
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente({
        "regionmadrededios": TABLA,
        "regioncusco": SIN_TABLA,
    }))

    nuevas = await o._run_gore_portals(0)

    assert len(nuevas) == 1 and nuevas[0]["departamento"] == "Madre de Dios"
    assert _aislado["encontradas"] == 1
    detalle = _aislado["detalle"] or ""
    assert detalle.startswith("Cusco: SIN EXTRAER"), detalle
    assert "revisar selectores" in detalle
    # El que rindio no aparece en el aviso: el aviso manda a mirar UN sitio.
    assert "Madre de Dios" not in detalle


async def test_un_portal_caido_queda_como_caida_con_su_codigo(_aislado, monkeypatch):
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente({
        "regionmadrededios": TABLA,
        # regioncusco no esta: el cliente contesta 404
    }))

    await o._run_gore_portals(0)

    detalle = _aislado["detalle"] or ""
    assert detalle.startswith("Cusco: CAIDA"), detalle
    assert "HTTP 404" in detalle
    assert _aislado["errores"] == 1


async def test_dos_portales_sanos_no_dejan_diagnostico(_aislado, monkeypatch):
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente({
        "regionmadrededios": TABLA,
        "regioncusco": TABLA.replace("GOREMAD", "GORECUSCO"),
    }))

    nuevas = await o._run_gore_portals(0)

    assert {n["departamento"] for n in nuevas} == {"Madre de Dios", "Cusco"}
    assert _aislado["detalle"] is None
    assert _aislado["errores"] == 0


# ─── La ficha de cada solicitud ──────────────────────────────────────────
#
#   El listado solo trae el concepto. Los items (lo que de verdad se compra)
#   estan en /solicitud/<id>. La fixture es la ficha real de la SC
#   5884-2026-GOREMAD tal como la sirvio el portal el 2026-10-09, con el token
#   CSRF de la sesion vaciado.

from pathlib import Path

from shared.config import match_keywords

FICHA = (Path(__file__).parent / "fixtures" / "goremad_solicitud_21306.html").read_text(
    encoding="utf-8")

CONCEPTO = ("[BIENES] ADQUISICION DE ALIMENTOS NO PERECIBLE - PARA LA GERENCIA "
            "REGIONAL DE GESTION DEL RIESGO DE DESASTRES")

LISTADO_CON_FICHA = TABLA.replace('href="/detalle/123"', 'href="/solicitud/21306"')


def test_la_ficha_da_los_nueve_items_con_cantidad_y_medida():
    detalle = o._detalle_cotizacion(FICHA)

    lineas = detalle.splitlines()
    assert len(lineas) == 9
    assert lineas[0] == "SAL DE MESA (54 KLG)"
    assert lineas[7] == "ARROZ EXTRA (399 KLG)"
    assert lineas[8] == "ACEITE VEGETAL COMESTIBLE (54 LITRO)"
    # Las caracteristicas vacias no dejan "[]"; las que hay van detras.
    assert "[" not in lineas[0]
    assert lineas[3].startswith("LENTEJA CALIDAD 1 - EXTRA (108 KLG) [ESPECIFICACIONES TECNICAS:")
    assert "DAMNIFICADOS" in lineas[3]


def test_una_pagina_sin_items_no_inventa_detalle():
    assert o._detalle_cotizacion("<html><body><p>Mantenimiento</p></body></html>") is None


def test_los_items_hacen_matchear_lo_que_el_concepto_no_nombra():
    """El caso que motivo el cambio: 'arroz' no esta en el concepto."""
    detalle = o._detalle_cotizacion(FICHA)
    for kw in ("arroz", "atun", "aceite", "lenteja", "azucar"):
        assert not match_keywords(CONCEPTO, [kw]), kw
        assert match_keywords(f"{CONCEPTO} {detalle}", [kw]), kw


def test_el_filtro_del_scraper_mira_los_items_pero_no_les_aplica_el_de_ruido():
    filtros = {"keywords": ["arroz"], "keywords_excluir": [], "regiones": [],
               "monto_min": 0, "monto_max": 999999999}
    entidad = "Gobierno Regional de Madre de Dios"

    assert not o._apply_filters(entidad, CONCEPTO, None, "Madre de Dios", filtros)
    assert o._apply_filters(entidad, CONCEPTO, None, "Madre de Dios", filtros,
                            detalle="ARROZ EXTRA (399 KLG)")
    # "inicio" es un patron de ruido de menus; dentro de un item no tumba la fila.
    assert o._apply_filters(entidad, CONCEPTO, None, "Madre de Dios", filtros,
                            detalle="ARROZ EXTRA [ENTREGA AL INICIO DEL MES]")


async def test_la_pasada_guarda_los_items_en_descripcion(_aislado, monkeypatch):
    guardadas = []

    async def _refrescar(licit):
        guardadas.append(licit)
        return True

    monkeypatch.setattr(o, "refrescar_licitacion", _refrescar)
    monkeypatch.setattr(o, "GORE_COTIZACIONES_PORTALS", {
        "Madre de Dios": o.GORE_COTIZACIONES_PORTALS["Madre de Dios"]})
    # "solicitud" va antes: la URL de la ficha tambien contiene el host.
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente({
        "solicitud/21306": FICHA,
        "regionmadrededios": LISTADO_CON_FICHA,
    }))

    await o._run_gore_portals(0)

    assert len(guardadas) == 1
    assert guardadas[0]["url"].endswith("/solicitud/21306")
    assert "ARROZ EXTRA (399 KLG)" in guardadas[0]["descripcion"]
    assert guardadas[0]["monto_referencial"] is None  # el portal no lo publica


async def test_una_ficha_caida_no_tumba_la_fila_ni_marca_el_portal(_aislado, monkeypatch):
    guardadas = []

    async def _refrescar(licit):
        guardadas.append(licit)
        return True

    monkeypatch.setattr(o, "refrescar_licitacion", _refrescar)
    monkeypatch.setattr(o, "GORE_COTIZACIONES_PORTALS", {
        "Madre de Dios": o.GORE_COTIZACIONES_PORTALS["Madre de Dios"]})
    # Solo responde el listado: la ficha da 404.
    monkeypatch.setattr(o.httpx, "AsyncClient", _Cliente({
        "regionmadrededios.gob.pe/": LISTADO_CON_FICHA.replace(
            "/solicitud/21306", "/solicitud/99999"),
    }))
    real_get = _Cliente.get

    async def _get(self, url):
        if "/solicitud/" in url:
            return _Resp(url, "", 404)
        return await real_get(self, url)

    monkeypatch.setattr(_Cliente, "get", _get)

    await o._run_gore_portals(0)

    assert len(guardadas) == 1
    assert guardadas[0]["descripcion"] is None
    assert _aislado["errores"] == 0
    assert _aislado["detalle"] is None

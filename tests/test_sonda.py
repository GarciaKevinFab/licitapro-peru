"""Pruebas de la sonda de fuentes: que un 404 no se vea como silencio.

QUE PROTEGEN

  Durante 21 pasadas seguidas, cinco fuentes de produccion anotaron
  `0 encontradas, 0 errores` mientras sus URLs devolvian 404 y 500. El parte
  diario las listaba como "Sin nuevas", que es la frase que se usa para una
  fuente SANA en domingo. Nadie miro nada porque no habia nada que mirar.

  La averia no estaba en los scrapers: estaba en que la unica linea que sabia
  del 404 lo tiraba a la basura.

      if resp.status_code != 200:
          continue

  Lo que se prueba aqui es que esa informacion sobrevive hasta el parte. Es
  decir: no se prueba que las fuentes funcionen -- eso depende de terceros --,
  sino que cuando dejen de funcionar SE NOTE.

POR QUE NO TOCAN NI LA RED NI LA BASE

  Una prueba que pide la URL de verdad falla el dia que el portal esta en
  mantenimiento, y entonces se acostumbra uno a ver rojo y a ignorarlo. Lo que
  se comprueba es la REGLA, que es lo que se rompe al editar el codigo.
"""
import pytest

from radar_bot.scrapers.orchestrator import Sonda, _corto, format_scraping_report


class _Respuesta:
    def __init__(self, codigo):
        self.status_code = codigo


class _ClienteFalso:
    """Devuelve codigos preparados, o lanza si el 'codigo' es una excepcion."""

    def __init__(self, *codigos):
        self.codigos = list(codigos)

    async def get(self, url):
        codigo = self.codigos.pop(0)
        if isinstance(codigo, Exception):
            raise codigo
        return _Respuesta(codigo)


# ─── Fuente sana ─────────────────────────────────────────

async def test_una_pasada_normal_no_deja_diagnostico():
    """Todo respondio y salieron filas: la sonda calla.

    Una sonda que habla cuando todo va bien llena el parte de ruido, y a las
    dos semanas nadie lo lee -- que es exactamente el estado del que veniamos.
    """
    sonda = Sonda("prueba")
    assert await sonda.get(_ClienteFalso(200), "https://ejemplo.pe/a") is not None
    assert sonda.diagnostico(encontradas=25) is None
    assert sonda.errores == 0


async def test_cero_filas_con_la_pagina_viva_no_es_una_caida():
    """Responde 200 y no extrae nada: apunta a los selectores, no a la URL.

    Son dos averias distintas y mandan a sitios distintos: una se arregla
    cambiando la direccion y la otra mirando el HTML. Un diagnostico
    equivocado hace perder la tarde igual que no tener ninguno.
    """
    sonda = Sonda("prueba")
    await sonda.get(_ClienteFalso(200), "https://ejemplo.pe/a")
    detalle = sonda.diagnostico(encontradas=0)
    assert detalle.startswith("SIN EXTRAER")
    assert "selectores" in detalle


# ─── Fuente caida ────────────────────────────────────────

@pytest.mark.parametrize("codigo", [404, 500, 302, 403])
async def test_ninguna_url_responde_es_una_caida(codigo):
    """Con todas las URLs muertas se dice CAIDA y se dice el codigo.

    El codigo importa: un 403 es un bloqueo por origen -- lo que ya obligo a
    montar el puente -- y un 404 es una pagina que se movio. Sin el numero,
    las dos se investigan igual y una de las dos se investiga en balde.
    """
    sonda = Sonda("prueba")
    assert await sonda.get(_ClienteFalso(codigo), "https://ejemplo.pe/a") is None
    detalle = sonda.diagnostico(encontradas=0)
    assert detalle.startswith("CAIDA")
    assert str(codigo) in detalle
    assert sonda.errores == 1


async def test_una_url_caida_de_varias_no_tumba_la_fuente():
    """La sonda devuelve None y quien llama sigue con la siguiente URL.

    Por eso `get` devuelve None en vez de lanzar: una fuente con tres
    direcciones de las que una murio tiene que seguir leyendo las otras dos.
    """
    cliente = _ClienteFalso(404, 200)
    sonda = Sonda("prueba")
    assert await sonda.get(cliente, "https://ejemplo.pe/vieja") is None
    assert await sonda.get(cliente, "https://ejemplo.pe/buena") is not None

    detalle = sonda.diagnostico(encontradas=12)
    assert detalle.startswith("PARCIAL")
    assert "404" in detalle


async def test_un_fallo_de_red_queda_registrado_con_su_nombre():
    """Un timeout o un DNS caido no son 200 ni 404: tambien tienen que constar."""
    sonda = Sonda("prueba")
    cliente = _ClienteFalso(ConnectionError("sin ruta"))
    assert await sonda.get(cliente, "https://ejemplo.pe/a") is None
    assert "ConnectionError" in sonda.diagnostico(encontradas=0)


def test_la_url_se_acorta_para_que_quepa_en_un_movil():
    assert _corto("https://www.sbs.gob.pe/") == "www.sbs.gob.pe"
    assert _corto("https://a.pe/uno/dos/tres") == "a.pe/.../tres"


# ─── El parte lo cuenta ──────────────────────────────────

def test_el_parte_no_llama_sin_nuevas_a_una_fuente_caida():
    """"Sin nuevas" se reserva para las sanas.

    Es LA frase del problema original: suena a domingo tranquilo y consigue
    que nadie mire. Una fuente caida tiene que leerse distinta de un fin de
    semana sin convocatorias.
    """
    parte = format_scraping_report({
        "timestamp": "2026-08-30T08:00:00",
        "total_nuevas": 0,
        "por_fuente": {"gore_portals": 0, "ocds_oece": 0},
        "errores": [],
        "diagnosticos": {"gore_portals": "CAIDA -- a.pe/x: HTTP 404"},
    })
    assert "gore_portals: CAIDA" in parte or "GOREs Regionales: CAIDA" in parte
    assert "HTTP 404" in parte
    # La sana si conserva su "Sin nuevas".
    assert "Sin nuevas" in parte


def test_el_detalle_va_escapado_para_que_telegram_no_lo_rechace():
    """Una URL con & rompe el parse_mode HTML y el mensaje NO llega.

    Y un parte que no llega se ve exactamente igual que un dia sin averias,
    que es la forma mas tonta de reintroducir el fallo que esto arregla.
    """
    parte = format_scraping_report({
        "timestamp": "2026-08-30T08:00:00",
        "total_nuevas": 0,
        "por_fuente": {"sbs": 0},
        "errores": [],
        "diagnosticos": {"sbs": "CAIDA -- a.pe/x?a=1&b=<2>: HTTP 404"},
    })
    assert "&amp;" in parte and "&lt;2&gt;" in parte
    assert "?a=1&b=<2>" not in parte
